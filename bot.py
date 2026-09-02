"""Discord DM bot that gives its owner full control of this PC, including
running Claude Code prompts with live progress and screenshots.

Run with:  python bot.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable

import discord

import claude_runner
import config
import convos
import screen
import sysctl
import ui
import usage
import window

# --------------------------------------------------------------------------- state

COMMANDS: dict[str, "Command"] = {}
ALIASES: dict[str, str] = {}


class Command:
    def __init__(self, name, handler, usage, help_text, group, aliases):
        self.name = name
        self.handler = handler
        self.usage = usage
        self.help = help_text
        self.group = group
        self.aliases = aliases


def command(name: str, usage: str, help_text: str, group: str, aliases: tuple[str, ...] = ()):
    """Register a command, refusing any name or alias that is already spoken for.

    on_message resolves aliases *before* command names, so an alias that happens
    to equal another command's name silently swallows it — `!chrome` once
    resolved to `!browser` and the real `!chrome` was unreachable. Failing loudly
    at import turns that into a crash on the next `!relaunch`, which is caught by
    the compile check, instead of a command that quietly does the wrong thing.
    """

    def decorator(func: Callable[["Ctx"], Awaitable[None]]):
        if name in COMMANDS:
            raise ValueError(f"duplicate command name: {name!r}")
        if name in ALIASES:
            raise ValueError(f"command {name!r} is already an alias of {ALIASES[name]!r}")
        for alias in aliases:
            if alias in COMMANDS:
                raise ValueError(f"alias {alias!r} of {name!r} shadows the {alias!r} command")
            if alias in ALIASES:
                raise ValueError(f"alias {alias!r} is already claimed by {ALIASES[alias]!r}")
        COMMANDS[name] = Command(name, func, usage, help_text, group, aliases)
        for alias in aliases:
            ALIASES[alias] = name
        return func

    return decorator


class ChannelState:
    """Live per-channel state, layered over the persisted conversation set.

    `cwd` and `session` are views onto whichever conversation is active, so the
    rest of the bot keeps working against the same two attributes it always did.
    """

    def __init__(self, store: convos.ChannelConvos):
        self.store = store
        self.claude_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        # One live ClaudeSession per label. These are long-lived because
        # `!cancel` needs the object that owns the running subprocess.
        self._sessions: dict[str, claude_runner.ClaudeSession] = {}
        # The session that owns the subprocess right now. Held separately
        # because `!convo` can move `current` mid-run, and cancelling the
        # *newly active* session would leave the real one running headless.
        self.running: claude_runner.ClaudeSession | None = None
        # Last channel object seen for this ID, so the tray can report a kill
        # without depending on discord.py's channel cache holding the DM.
        self.channel: discord.abc.Messageable | None = None

    @property
    def convo(self) -> convos.Convo:
        return self.store.current

    @property
    def cwd(self) -> str:
        return self.convo.cwd

    @cwd.setter
    def cwd(self, value: str) -> None:
        self.convo.cwd = value
        if self.convo.label in self._sessions:
            self._sessions[self.convo.label].cwd = value

    @property
    def session(self) -> claude_runner.ClaudeSession:
        convo = self.convo
        session = self._sessions.get(convo.label)
        if session is None:
            session = claude_runner.ClaudeSession(
                convo.cwd,
                convo.session_id,
                convo.model,
                convo.effort,
                chrome=convo.chrome and config.browser_mode() == "chrome",
            )
            self._sessions[convo.label] = session
        return session

    def reset_session(self) -> None:
        """Drop the active conversation's transcript and start a fresh one."""
        convo = self.convo
        convo.session_id = None
        convo.turns = 0
        convo.warned_busy = False
        self._sessions.pop(convo.label, None)

    def sync(self) -> None:
        """Persist whatever the live session learned back onto the conversation."""
        convo = self.convo
        session = self._sessions.get(convo.label)
        if session is not None:
            convo.session_id = session.session_id
            convo.cwd = session.cwd
        convo.last_used = time.time()


STATES: dict[int, ChannelState] = {}
CONVOS: dict[int, convos.ChannelConvos] = {}

UI = ui.DesktopUI()

# Set once the client is up, so the tray and HUD threads can hop onto the
# asyncio loop. The tray's "Kill current task" and the HUD's Kill button walk
# STATES for whatever is running rather than tracking a single global, which
# used to lose track of the real task whenever two channels overlapped.
LOOP: asyncio.AbstractEventLoop | None = None
WINDOW_TASK: asyncio.Task | None = None

# sessions.json is read exactly once. Keying off "is CONVOS still empty" meant
# the first channel to appear populated it, so every *later* channel silently
# skipped the load and started from scratch on top of its own saved history.
_CONVOS_LOADED = False


def state_for(channel_id: int) -> ChannelState:
    global _CONVOS_LOADED
    if not _CONVOS_LOADED:
        CONVOS.update(convos.load())
        _CONVOS_LOADED = True
    if channel_id not in STATES:
        store = CONVOS.get(channel_id)
        if store is None:
            store = convos.ChannelConvos(config.DEFAULT_WORKDIR)
            CONVOS[channel_id] = store
        STATES[channel_id] = ChannelState(store)
    return STATES[channel_id]


def save_sessions() -> None:
    for channel_id, state in STATES.items():
        state.sync()
        CONVOS[channel_id] = state.store
    convos.save(CONVOS)


# ------------------------------------------------------------------------ plumbing


class Ctx:
    """Everything a command handler needs."""

    def __init__(self, message: discord.Message, name: str, rest: str):
        self.message = message
        self.channel = message.channel
        self.name = name
        self.rest = rest.strip()
        self.state = state_for(message.channel.id)
        self.state.channel = message.channel

    @property
    def args(self) -> list[str]:
        try:
            return shlex.split(self.rest, posix=False)
        except ValueError:
            return self.rest.split()

    def resolve(self, path: str) -> Path:
        expanded = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path(self.state.cwd) / candidate
        return candidate

    async def send(self, content: str = "", **kwargs) -> discord.Message:
        return await self.channel.send(content or None, **kwargs)

    async def ok(self, text: str) -> discord.Message:
        return await self.send(f"✅ {text}")

    async def fail(self, text: str) -> discord.Message:
        return await self.send(f"❌ {text}")

    async def long(self, text: str, lang: str = "", filename: str = "output.txt") -> None:
        await send_long(self.channel, text, lang=lang, filename=filename)

    async def image(self, data: bytes, ext: str, caption: str = "") -> None:
        name = f"{self.name}_{int(time.time())}.{ext}"
        await self.channel.send(caption or None, file=discord.File(io.BytesIO(data), filename=name))


async def send_long(channel, text: str, lang: str = "", filename: str = "output.txt") -> None:
    """Send text, chunking to Discord's limit and falling back to a file attachment."""
    text = text.rstrip()
    if not text:
        await channel.send("_(empty)_")
        return

    if len(text) > 9000:
        await channel.send(
            f"Output is {len(text):,} characters — attached as a file.",
            file=discord.File(io.BytesIO(text.encode("utf-8")), filename=filename),
        )
        return

    fence = f"```{lang}\n" if lang else ""
    close = "```" if lang else ""
    budget = 1990 - len(fence) - len(close)

    chunk: list[str] = []
    size = 0
    for line in text.split("\n"):
        # A single line longer than the budget has to be hard-split.
        while len(line) > budget:
            if chunk:
                await channel.send(fence + "\n".join(chunk) + close)
                chunk, size = [], 0
            await channel.send(fence + line[:budget] + close)
            line = line[budget:]
        if size + len(line) + 1 > budget and chunk:
            await channel.send(fence + "\n".join(chunk) + close)
            chunk, size = [], 0
        chunk.append(line)
        size += len(line) + 1
    if chunk:
        await channel.send(fence + "\n".join(chunk) + close)


