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
import screen
import sysctl
import usage

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
    def decorator(func: Callable[["Ctx"], Awaitable[None]]):
        COMMANDS[name] = Command(name, func, usage, help_text, group, aliases)
        for alias in aliases:
            ALIASES[alias] = name
        return func

    return decorator


class ChannelState:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.session = claude_runner.ClaudeSession(cwd)
        self.claude_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None


STATES: dict[int, ChannelState] = {}


def state_for(channel_id: int) -> ChannelState:
    if channel_id not in STATES:
        STATES[channel_id] = ChannelState(config.DEFAULT_WORKDIR)
        saved = _load_sessions().get(str(channel_id))
        if saved:
            STATES[channel_id].cwd = saved.get("cwd", config.DEFAULT_WORKDIR)
            STATES[channel_id].session = claude_runner.ClaudeSession(
                STATES[channel_id].cwd, saved.get("session_id")
            )
    return STATES[channel_id]


def _load_sessions() -> dict:
    try:
        return json.loads(config.SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_sessions() -> None:
    data = {
        str(cid): {"session_id": st.session.session_id, "cwd": st.cwd}
        for cid, st in STATES.items()
    }
    try:
        config.SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------------ plumbing


class Ctx:
    """Everything a command handler needs."""

    def __init__(self, message: discord.Message, name: str, rest: str):
        self.message = message
        self.channel = message.channel
        self.name = name
        self.rest = rest.strip()
        self.state = state_for(message.channel.id)

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
    session = ctx.state.session
    session.cwd = ctx.state.cwd
    resuming = bool(session.session_id)

    header = (
        f"🧠 **Claude Code** · `{ctx.state.cwd}`\n"
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
        elif kind == "text":
            texts.append(event["text"])
            preview = " ".join(event["text"].split())[:140]
            status.add(f"💬 {preview}")
        elif kind == "tool":
            summary = event.get("summary", "")
            status.add(f"🔧 **{event['name']}** `{summary}`" if summary else f"🔧 **{event['name']}**")
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
    icon = "❌" if final.is_error else "✅"
    status.replace_header(f"{header}\n{icon} done · {' · '.join(footer_bits) or 'no stats'}")
    await status.flush(force=True)

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
    window = float(ctx.rest) if ctx.rest.replace(".", "", 1).isdigit() else 5.0
    window = max(0.1, min(window, 24 * 7))
    async with ctx.channel.typing():
        try:
            payload = await asyncio.to_thread(usage.fetch_limits)
        except usage.UsageError as exc:
            await ctx.fail(str(exc))
            return
        tokens = await asyncio.to_thread(usage.local_tokens, window)
    await ctx.long(usage.report(payload, tokens), lang="")


@command("cancel", "!cancel", "Stop the Claude run in progress.", "Claude Code", aliases=("stopclaude",))
async def cmd_cancel(ctx: Ctx) -> None:
    task = ctx.state.claude_task
    if task and not task.done():
        task.cancel()
        await ctx.state.session.cancel()
        await ctx.ok("Cancelled the running Claude turn.")
    else:
        await ctx.fail("Nothing is running here.")


@command("new", "!new", "Forget the Claude conversation and start fresh.", "Claude Code", aliases=("reset",))
async def cmd_new(ctx: Ctx) -> None:
    ctx.state.session = claude_runner.ClaudeSession(ctx.state.cwd)
    save_sessions()
    await ctx.ok("Started a new Claude session.")


@command("session", "!session [id]", "Show the Claude session ID, or resume a specific one.", "Claude Code", aliases=("resume",))
async def cmd_session(ctx: Ctx) -> None:
    if ctx.rest:
        ctx.state.session = claude_runner.ClaudeSession(ctx.state.cwd, ctx.rest.strip())
        save_sessions()
        await ctx.ok(f"Next prompt resumes session `{ctx.rest.strip()}`.")
        return
    sid = ctx.state.session.session_id
    await ctx.send(
        f"**Session:** `{sid}`\n**Directory:** `{ctx.state.cwd}`" if sid
        else f"No session yet in `{ctx.state.cwd}` — the next prompt creates one."
    )


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
    aliases=("chrome",),
)
async def cmd_browser(ctx: Ctx) -> None:
    profile = config.browser_profile_dir()
    if not profile:
        await ctx.fail(
            "No browser profile configured. Copy `mcp.json.example` to `mcp.json`, "
            "set `--user-data-dir=`, and restart the bot."
        )
        return
    try:
        note = await asyncio.to_thread(sysctl.launch_browser, profile, ctx.rest)
    except FileNotFoundError as exc:
        await ctx.fail(str(exc))
        return
    await ctx.ok(f"{note}\nSign in here and Claude keeps the session. Close it before browsing.")


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
    for group in ("Claude Code", "Shell", "Screen", "Input", "Files", "System", "Power", "Meta"):
        cmds = groups.get(group)
        if not cmds:
            continue
        body = "\n".join(f"`{c.usage.replace('!', p, 1)}` — {c.help}" for c in sorted(cmds, key=lambda c: c.name))
        embed.add_field(name=group, value=body[:1024], inline=False)
    await ctx.channel.send(embed=embed)


# ------------------------------------------------------------------------------ client


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready() -> None:
    print(f"[pccontrol] logged in as {client.user} (id {client.user.id})")
    print(f"[pccontrol] owner: {config.OWNER_ID} · prefix: {config.PREFIX}")
    print(f"[pccontrol] default working directory: {config.DEFAULT_WORKDIR}")
    try:
        owner = await client.fetch_user(config.OWNER_ID)
        await owner.send(
            f"🟢 **PC control online** on `{os.environ.get('COMPUTERNAME', 'this PC')}`.\n"
            f"Send a message to prompt Claude Code, or `{config.PREFIX}help` for commands."
        )
    except discord.HTTPException as exc:
        print(f"[pccontrol] could not DM the owner: {exc}")


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

    client.run(config.TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
