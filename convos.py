"""Named, switchable Claude conversations, one set per Discord channel.

The bot used to keep exactly one session per channel, resumed forever. That is
the worst case for prompt caching: the history only ever grows, every turn
re-sends all of it, and a single cache miss reprocesses the lot at full price.
Naming conversations makes the alternative cheap — keep a short one per task and
switch, instead of piling every task into one transcript.

State lives in .state/sessions.json. The old single-session-per-channel format
is migrated on first read, so an upgrade never loses a running conversation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

import config

# Roughly where a resumed conversation stops being cheap. Not a hard limit —
# Claude Code auto-compacts long before the context window fills — but past this
# many turns the history dominates every request, so the bot suggests a split.
BUSY_TURNS = 40

_LABEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def valid_label(label: str) -> bool:
    return bool(_LABEL_OK.match(label))


@dataclass
class Convo:
    label: str
    cwd: str
    session_id: str | None = None
    model: str = ""
    effort: str = ""
    turns: int = 0
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def summary(self, active: bool) -> str:
        mark = "▶" if active else " "
        age = _ago(self.last_used)
        bits = [f"{self.turns} turns", age]
        if self.model:
            bits.append(self.model)
        if self.effort:
            bits.append(self.effort)
        state = "fresh" if not self.session_id else " · ".join(bits)
        return f"{mark} {self.label:<20} {state}\n    {self.cwd}"


def _ago(stamp: float) -> str:
    seconds = max(0, int(time.time() - stamp))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class ChannelConvos:
    """Every conversation for one channel, plus which one is active."""

    def __init__(self, cwd: str):
        self.convos: dict[str, Convo] = {}
        self.active: str = "main"
        self.convos["main"] = Convo(label="main", cwd=cwd)

    # ------------------------------------------------------------------ access

    @property
    def current(self) -> Convo:
        if self.active not in self.convos:
            # The active label was dropped; fall back to any survivor.
            self.active = next(iter(self.convos), "main")
            self.convos.setdefault(self.active, Convo(label=self.active, cwd=config.DEFAULT_WORKDIR))
        return self.convos[self.active]

    def create(self, label: str, cwd: str) -> Convo:
        convo = Convo(label=label, cwd=cwd)
        self.convos[label] = convo
        self.active = label
        return convo

    def switch(self, label: str) -> Convo | None:
        convo = self.convos.get(label)
        if convo is None:
            return None
        self.active = label
        convo.last_used = time.time()
        return convo

    def rename(self, old: str, new: str) -> bool:
        if old not in self.convos or new in self.convos:
            return False
        convo = self.convos.pop(old)
        convo.label = new
        self.convos[new] = convo
        if self.active == old:
            self.active = new
        return True

    def drop(self, label: str) -> bool:
        if label not in self.convos or len(self.convos) == 1:
            return False
        del self.convos[label]
        if self.active == label:
            self.active = next(iter(self.convos))
        return True

    def listing(self) -> str:
        rows = sorted(self.convos.values(), key=lambda c: -c.last_used)
        return "\n".join(c.summary(c.label == self.active) for c in rows)

    # ------------------------------------------------------------------ (de)ser

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "convos": {label: asdict(c) for label, c in self.convos.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, fallback_cwd: str) -> "ChannelConvos":
        self = cls(fallback_cwd)
        raw = data.get("convos")
        if not isinstance(raw, dict) or not raw:
            # Pre-labels format: {"session_id": ..., "cwd": ...}
            self.convos["main"] = Convo(
                label="main",
                cwd=data.get("cwd") or fallback_cwd,
                session_id=data.get("session_id"),
            )
            self.active = "main"
            return self

        self.convos = {}
        for label, blob in raw.items():
            if not isinstance(blob, dict):
                continue
            self.convos[label] = Convo(
                label=label,
                cwd=blob.get("cwd") or fallback_cwd,
                session_id=blob.get("session_id"),
                model=blob.get("model") or "",
                effort=blob.get("effort") or "",
                turns=int(blob.get("turns") or 0),
                created=float(blob.get("created") or time.time()),
                last_used=float(blob.get("last_used") or time.time()),
            )
        if not self.convos:
            self.convos["main"] = Convo(label="main", cwd=fallback_cwd)
        self.active = data.get("active") if data.get("active") in self.convos else next(iter(self.convos))
        return self


# ------------------------------------------------------------------- whole file


def load() -> dict[int, ChannelConvos]:
    try:
        blob = json.loads(config.SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[int, ChannelConvos] = {}
    for key, data in blob.items():
        try:
            channel_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out[channel_id] = ChannelConvos.from_dict(data, config.DEFAULT_WORKDIR)
    return out


def save(all_convos: dict[int, ChannelConvos]) -> None:
    blob = {str(cid): c.to_dict() for cid, c in all_convos.items()}
    try:
        config.SESSION_FILE.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    except OSError:
        pass