class LiveStatus:
    """A single Discord message edited in place to show Claude's progress."""

    MAX_LINES = 14
    MIN_INTERVAL = 1.6

    def __init__(self, message: discord.Message, header: str):
        self.message = message
        self.header = header
        self.lines: list[str] = []
        self._dirty = False
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    def add(self, line: str) -> None:
        self.lines.append(line)
        self._dirty = True

    def replace_header(self, header: str) -> None:
        self.header = header
        self._dirty = True

    def _render(self) -> str:
        shown = self.lines[-self.MAX_LINES :]
        hidden = len(self.lines) - len(shown)
        body = "\n".join(shown)
        if hidden > 0:
            body = f"… {hidden} earlier step(s)\n{body}"
        text = f"{self.header}\n{body}".strip()
        return text[:1990]

    async def flush(self, force: bool = False) -> None:
        if not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self.MIN_INTERVAL:
            return
        async with self._lock:
            self._dirty = False
            self._last_edit = now
            try:
                await self.message.edit(content=self._render())
            except discord.HTTPException:
                pass


def human_duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


async def run_shell(command_line: str, cwd: str, powershell: bool) -> tuple[int, str]:
    if powershell:
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command_line,
        ]
    else:
        argv = ["cmd.exe", "/c", command_line]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"(timed out after {config.SHELL_TIMEOUT}s and was killed)"
    return proc.returncode or 0, out.decode("utf-8", "replace")


# ----------------------------------------------------------------------- Claude Code


