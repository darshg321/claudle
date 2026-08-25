"""Claude subscription usage limits and local token accounting.

Two independent sources, because neither one answers the whole question:

* Anthropic's OAuth usage endpoint knows the *limits* — but only as
  percentages of a rolling window. It never reports token quotas.
* The local session transcripts know the *tokens* — exact counts, but only
  what this machine spent, with no idea what the ceiling is.

Every function here is blocking; call them through asyncio.to_thread.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
CREDENTIALS = CLAUDE_HOME / ".credentials.json"
PROJECTS = CLAUDE_HOME / "projects"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TIMEOUT = 30


class UsageError(RuntimeError):
    pass


def _oauth() -> dict:
    try:
        blob = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(
            "no Claude credentials on this machine — run `claude` once to sign in"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"could not read Claude credentials: {exc}") from None

    creds = blob.get("claudeAiOauth")
    if not creds or not creds.get("accessToken"):
        raise UsageError(
            "Claude is authenticated with an API key, not a subscription — "
            "there are no plan limits to report"
        )
    return creds


def fetch_limits() -> dict:
    """Ask Anthropic where this subscription stands against its rate limits."""
    creds = _oauth()
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "pccontrol-bot",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Claude Code refreshes this token in the background; a stale one
            # means nothing has run recently, not that anything is broken.
            raise UsageError(
                "the stored OAuth token was rejected (401) — run any `claude` "
                "command to refresh it, then try again"
            ) from None
        raise UsageError(f"usage endpoint returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise UsageError(f"could not reach the usage endpoint: {exc.reason}") from None

    payload["_plan"] = creds.get("subscriptionType") or "unknown"
    payload["_token_expires_in"] = creds.get("expiresAt", 0) / 1000 - time.time()
    return payload


# ------------------------------------------------------------------ local tokens


def _iter_usage_records(since: datetime):
    """Yield (timestamp, usage dict) from session transcripts touched since `since`."""
    if not PROJECTS.is_dir():
        return
    cutoff = since.timestamp()
    for path in PROJECTS.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue  # whole file predates the window
        except OSError:
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = (event.get("message") or {}).get("usage")
                    stamp = event.get("timestamp")
                    if not usage or not stamp:
                        continue
                    try:
                        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if when >= since:
                        yield when, usage
        except OSError:
            continue


def local_tokens(hours: float = 5.0) -> dict:
    """Total tokens this machine has spent in the last `hours`."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    totals = {
        "input": 0,
        "output": 0,
        "cache_write": 0,
        "cache_read": 0,
        "messages": 0,
        "sessions": 0,
    }
    for _, usage in _iter_usage_records(since):
        totals["messages"] += 1
        totals["input"] += usage.get("input_tokens") or 0
        totals["output"] += usage.get("output_tokens") or 0
        totals["cache_write"] += usage.get("cache_creation_input_tokens") or 0
        totals["cache_read"] += usage.get("cache_read_input_tokens") or 0

    totals["billable"] = totals["input"] + totals["output"] + totals["cache_write"]
    totals["total"] = totals["billable"] + totals["cache_read"]
    totals["hours"] = hours
    return totals


# ---------------------------------------------------------------------- report


def _bar(percent: float, width: int = 20) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _until(iso: str | None) -> str:
    if not iso:
        return "no reset scheduled"
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    delta = when - datetime.now(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "resetting now"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def _money(block: dict | None) -> str | None:
    if not block:
        return None
    exponent = block.get("exponent", 2)
    amount = block.get("amount_minor", 0) / (10 ** exponent)
    return f"{amount:,.2f} {block.get('currency', '')}".strip()


def _window(payload: dict, key: str, label: str) -> list[str]:
    block = payload.get(key)
    if not block or block.get("utilization") is None:
        return []
    percent = float(block["utilization"])
    return [
        f"{label:<14} {_bar(percent)} {percent:5.1f}%  ({_until(block.get('resets_at'))})"
    ]


def report(payload: dict, tokens: dict) -> str:
    """Render the usage payload plus local token counts as one Discord code block."""
    lines = [f"Plan: {payload.get('_plan', 'unknown')}", ""]

    lines += _window(payload, "five_hour", "5-hour window")
    lines += _window(payload, "seven_day", "7-day window")
    for key, label in (
        ("seven_day_opus", "7-day Opus"),
        ("seven_day_sonnet", "7-day Sonnet"),
    ):
        lines += _window(payload, key, label)

    spend = payload.get("spend") or {}
    if spend.get("enabled"):
        percent = float(spend.get("percent") or 0)
        used, limit = _money(spend.get("used")), _money(spend.get("limit"))
        lines += [
            "",
            f"{'Extra credits':<14} {_bar(percent)} {percent:5.1f}%",
            f"               {used} of {limit} used",
        ]
        if spend.get("spend_limit_reached"):
            lines.append("               ⚠ spend limit reached")

    lines += [
        "",
        f"Tokens used on this machine (last {tokens['hours']:g}h):",
        f"  input        {tokens['input']:>12,}",
        f"  output       {tokens['output']:>12,}",
        f"  cache write  {tokens['cache_write']:>12,}",
        f"  cache read   {tokens['cache_read']:>12,}",
        f"  billable     {tokens['billable']:>12,}   ({tokens['messages']:,} messages)",
        "",
        "Anthropic reports plan limits as % of a rolling window, never as a",
        "token quota — so there is no 'tokens remaining' figure to show.",
    ]
    return "\n".join(lines)
