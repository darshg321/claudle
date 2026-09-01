"""Watch the subscription's rolling 5-hour window; anchor and announce resets.

Two jobs, one loop:

* **Announce.** Poll Anthropic's OAuth usage endpoint (a plain HTTPS GET — it
  costs no tokens) and DM the owner the moment the 5-hour window rolls over.

* **Anchor.** The window is *rolling*: it opens on your first request and closes
  five hours later. Left alone it drifts — a window opened by a 2am insomnia
  prompt expires at 7am, when you are asleep, and the next one opens whenever
  you happen to sit down. Firing one tiny prompt right after each reset pins the
  next window to a boundary you know, so a long session never runs into a
  ceiling that arrived at an unpredictable hour.

  The anchor costs ~1.3k cached input tokens and a handful of output tokens on
  Haiku. Subscription usage windows are shared across models, so a Haiku anchor
  opens exactly the window Opus later draws on.

Everything here is blocking except `watch`, which is the asyncio task the bot
starts on login.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone

import config
import usage


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def snapshot() -> dict:
    """Current 5-hour window state. Raises usage.UsageError if unreachable."""
    payload = usage.fetch_limits()
    block = payload.get("five_hour") or {}
    resets_at = block.get("resets_at")
    return {
        "utilization": float(block.get("utilization") or 0.0),
        "resets_at": resets_at,
        "resets_dt": _parse(resets_at),
        "open": bool(resets_at),
        "payload": payload,
    }


def _load_state() -> dict:
    try:
        return json.loads(config.WINDOW_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        config.WINDOW_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def anchor(model: str | None = None) -> dict:
    """Open a new 5-hour window with the cheapest request that can do it.

    `--tools ""` drops every tool definition from the request and
    `--no-session-persistence` keeps it out of the transcript history, so this
    leaves nothing behind for a later `--resume` to re-read.
    """
    argv = [
        "cmd.exe", "/c", "claude",
        "-p",
        "--model", model or config.WINDOW_WARM_MODEL,
        "--effort", "low",
        "--tools", "",
        "--no-chrome",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--output-format", "json",
        "Reply with the single word: ok",
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        timeout=180,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        blob = json.loads(proc.stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError(
            proc.stderr.decode("utf-8", "replace").strip()[:400] or "claude produced no JSON"
        )
    usage_blob = blob.get("usage") or {}
    return {
        "ok": not blob.get("is_error"),
        "cost": float(blob.get("total_cost_usd") or 0.0),
        "input": int(usage_blob.get("input_tokens") or 0),
        "cache_write": int(usage_blob.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage_blob.get("cache_read_input_tokens") or 0),
        "output": int(usage_blob.get("output_tokens") or 0),
    }


def describe(state: dict) -> str:
    if not state["open"]:
        return "No 5-hour window is open — the next prompt starts one."
    when = state["resets_dt"]
    local = when.astimezone() if when else None
    left = ""
    if when:
        seconds = int((when - datetime.now(timezone.utc)).total_seconds())
        if seconds > 0:
            left = f" (in {seconds // 3600}h {seconds % 3600 // 60}m)"
    stamp = local.strftime("%H:%M") if local else state["resets_at"]
    return f"5-hour window at **{state['utilization']:.1f}%** · resets **{stamp}**{left}"


async def watch(notify) -> None:
    """Poll until cancelled, calling `notify(text)` on every reset.

    `notify` is an async callable taking one string.
    """
    state = _load_state()
    last_resets_at = state.get("resets_at")
    announced = state.get("announced_at")

    while True:
        try:
            now = await asyncio.to_thread(snapshot)
        except usage.UsageError:
            # A stale OAuth token or a flaky network must never kill the loop;
            # the next poll picks it back up.
            await asyncio.sleep(config.WINDOW_POLL_SECONDS)
            continue
        except Exception:
            await asyncio.sleep(config.WINDOW_POLL_SECONDS)
            continue

        current = now["resets_at"]
        rolled = (
            last_resets_at is not None
            and current != last_resets_at
            and announced != last_resets_at
        )

        if rolled:
            announced = last_resets_at
            lines = ["♻️ **Usage window reset.**"]
            if now["open"]:
                lines.append(describe(now))
            else:
                lines.append("The 5-hour window is empty — nothing is counting against it.")

            if config.WINDOW_WARM:
                try:
                    result = await asyncio.to_thread(anchor)
                    fresh = await asyncio.to_thread(snapshot)
                    lines.append(
                        f"⚓ Anchored the next window with a {result['cache_write'] + result['cache_read'] + result['input']:,}-token "
                        f"probe on `{config.WINDOW_WARM_MODEL}`."
                    )
                    lines.append(describe(fresh))
                    current = fresh["resets_at"]
                except Exception as exc:
                    lines.append(f"⚠️ Could not anchor the new window: {exc}")

            await notify("\n".join(lines))

        last_resets_at = current
        _save_state({"resets_at": current, "announced_at": announced})

        # Poll harder as the reset approaches so the DM lands close to the event.
        delay = config.WINDOW_POLL_SECONDS
        if now["resets_dt"]:
            seconds = (now["resets_dt"] - datetime.now(timezone.utc)).total_seconds()
            if 0 < seconds < 600:
                delay = 30
        await asyncio.sleep(delay)