@command(
    "claude",
    "!claude <prompt>",
    "Run a Claude Code prompt in the current directory. Any plain message works too.",
    "Claude Code",
    aliases=("c", "ask"),
)
async def cmd_claude(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Give me a prompt, e.g. `!claude summarise the files in this folder`")
        return
    if ctx.state.claude_task and not ctx.state.claude_task.done():
        await ctx.fail("Claude is already working here — `!cancel` to stop it.")
        return

    ctx.state.claude_task = asyncio.create_task(_drive_claude(ctx, ctx.rest))
    await ctx.state.claude_task


async def _drive_claude(ctx: Ctx, prompt: str) -> None:
    """Own the desktop UI for the length of one turn, then always release it."""
    UI.task_started(Path(ctx.state.cwd).name or ctx.state.cwd)
    try:
        await _run_claude_turn(ctx, prompt)
    finally:
        ctx.state.running = None
        UI.task_finished("idle")


async def _run_claude_turn(ctx: Ctx, prompt: str) -> None:
    convo = ctx.state.convo
    session = ctx.state.session
    session.cwd = ctx.state.cwd
    ctx.state.running = session
    resuming = bool(session.session_id)

    header = (
        f"🧠 **Claude Code** · `{convo.label}` · `{ctx.state.cwd}`\n"
        f"{'↩️ resuming session' if resuming else '🆕 new session'}"
    )
    status = LiveStatus(await ctx.send(f"{header}\n⏳ starting…"), header)
    texts: list[str] = []
    final: claude_runner.RunResult | None = None

    async def on_event(event: dict) -> None:
        nonlocal final
        kind = event["kind"]
        if kind == "init":
            model = event.get("model") or "default model"
            status.replace_header(f"{header} · `{model}`")
        elif kind == "session":
            save_sessions()
        elif kind == "thinking":
            status.add("💭 thinking…")
            UI.task_update("thinking…")
        elif kind == "text":
            texts.append(event["text"])
            preview = " ".join(event["text"].split())[:140]
            status.add(f"💬 {preview}")
            UI.task_update(preview)
        elif kind == "tool":
            summary = event.get("summary", "")
            status.add(f"🔧 **{event['name']}** `{summary}`" if summary else f"🔧 **{event['name']}**")
            UI.task_update(f"{event['name']} {summary}".strip())
        elif kind == "tool_result":
            if event.get("is_error"):
                status.add(f"⚠️ error: `{event.get('preview', '')}`")
        elif kind == "notice":
            status.add(f"ℹ️ {event['text']}")
        elif kind == "result":
            final = event["result"]
        await status.flush()

    try:
        result = await session.run(prompt, on_event)
    except asyncio.CancelledError:
        status.add("🛑 cancelled")
        await status.flush(force=True)
        raise
    except claude_runner.ClaudeNotFound as exc:
        await status.message.edit(content=f"❌ {exc}")
        return
    except Exception:
        await status.message.edit(content=f"{header}\n❌ launcher crashed")
        await ctx.long(traceback.format_exc(), lang="py", filename="claude_error.txt")
        return

    final = final or result
    convo.turns += 1
    save_sessions()

    footer_bits = []
    if final.duration_ms:
        footer_bits.append(human_duration(final.duration_ms))
    if final.num_turns:
        footer_bits.append(f"{final.num_turns} turns")
    if final.cost_usd:
        footer_bits.append(f"${final.cost_usd:.4f}")
    if final.tools_used:
        footer_bits.append(f"{len(final.tools_used)} tool calls")

    # Cache health, because it is the single biggest lever on this bot's token
    # spend and an invisible regression otherwise. A resumed turn should be
    # mostly reads; a big write means something invalidated the prefix.
    served = final.cache_read + final.cache_write + final.input_tokens
    if served:
        hit = 100 * final.cache_read / served
        footer_bits.append(f"{hit:.0f}% cached")
        if resuming and hit < 60 and final.cache_write > 20_000:
            footer_bits.append(f"⚠️ {final.cache_write:,} re-cached")

    icon = "❌" if final.is_error else "✅"
    status.replace_header(f"{header}\n{icon} done · {' · '.join(footer_bits) or 'no stats'}")
    await status.flush(force=True)

    # `==` missed the nudge entirely whenever a turn count jumped the boundary
    # (a resumed session, an imported ID); the flag also keeps it to once.
    if convo.turns >= convos.BUSY_TURNS and not convo.warned_busy:
        convo.warned_busy = True
        save_sessions()
        await ctx.send(
            f"📊 `{convo.label}` has hit {convo.turns} turns. Every prompt now re-sends "
            f"all of it. If you have moved on to something else, `{config.PREFIX}convo new <label>` "
            f"is cheaper than continuing here."
        )

    body = final.text or "\n\n".join(texts)
    if body.strip():
        await ctx.long(body, filename="claude_reply.md")
    elif final.stderr:
        await ctx.long(final.stderr, lang="", filename="claude_stderr.txt")
    else:
        await ctx.send("_(Claude produced no output)_")

    if final.stderr and final.is_error:
        await ctx.long(final.stderr[:4000], filename="claude_stderr.txt")

    if config.AUTO_SCREENSHOT and not final.is_error:
        try:
            data, ext, _ = await asyncio.to_thread(screen.screenshot)
            await ctx.image(data, ext, "📸 Screen right after Claude finished")
        except Exception as exc:  # a failed screenshot must not mask a good run
            await ctx.send(f"_(could not grab the follow-up screenshot: {exc})_")


@command("usage", "!usage [hours]", "Claude plan limits and tokens spent on this machine.", "Claude Code", aliases=("limits", "quota"))
async def cmd_usage(ctx: Ctx) -> None:
    # Local name `window` would shadow the module of the same name.
    hours = float(ctx.rest) if ctx.rest.replace(".", "", 1).isdigit() else 5.0
    hours = max(0.1, min(hours, 24 * 7))
    async with ctx.channel.typing():
        try:
            payload = await asyncio.to_thread(usage.fetch_limits)
        except usage.UsageError as exc:
            await ctx.fail(str(exc))
            return
        tokens = await asyncio.to_thread(usage.local_tokens, hours)
    await ctx.long(usage.report(payload, tokens), lang="")


@command(
    "window",
    "!window [anchor]",
    "Show the 5-hour usage window, or open a new one with a tiny probe.",
    "Claude Code",
    aliases=("win",),
)
async def cmd_window(ctx: Ctx) -> None:
    async with ctx.channel.typing():
        try:
            state = await asyncio.to_thread(window.snapshot)
        except usage.UsageError as exc:
            await ctx.fail(str(exc))
            return

        if ctx.rest.strip().lower() not in ("anchor", "warm", "start"):
            watching = "on" if config.WINDOW_WATCH else "off"
            warming = "on" if config.WINDOW_WARM else "off"
            await ctx.send(
                f"{window.describe(state)}\n"
                f"_Reset alerts: **{watching}** · auto-anchor: **{warming}** · "
                f"`{config.PREFIX}window anchor` opens one now._"
            )
            return

        if state["open"]:
            await ctx.fail(
                f"A window is already open — anchoring now would not move it.\n{window.describe(state)}"
            )
            return

        try:
            result = await asyncio.to_thread(window.anchor)
        except Exception as exc:
            await ctx.fail(f"Anchor failed: {exc}")
            return
        fresh = await asyncio.to_thread(window.snapshot)

    spent = result["input"] + result["cache_write"] + result["cache_read"]
    await ctx.ok(
        f"Anchored with a {spent:,}-token probe on `{config.WINDOW_WARM_MODEL}`.\n"
        f"{window.describe(fresh)}"
    )


@command("cancel", "!cancel", "Stop the Claude run in progress.", "Claude Code", aliases=("stopclaude",))
async def cmd_cancel(ctx: Ctx) -> None:
    task = ctx.state.claude_task
    if task and not task.done():
        task.cancel()
        # The session that owns the subprocess, not whichever one is active now.
        await (ctx.state.running or ctx.state.session).cancel()
        await ctx.ok("Cancelled the running Claude turn.")
    else:
        await ctx.fail("Nothing is running here.")


@command("new", "!new", "Wipe the active conversation's history and start it fresh.", "Claude Code", aliases=("reset",))
async def cmd_new(ctx: Ctx) -> None:
    ctx.state.reset_session()
    save_sessions()
    await ctx.ok(f"`{ctx.state.convo.label}` is empty again — the next prompt starts a new session.")


@command(
    "convo",
    "!convo [<label> | new <label> [dir] | switch <label> | rename <a> <b> | drop <label>]",
    "List, create and switch between named conversations.",
    "Claude Code",
    aliases=("chat", "convos"),  # not "c" — that is already !claude
)
async def cmd_convo(ctx: Ctx) -> None:
    store = ctx.state.store
    parts = ctx.args
    p = config.PREFIX

    if not parts or (parts[0].lower() == "list" and "list" not in store.convos):
        await ctx.long(store.listing(), lang="")
        await ctx.send(f"_`{p}convo new <label>` to start one · `{p}convo <label>` to switch_")
        return

    verb = parts[0].lower()
    rest = parts[1:]

    if verb in ("new", "add"):
        if not rest:
            # A pre-existing conversation may still carry a now-reserved name.
            hint = (
                f"\n_You already have one called `{verb}` — `{p}convo switch {verb}` reaches it, "
                f"`{p}convo rename {verb} <newname>` gets it out of the way._"
                if verb in store.convos
                else ""
            )
            await ctx.fail(f"Name it: `{p}convo new refactor`{hint}")
            return
        label = rest[0]
        if not convos.valid_label(label):
            await ctx.fail(
                "Labels are 1-32 chars — letters, digits, `.`, `_`, `-` — and cannot be "
                f"one of: {', '.join(f'`{w}`' for w in sorted(convos.RESERVED))} "
                "(those are `!convo` subcommands)."
            )
            return
        if label in store.convos:
            await ctx.fail(f"`{label}` already exists — `{p}convo {label}` switches to it.")
            return

        cwd = ctx.state.cwd
        if len(rest) > 1:
            target = ctx.resolve(" ".join(rest[1:]))
            if not target.is_dir():
                # Without this a typo'd label silently becomes the directory.
                await ctx.fail(
                    f"`{target}` is not a directory.\n"
                    f"Usage: `{p}convo new <label> [directory]` — labels take no spaces."
                )
                return
            cwd = str(target.resolve())

        store.create(label, cwd)
        save_sessions()
        await ctx.ok(f"Started `{label}` in `{cwd}`. It is now active.")
        return

    if verb == "rename":
        if len(rest) < 2:
            await ctx.fail(f"Usage: `{p}convo rename old new`")
            return
        if not convos.valid_label(rest[1]):
            await ctx.fail(
                "Labels are 1-32 chars — letters, digits, `.`, `_`, `-` — and cannot be "
                f"one of: {', '.join(f'`{w}`' for w in sorted(convos.RESERVED))}."
            )
            return
        if not store.rename(rest[0], rest[1]):
            await ctx.fail(f"Could not rename `{rest[0]}` → `{rest[1]}`.")
            return
        ctx.state._sessions.pop(rest[0], None)
        save_sessions()
        await ctx.ok(f"`{rest[0]}` is now `{rest[1]}`.")
        return

    if verb in ("drop", "delete", "rm"):
        if not rest:
            await ctx.fail(f"Usage: `{p}convo drop <label>`")
            return
        if not store.drop(rest[0]):
            await ctx.fail(f"Could not drop `{rest[0]}` — unknown, or it is the last one left.")
            return
        ctx.state._sessions.pop(rest[0], None)
        save_sessions()
        await ctx.ok(f"Dropped `{rest[0]}`. Active is now `{store.active}`.")
        return

    # Anything else is a label to switch to.
    label = parts[0] if verb != "switch" else (rest[0] if rest else "")
    convo = store.switch(label)
    if convo is None:
        await ctx.fail(f"No conversation called `{label}`. `{p}convo` lists them.")
        return
    save_sessions()
    await ctx.ok(
        f"Switched to `{convo.label}` · `{convo.cwd}` · "
        f"{convo.turns} turns{'' if convo.session_id else ' (fresh)'}"
    )


@command(
    "model",
    "!model [opus|sonnet|haiku|fable|<id>]",
    "Show or change the model for the active conversation.",
    "Claude Code",
)
async def cmd_model(ctx: Ctx) -> None:
    convo = ctx.state.convo
    p = config.PREFIX
    if not ctx.rest:
        current = convo.model or config.CLAUDE_MODEL or "account default"
        await ctx.send(
            f"**Model:** `{current}` in `{convo.label}`\n"
            f"Options: {', '.join(f'`{m}`' for m in config.MODELS)}, or a full model ID.\n"
            f"_`{p}model default` clears the override._"
        )
        return

    choice = ctx.rest.strip()
    if choice.lower() in ("default", "clear", "reset"):
        choice = ""
    elif choice.lower() in config.MODELS:
        choice = choice.lower()
    # Anything else is passed through as a full model ID; the CLI validates it.

    if choice == convo.model:
        await ctx.ok(f"Already on `{choice or 'account default'}`.")
        return

    convo.model = choice
    ctx.state._sessions.pop(convo.label, None)
    save_sessions()

    note = ""
    if convo.session_id:
        # Model is part of the prompt cache key, so this is not free.
        note = (
            f"\n⚠️ Each model has its own cache, so the next prompt in `{convo.label}` "
            f"re-reads its whole history uncached. `{p}convo new` avoids that."
        )
    await ctx.ok(f"`{convo.label}` now uses `{choice or 'account default'}`.{note}")


@command(
    "effort",
    "!effort [low|medium|high|xhigh|max]",
    "Show or change reasoning effort for the active conversation.",
    "Claude Code",
)
async def cmd_effort(ctx: Ctx) -> None:
    convo = ctx.state.convo
    p = config.PREFIX
    if not ctx.rest:
        await ctx.send(
            f"**Effort:** `{convo.effort or config.CLAUDE_EFFORT}` in `{convo.label}`\n"
            f"Options: {', '.join(f'`{e}`' for e in config.EFFORTS)}\n"
            f"_Effort drives thinking tokens, which bill as output. `medium` suits "
            f"most chat prompts; raise it for hard multi-step work._"
        )
        return

    choice = ctx.rest.strip().lower()
    if choice not in config.EFFORTS:
        await ctx.fail(f"Pick one of: {', '.join(config.EFFORTS)}")
        return
    if choice == (convo.effort or config.CLAUDE_EFFORT):
        await ctx.ok(f"Already on `{choice}`.")
        return

    convo.effort = choice
    ctx.state._sessions.pop(convo.label, None)
    save_sessions()

    note = ""
    if convo.session_id:
        note = (
            f"\n⚠️ Effort is part of the cache key too — the next prompt in "
            f"`{convo.label}` re-reads its history uncached."
        )
    await ctx.ok(f"`{convo.label}` now runs at `{choice}` effort.{note}")


@command(
    "chrome",
    "!chrome [on|off]",
    "Show or toggle browser tools for the active conversation (off is cheaper).",
    "Claude Code",
    aliases=("browsertools",),
)
async def cmd_chrome(ctx: Ctx) -> None:
    convo = ctx.state.convo
    p = config.PREFIX
    mode = config.browser_mode()

    if mode == "mcp":
        await ctx.send(
            f"Browser tools come from the MCP server in `{config.MCP_CONFIG}`, which is "
            f"always loaded. `{p}chrome` only controls Claude in Chrome — remove "
            f"`mcp.json` to switch back to it."
        )
        return
    if not config.CLAUDE_CHROME:
        await ctx.send("Claude in Chrome is off globally (`CLAUDE_CHROME=false` in `.env`).")
        return

    if not ctx.rest:
        await ctx.send(
            f"**Browser tools:** `{'on' if convo.chrome else 'off'}` in `{convo.label}`\n"
            f"_Every browser tool definition rides in this conversation's cached prefix, "
            f"so leaving it off keeps non-browsing prompts smaller. "
            f"`{p}chrome on` when you need Claude to actually open a page._"
        )
        return

    choice = ctx.rest.strip().lower()
    if choice not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
        await ctx.fail(f"Usage: `{p}chrome on` or `{p}chrome off`")
        return
    wanted = choice in ("on", "true", "1", "yes")

    if wanted == convo.chrome:
        await ctx.ok(f"Browser tools are already `{'on' if wanted else 'off'}` here.")
        return

    convo.chrome = wanted
    ctx.state._sessions.pop(convo.label, None)
    save_sessions()

    note = ""
    if convo.session_id:
        # Tool definitions sit at the very front of the cached prefix, so
        # changing the toolset invalidates the whole conversation — same cost
        # shape as switching model or effort.
        note = (
            f"\n⚠️ The toolset is part of the cached prefix, so the next prompt in "
            f"`{convo.label}` re-reads its history uncached. `{p}convo new` avoids that."
        )
    await ctx.ok(f"Browser tools `{'on' if wanted else 'off'}` in `{convo.label}`.{note}")


@command("session", "!session [id]", "Show the Claude session ID, or resume a specific one.", "Claude Code", aliases=("resume",))
async def cmd_session(ctx: Ctx) -> None:
    convo = ctx.state.convo
    if ctx.rest:
        convo.session_id = ctx.rest.strip()
        ctx.state._sessions.pop(convo.label, None)
        save_sessions()
        await ctx.ok(f"`{convo.label}` will resume session `{convo.session_id}`.")
        return
    await ctx.send(
        f"**Conversation:** `{convo.label}`\n**Session:** `{convo.session_id}`\n"
        f"**Directory:** `{convo.cwd}`\n**Turns:** {convo.turns}"
        if convo.session_id
        else f"`{convo.label}` has no session yet — the next prompt creates one."
    )


# ------------------------------------------------------------------------ self-edit
#
# The bot edits its own source often enough that doing it by hand — !cd to the
# repo, prompt, !sh git commit, kill the tray, relaunch — is the slow path. These
# five commands are that loop, with the bits that are easy to get wrong (which
# directory, which interpreter, releasing the tray before exec) handled here.


async def _git(ctx: Ctx, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=config.SELF_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


@command(
    "self",
    "!self <what to change>",
    "Run Claude against the bot's own source in a dedicated conversation.",
    "Self",
    aliases=("edit",),
)
async def cmd_self(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail(f"Say what to change, e.g. `{config.PREFIX}self add a !uptime command`")
        return

    # A dedicated conversation keeps bot-surgery out of whatever else this
    # channel was working on, and keeps its history short and cheap.
    store = ctx.state.store
    if "self" not in store.convos:
        store.create("self", config.SELF_DIR)
    else:
        store.switch("self")
        store.convos["self"].cwd = config.SELF_DIR
    save_sessions()

    await ctx.send(
        f"🔧 Editing myself in `{config.SELF_DIR}` (conversation `self`).\n"
        f"_When it looks right: `{config.PREFIX}diff`, then `{config.PREFIX}commit`, "
        f"then `{config.PREFIX}relaunch`._"
    )
    await cmd_claude(ctx)


@command("diff", "!diff [path]", "Show uncommitted changes to the bot's source.", "Self")
async def cmd_diff(ctx: Ctx) -> None:
    args = ["diff", "--stat"] if not ctx.rest else ["diff", "--", ctx.rest.strip()]
    _, stat = await _git(ctx, *args)
    _, status = await _git(ctx, "status", "--short")
    body = f"$ git status --short\n{status or '(clean)'}\n\n$ git {' '.join(args)}\n{stat or '(no changes)'}"
    await ctx.long(body, lang="diff", filename="selfdiff.txt")


@command("log", "!log [n]", "Recent commits to the bot's source.", "Self", aliases=("commits",))
async def cmd_log(ctx: Ctx) -> None:
    count = ctx.rest.strip() if ctx.rest.strip().isdigit() else "10"
    _, out = await _git(ctx, "log", f"-{count}", "--oneline", "--decorate")
    await ctx.long(out or "(no commits)", lang="")


@command("commit", "!commit [message]", "Stage and commit the bot's current changes.", "Self")
async def cmd_commit(ctx: Ctx) -> None:
    _, status = await _git(ctx, "status", "--porcelain")
    if not status.strip():
        await ctx.fail("Nothing to commit — the working tree is clean.")
        return

    message = ctx.rest.strip() or "Update the bot from Discord"
    code, out = await _git(ctx, "add", "-A")
    if code != 0:
        await ctx.fail("`git add` failed:")
        await ctx.long(out, lang="")
        return
    code, out = await _git(ctx, "commit", "-m", message)
    if code != 0:
        await ctx.fail("`git commit` failed:")
        await ctx.long(out, lang="")
        return
    await ctx.ok(f"Committed.\n```\n{out.strip()[:1500]}\n```")


@command(
    "relaunch",
    "!relaunch",
    "Restart the bot process so source changes take effect.",
    "Self",
    # Deliberately NOT "restart": that is already an alias of !reboot, which
    # reboots the whole PC. Two very different blast radii, one word.
    aliases=("reload", "selfrestart"),
)
async def cmd_relaunch(ctx: Ctx) -> None:
    # Refuse to restart into a file that will not import — a syntax error here
    # takes the bot off Discord with no way to fix it remotely.
    entry = Path(config.SELF_DIR) / "startup.pyw"
    # Globbed, not listed: a hand-maintained list silently stops covering any
    # module Claude adds to the package while editing itself.
    sources = sorted(p.name for p in Path(config.SELF_DIR).glob("*.py"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "py_compile", *sources,
        cwd=config.SELF_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        await ctx.fail("Refusing to restart — the source does not compile:")
        await ctx.long(out.decode("utf-8", "replace"), lang="py", filename="syntax.txt")
        return

    await ctx.ok("Restarting… I will say hello again in a few seconds.")
    save_sessions()

    # Detach the replacement so it survives this process exiting, then tear the
    # tray down before quitting or the icon lingers as a ghost.
    subprocess.Popen(
        [sys.executable, str(entry)],
        cwd=config.SELF_DIR,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    await asyncio.sleep(1.0)
    UI.stop()
    await client.close()
    os._exit(0)


# ----------------------------------------------------------------------------- shell


@command("sh", "!sh <command>", "Run a PowerShell command.", "Shell", aliases=("ps", "pwsh"))
async def cmd_sh(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Give me a command.")
        return
    async with ctx.channel.typing():
        code, output = await run_shell(ctx.rest, ctx.state.cwd, powershell=True)
    await ctx.long(output or "(no output)", lang="powershell", filename="shell.txt")
    if code != 0:
        await ctx.send(f"_exit code {code}_")


@command("cmd", "!cmd <command>", "Run a legacy cmd.exe command.", "Shell")
async def cmd_cmd(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Give me a command.")
        return
    async with ctx.channel.typing():
        code, output = await run_shell(ctx.rest, ctx.state.cwd, powershell=False)
    await ctx.long(output or "(no output)", lang="", filename="cmd.txt")
    if code != 0:
        await ctx.send(f"_exit code {code}_")


# ------------------------------------------------------------------------- filesystem


@command("cd", "!cd <path>", "Change the working directory used by Claude and the shell.", "Files")
async def cmd_cd(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.send(f"`{ctx.state.cwd}`")
        return
    target = ctx.resolve(ctx.rest)
    if not target.is_dir():
        await ctx.fail(f"`{target}` is not a directory.")
        return
    ctx.state.cwd = str(target.resolve())
    ctx.state.session.cwd = ctx.state.cwd
    save_sessions()
    await ctx.ok(f"Now in `{ctx.state.cwd}`")


@command("pwd", "!pwd", "Show the working directory.", "Files")
async def cmd_pwd(ctx: Ctx) -> None:
    await ctx.send(f"`{ctx.state.cwd}`")


@command("ls", "!ls [path]", "List a directory.", "Files", aliases=("dir",))
async def cmd_ls(ctx: Ctx) -> None:
    target = ctx.resolve(ctx.rest) if ctx.rest else Path(ctx.state.cwd)
    if not target.is_dir():
        await ctx.fail(f"`{target}` is not a directory.")
        return

    def listing() -> str:
        rows = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                stat = entry.stat()
                size = "<DIR>" if entry.is_dir() else f"{stat.st_size:,}"
            except OSError:
                size = "?"
            rows.append(f"{size:>14}  {entry.name}")
        return "\n".join(rows) or "(empty)"

    await ctx.long(f"{target}\n\n{await asyncio.to_thread(listing)}", lang="", filename="listing.txt")


@command("cat", "!cat <path>", "Print a text file.", "Files", aliases=("read",))
async def cmd_cat(ctx: Ctx) -> None:
    target = ctx.resolve(ctx.rest)
    if not target.is_file():
        await ctx.fail(f"`{target}` is not a file.")
        return
    text = await asyncio.to_thread(target.read_text, "utf-8", "replace")
    suffix = target.suffix.lstrip(".")
    await ctx.long(text, lang=suffix if suffix.isalnum() else "", filename=target.name)


@command("get", "!get <path>", "Download a file from the PC into Discord.", "Files", aliases=("download",))
async def cmd_get(ctx: Ctx) -> None:
    target = ctx.resolve(ctx.rest)
    if not target.is_file():
        await ctx.fail(f"`{target}` is not a file.")
        return
    size = target.stat().st_size
    if size > config.MAX_UPLOAD_BYTES:
        await ctx.fail(f"`{target.name}` is {size / 1048576:.1f} MiB, over the upload limit.")
        return
    data = await asyncio.to_thread(target.read_bytes)
    await ctx.channel.send(f"📄 `{target}`", file=discord.File(io.BytesIO(data), filename=target.name))


@command("rm", "!rm <path>", "Delete a file (directories need `!sh Remove-Item -Recurse`).", "Files", aliases=("del",))
async def cmd_rm(ctx: Ctx) -> None:
    target = ctx.resolve(ctx.rest)
    if not target.exists():
        await ctx.fail(f"`{target}` does not exist.")
        return
    if target.is_dir():
        await ctx.fail("That is a directory — use `!sh Remove-Item -Recurse -Force <path>` if you mean it.")
        return
    await asyncio.to_thread(target.unlink)
    await ctx.ok(f"Deleted `{target}`")


@command("mkdir", "!mkdir <path>", "Create a directory (parents included).", "Files")
async def cmd_mkdir(ctx: Ctx) -> None:
    target = ctx.resolve(ctx.rest)
    await asyncio.to_thread(lambda: target.mkdir(parents=True, exist_ok=True))
    await ctx.ok(f"Created `{target}`")


async def save_attachments(ctx: Ctx, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    saved = []
    for attachment in ctx.message.attachments:
        path = destination / attachment.filename
        await attachment.save(path)
        saved.append(f"`{path}`  ({attachment.size:,} bytes)")
    await ctx.ok("Saved:\n" + "\n".join(saved))


@command("put", "!put [dir]", "Upload the attached file(s) to the PC (defaults to the working dir).", "Files", aliases=("upload",))
async def cmd_put(ctx: Ctx) -> None:
    if not ctx.message.attachments:
        await ctx.fail("Attach at least one file to the message.")
        return
    destination = ctx.resolve(ctx.rest) if ctx.rest else Path(ctx.state.cwd)
    await save_attachments(ctx, destination)


# ------------------------------------------------------------------------------ screen


@command("ss", "!ss", "Screenshot the primary monitor.", "Screen", aliases=("screenshot", "shot"))
async def cmd_ss(ctx: Ctx) -> None:
    async with ctx.channel.typing():
        data, ext, size = await asyncio.to_thread(screen.screenshot)
    await ctx.image(data, ext, f"🖥️ primary monitor · {size[0]}×{size[1]}")


@command("monitors", "!monitors", "Show the primary monitor's geometry.", "Screen")
async def cmd_monitors(ctx: Ctx) -> None:
    mon = await asyncio.to_thread(screen.primary_monitor)
    await ctx.long(
        f"primary — {mon['width']}×{mon['height']} at ({mon['left']}, {mon['top']})",
        lang="",
    )


@command("rec", "!rec [seconds] [fps]", "Record the primary monitor to an animated GIF (max 60s).", "Screen", aliases=("record", "gif"))
async def cmd_rec(ctx: Ctx) -> None:
    parts = ctx.args
    seconds = float(parts[0]) if parts and parts[0].replace(".", "", 1).isdigit() else 6.0
    fps = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 4.0

    notice = await ctx.send(f"🎥 Recording the primary monitor for {min(seconds, 60):.0f}s at {fps:g} fps…")
    data, frames = await asyncio.to_thread(screen.record_gif, seconds, fps)
    await notice.edit(content=f"🎥 Recorded {frames} frames ({len(data) / 1048576:.1f} MiB)")
    await ctx.channel.send(file=discord.File(io.BytesIO(data), filename=f"rec_{int(time.time())}.gif"))


@command("cam", "!cam [index]", "Take a webcam photo.", "Screen", aliases=("webcam",))
async def cmd_cam(ctx: Ctx) -> None:
    index = int(ctx.rest) if ctx.rest.isdigit() else 0
    async with ctx.channel.typing():
        data, ext = await asyncio.to_thread(screen.webcam, index)
    await ctx.image(data, ext, f"📷 Camera {index}")


@command("watch", "!watch [seconds]", "Stream a screenshot every N seconds until `!stop`.", "Screen")
async def cmd_watch(ctx: Ctx) -> None:
    if ctx.state.watch_task and not ctx.state.watch_task.done():
        await ctx.fail("Already watching — `!stop` first.")
        return
    interval = max(2.0, float(ctx.rest)) if ctx.rest.replace(".", "", 1).isdigit() else 5.0

    async def loop() -> None:
        try:
            while True:
                data, ext, _ = await asyncio.to_thread(screen.screenshot, 0.6)
                await ctx.channel.send(
                    file=discord.File(io.BytesIO(data), filename=f"watch.{ext}")
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await ctx.channel.send(f"❌ Watch stopped: {exc}")

    ctx.state.watch_task = asyncio.create_task(loop())
    await ctx.ok(f"Watching every {interval:g}s. `!stop` to end.")


@command("stop", "!stop", "Stop the `!watch` stream.", "Screen")
async def cmd_stop(ctx: Ctx) -> None:
    task = ctx.state.watch_task
    if task and not task.done():
        task.cancel()
        await ctx.ok("Stopped watching.")
    else:
        await ctx.fail("Not watching anything.")


# ------------------------------------------------------------------------------- input


@command("click", "!click [x y] [left|right|middle] [double]", "Click the mouse.", "Input", aliases=("cl",))
async def cmd_click(ctx: Ctx) -> None:
    parts = ctx.args
    coords = [p for p in parts if p.lstrip("-").isdigit()]
    words = [p.lower() for p in parts if not p.lstrip("-").isdigit()]
    button = next((w for w in words if w in ("left", "right", "middle")), "left")
    clicks = 2 if "double" in words else 1
    x = int(coords[0]) if len(coords) >= 2 else None
    y = int(coords[1]) if len(coords) >= 2 else None
    await asyncio.to_thread(sysctl.click, x, y, button, clicks)
    where = f"at ({x}, {y})" if x is not None else "at the cursor"
    await ctx.ok(f"{'Double-' if clicks == 2 else ''}{button} click {where}")


@command("move", "!move <x> <y>", "Move the mouse.", "Input", aliases=("mv",))
async def cmd_move(ctx: Ctx) -> None:
    parts = ctx.args
    if len(parts) < 2:
        await ctx.fail("Usage: `!move <x> <y>`")
        return
    await asyncio.to_thread(sysctl.move, int(parts[0]), int(parts[1]))
    await ctx.ok(f"Moved to ({parts[0]}, {parts[1]})")


@command("drag", "!drag <x1> <y1> <x2> <y2>", "Drag from one point to another.", "Input")
async def cmd_drag(ctx: Ctx) -> None:
    parts = ctx.args
    if len(parts) < 4:
        await ctx.fail("Usage: `!drag <x1> <y1> <x2> <y2>`")
        return
    nums = [int(p) for p in parts[:4]]
    await asyncio.to_thread(sysctl.drag, *nums)
    await ctx.ok(f"Dragged ({nums[0]}, {nums[1]}) → ({nums[2]}, {nums[3]})")


@command("scroll", "!scroll <amount> [x y]", "Scroll (positive is up).", "Input")
async def cmd_scroll(ctx: Ctx) -> None:
    parts = ctx.args
    if not parts:
        await ctx.fail("Usage: `!scroll <amount>` — e.g. `!scroll -500`")
        return
    x = int(parts[1]) if len(parts) > 2 else None
    y = int(parts[2]) if len(parts) > 2 else None
    await asyncio.to_thread(sysctl.scroll, int(parts[0]), x, y)
    await ctx.ok(f"Scrolled {parts[0]}")


@command("type", "!type <text>", "Type text at the current focus.", "Input", aliases=("write",))
async def cmd_type(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Give me something to type.")
        return
    await asyncio.to_thread(sysctl.type_text, ctx.rest)
    await ctx.ok(f"Typed {len(ctx.rest)} characters")


@command("key", "!key <combo>", "Press keys, e.g. `!key ctrl+s` or `!key win r` for a sequence.", "Input", aliases=("hotkey", "press"))
async def cmd_key(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Usage: `!key ctrl+shift+esc`")
        return
    pressed = await asyncio.to_thread(sysctl.press_keys, ctx.rest)
    await ctx.ok("Pressed " + ", ".join(f"`{p}`" for p in pressed))


@command("mouse", "!mouse", "Report the cursor position and screen size.", "Input")
async def cmd_mouse(ctx: Ctx) -> None:
    pos = await asyncio.to_thread(sysctl.cursor_position)
    size = await asyncio.to_thread(sysctl.screen_size)
    await ctx.send(f"Cursor at **({pos[0]}, {pos[1]})** · primary screen {size[0]}×{size[1]}")


# --------------------------------------------------------------------------- processes


@command("procs", "!procs [filter]", "List the heaviest processes.", "System", aliases=("tasks", "top"))
async def cmd_procs(ctx: Ctx) -> None:
    rows = await asyncio.to_thread(sysctl.list_processes, ctx.rest, 25)
    if not rows:
        await ctx.fail(f"No process matched {ctx.rest!r}.")
        return
    lines = [f"{'PID':>7}  {'RAM':>9}  NAME"]
    lines += [f"{r['pid']:>7}  {r['mem_mb']:>7.0f}MB  {r['name']}" for r in rows]
    await ctx.long("\n".join(lines), lang="")


@command("kill", "!kill <pid|name> [force]", "Terminate a process.", "System")
async def cmd_kill(ctx: Ctx) -> None:
    parts = ctx.args
    if not parts:
        await ctx.fail("Usage: `!kill notepad.exe` or `!kill 1234 force`")
        return
    force = len(parts) > 1 and parts[1].lower() in ("force", "-f", "--force")
    try:
        results = await asyncio.to_thread(sysctl.kill_process, parts[0], force)
    except Exception as exc:
        await ctx.fail(str(exc))
        return
    await ctx.send("\n".join(f"• {line}" for line in results))


@command("sys", "!sys", "CPU, RAM, disks, uptime, battery.", "System", aliases=("status", "info"))
async def cmd_sys(ctx: Ctx) -> None:
    info = await asyncio.to_thread(sysctl.system_info)
    lines = [f"**{key.title()}:** {value}" for key, value in info.items()]
    await ctx.send("\n".join(lines)[:1990])


@command("windows", "!windows", "List visible window titles.", "System", aliases=("wins",))
async def cmd_windows(ctx: Ctx) -> None:
    titles = await asyncio.to_thread(sysctl.list_windows)
    await ctx.long("\n".join(titles) or "(none)", lang="")


@command("focus", "!focus <title fragment>", "Bring a window to the foreground.", "System")
async def cmd_focus(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Usage: `!focus chrome`")
        return
    await ctx.send(await asyncio.to_thread(sysctl.focus_window, ctx.rest))


@command("open", "!open <app|file|url>", "Open an app, file, folder or URL.", "System", aliases=("start", "launch"))
async def cmd_open(ctx: Ctx) -> None:
    if not ctx.rest:
        await ctx.fail("Usage: `!open https://example.com` or `!open notepad`")
        return
    await ctx.ok(await asyncio.to_thread(sysctl.open_target, ctx.rest, ctx.state.cwd))


@command(
    "browser",
    "!browser [url]",
    "Open Chrome on the profile Claude browses with, so you can sign into sites.",
    "System",
    # No `chrome` alias: `!chrome` is the browser-tools toggle. One word, two
    # very different jobs, and the alias silently won.
    aliases=("signin",),
)
async def cmd_browser(ctx: Ctx) -> None:
    mode = config.browser_mode()
    if mode == "none":
        await ctx.fail(
            "No browser toolset is active. Either set `CLAUDE_CHROME=true` in `.env` "
            "(Claude drives your normal Chrome), or copy `mcp.json.example` to "
            "`mcp.json` for an isolated profile. Restart the bot after either."
        )
        return

    # In `chrome` mode Claude works through the extension in the browser you
    # already use, so there is no separate profile to open — an empty profile
    # dir means "your normal Chrome".
    profile = config.MCP_BROWSER_PROFILE if mode == "mcp" else ""
    try:
        note = await asyncio.to_thread(sysctl.launch_browser, profile, ctx.rest)
    except FileNotFoundError as exc:
        await ctx.fail(str(exc))
        return

    if mode == "mcp":
        await ctx.ok(f"{note}\nSign in here and Claude keeps the session. Close it before browsing.")
    else:
        await ctx.ok(f"{note}\nThis is the Chrome the extension drives — Claude sees these logins.")


@command("clip", "!clip [text]", "Read the clipboard, or set it.", "System", aliases=("clipboard",))
async def cmd_clip(ctx: Ctx) -> None:
    if ctx.rest:
        await asyncio.to_thread(sysctl.clipboard_set, ctx.rest)
        await ctx.ok(f"Clipboard set ({len(ctx.rest)} chars)")
    else:
        text = await asyncio.to_thread(sysctl.clipboard_get)
        await ctx.long(text or "(clipboard is empty)", lang="", filename="clipboard.txt")


@command("vol", "!vol <0-100|up|down|mute>", "Control system volume.", "System", aliases=("volume",))
async def cmd_vol(ctx: Ctx) -> None:
    arg = ctx.rest.lower().strip()
    if arg in ("mute", "m", ""):
        await asyncio.to_thread(sysctl.volume_mute_toggle)
        await ctx.ok("Toggled mute")
    elif arg in ("up", "+"):
        await asyncio.to_thread(sysctl.volume_step, 5)
        await ctx.ok("Volume up")
    elif arg in ("down", "-"):
        await asyncio.to_thread(sysctl.volume_step, -5)
        await ctx.ok("Volume down")
    elif arg.isdigit():
        level = await asyncio.to_thread(sysctl.volume_set, int(arg))
        await ctx.ok(f"Volume set to ~{level}%")
    else:
        await ctx.fail("Usage: `!vol 40`, `!vol up`, `!vol down`, `!vol mute`")


# ------------------------------------------------------------------------------- power


@command("lock", "!lock", "Lock the workstation.", "Power")
async def cmd_lock(ctx: Ctx) -> None:
    await ctx.ok("Locking…")
    await asyncio.to_thread(sysctl.lock_workstation)


@command("sleep", "!sleep", "Put the PC to sleep.", "Power")
async def cmd_sleep(ctx: Ctx) -> None:
    await ctx.ok("Going to sleep — you will lose the connection until it wakes.")
    await asyncio.to_thread(sysctl.sleep_pc)


@command("shutdown", "!shutdown [seconds]", "Schedule a shutdown (default 60s, `!abort` cancels).", "Power")
async def cmd_shutdown(ctx: Ctx) -> None:
    delay = int(ctx.rest) if ctx.rest.isdigit() else 60
    await ctx.send(await asyncio.to_thread(sysctl.shutdown, delay))


@command("reboot", "!reboot [seconds]", "Schedule a restart (default 60s).", "Power", aliases=("restart",))
async def cmd_reboot(ctx: Ctx) -> None:
    delay = int(ctx.rest) if ctx.rest.isdigit() else 60
    await ctx.send(await asyncio.to_thread(sysctl.reboot, delay))


@command("abort", "!abort", "Cancel a pending shutdown or restart.", "Power")
async def cmd_abort(ctx: Ctx) -> None:
    await ctx.send(await asyncio.to_thread(sysctl.abort_shutdown))


# -------------------------------------------------------------------------------- help


@command("help", "!help [command]", "Show this list.", "Meta", aliases=("h", "commands"))
async def cmd_help(ctx: Ctx) -> None:
    p = config.PREFIX
    if ctx.rest:
        key = ALIASES.get(ctx.rest.lower(), ctx.rest.lower())
        cmd = COMMANDS.get(key)
        if not cmd:
            await ctx.fail(f"No command called `{ctx.rest}`.")
            return
        alias_note = f"\nAliases: {', '.join(f'`{p}{a}`' for a in cmd.aliases)}" if cmd.aliases else ""
        await ctx.send(f"**{p}{cmd.name}**\n`{cmd.usage.replace('!', p, 1)}`\n{cmd.help}{alias_note}")
        return

    groups: dict[str, list[Command]] = {}
    for cmd in COMMANDS.values():
        groups.setdefault(cmd.group, []).append(cmd)

    embed = discord.Embed(
        title="PC Control",
        description=(
            f"Send any plain message to run it as a Claude Code prompt, or use `{p}` commands.\n"
            f"Working directory: `{ctx.state.cwd}`"
        ),
        colour=0xD97757,
    )
    ordered = ("Claude Code", "Self", "Shell", "Screen", "Input", "Files", "System", "Power", "Meta")
    # Any group not in `ordered` would otherwise vanish from !help entirely.
    for group in (*ordered, *sorted(set(groups) - set(ordered))):
        cmds = groups.get(group)
        if not cmds:
            continue
        rows = [f"`{c.usage.replace('!', p, 1)}` — {c.help}" for c in sorted(cmds, key=lambda c: c.name)]
        # Discord caps a field at 1024 characters. The Claude Code group was
        # already at 881, so the next command added to it would have been
        # silently cut off the bottom of the list; spill into a second field
        # instead of losing rows.
        chunk: list[str] = []
        size = 0
        part = 0
        for row in rows:
            if chunk and size + len(row) + 1 > 1024:
                part += 1
                embed.add_field(name=group if part == 1 else f"{group} (cont.)", value="\n".join(chunk), inline=False)
                chunk, size = [], 0
            chunk.append(row)
            size += len(row) + 1
        if chunk:
            part += 1
            embed.add_field(name=group if part == 1 else f"{group} (cont.)", value="\n".join(chunk), inline=False)
    await ctx.channel.send(embed=embed)


# ------------------------------------------------------------------------------ client


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    global LOOP, WINDOW_TASK
    LOOP = asyncio.get_running_loop()
    UI.task_finished("idle")
    print(f"[pccontrol] logged in as {client.user} (id {client.user.id})")
    print(f"[pccontrol] owner: {config.OWNER_ID} · prefix: {config.PREFIX}")
    print(f"[pccontrol] default working directory: {config.DEFAULT_WORKDIR}")
    print(f"[pccontrol] model: {config.CLAUDE_MODEL or 'account default'} · effort: {config.CLAUDE_EFFORT}")
    print(f"[pccontrol] stable cache prefix: {config.CLAUDE_STABLE_PREFIX}")
    print(
        f"[pccontrol] browser: {config.browser_mode()}"
        f" · default per conversation: {'on' if config.CLAUDE_CHROME_DEFAULT_ON else 'off'}"
    )

    owner = None
    try:
        owner = await client.fetch_user(config.OWNER_ID)
        await owner.send(
            f"🟢 **PC control online** on `{os.environ.get('COMPUTERNAME', 'this PC')}`.\n"
            f"Send a message to prompt Claude Code, or `{config.PREFIX}help` for commands."
        )
    except discord.HTTPException as exc:
        print(f"[pccontrol] could not DM the owner: {exc}")

    # on_ready fires again on every reconnect; only ever run one watcher.
    if config.WINDOW_WATCH and owner is not None and (WINDOW_TASK is None or WINDOW_TASK.done()):
        async def notify(text: str) -> None:
            try:
                await owner.send(text)
            except discord.HTTPException as exc:
                print(f"[pccontrol] could not DM the window reset: {exc}")

        WINDOW_TASK = asyncio.create_task(window.watch(notify))
        print("[pccontrol] watching the 5-hour usage window")


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.id == client.user.id:
        return
    if message.author.id != config.OWNER_ID:
        return  # hard owner lock: everyone else is ignored entirely

    content = (message.content or "").strip()

    # A bare attachment with no text saves into the working directory.
    if not content and message.attachments:
        ctx = Ctx(message, "put", "")
        await save_attachments(ctx, Path(ctx.state.cwd))
        return
    if not content:
        return

    if content.startswith(config.PREFIX):
        raw = content[len(config.PREFIX) :].strip()
        if not raw:
            return
        head, _, rest = raw.partition(" ")
        key = ALIASES.get(head.lower(), head.lower())
        cmd = COMMANDS.get(key)
        if cmd is None:
            await message.channel.send(
                f"❌ Unknown command `{head}` — try `{config.PREFIX}help`."
            )
            return
        ctx = Ctx(message, key, rest)
    elif config.BARE_MESSAGE_IS_PROMPT:
        ctx = Ctx(message, "claude", content)
        cmd = COMMANDS["claude"]
    else:
        return

    try:
        await cmd.handler(ctx)
    except asyncio.CancelledError:
        pass
    except Exception:
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        await message.channel.send(f"❌ `{cmd.name}` failed:")
        await send_long(message.channel, detail, lang="py", filename="traceback.txt")


async def _kill_active(reason: str) -> None:
    """Stop every running Claude turn, in whichever channel started it."""
    for state in list(STATES.values()):
        task = state.claude_task
        if task is None or task.done():
            continue
        task.cancel()
        session = state.running or state.session
        await session.cancel()
        if state.channel is None:
            continue
        try:
            await state.channel.send(f"🛑 Killed from {reason}.")
        except discord.HTTPException:
            pass


async def _shutdown() -> None:
    await _kill_active("the desktop")
    if WINDOW_TASK is not None and not WINDOW_TASK.done():
        WINDOW_TASK.cancel()
    save_sessions()
    UI.stop()
    await client.close()


def _from_ui(coro_factory) -> None:
    """Hop from a UI thread onto the asyncio loop; fall back to a hard exit."""
    loop = LOOP
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(lambda: asyncio.create_task(coro_factory()))
    else:
        # Not connected yet — nothing to unwind gracefully.
        UI.stop()
        os._exit(0)


def main() -> None:
    problems = config.validate()
    if problems:
        print("Configuration problems:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nCopy .env.example to .env and fill it in.")
        sys.exit(1)
    try:
        claude_runner.claude_path()
    except claude_runner.ClaudeNotFound as exc:
        print(f"Warning: {exc}\nEverything except Claude commands will still work.\n")

    # The bot is remote code execution on this machine; it must never run
    # without a visible way to see and stop it. No tray, no bot.
    try:
        UI.start(
            on_quit=lambda: _from_ui(_shutdown),
            on_kill=lambda: _from_ui(lambda: _kill_active("the desktop")),
        )
    except ui.TrayUnavailable as exc:
        print(f"Refusing to start without a tray icon: {exc}")
        print("Install the tray dependency with: python -m pip install -r requirements.txt")
        sys.exit(1)

    try:
        client.run(config.TOKEN, log_handler=None)
    finally:
        UI.stop()


if __name__ == "__main__":
    main()
