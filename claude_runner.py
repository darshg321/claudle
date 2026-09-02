"""Drive the Claude Code CLI in headless streaming mode."""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import config

EventCallback = Callable[[dict], Awaitable[None]]


class ClaudeNotFound(RuntimeError):
    pass


def claude_path() -> str:
    found = shutil.which("claude")
    if not found:
        raise ClaudeNotFound(
            "The `claude` CLI is not on PATH. Install it with "
            "`npm install -g @anthropic-ai/claude-code`."
        )
    return found


@dataclass
class RunResult:
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    subtype: str = ""
    num_turns: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    tools_used: list[str] = field(default_factory=list)
    stderr: str = ""
    cancelled: bool = False
    returncode: int | None = None
    # Cache accounting. A healthy resumed turn is nearly all cache_read; a large
    # cache_write means something invalidated the prefix and the whole history
    # was reprocessed. `!usage` surfaces the ratio so a regression is visible.
    cache_read: int = 0
    cache_write: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _summarize_tool(name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            single_line = " ".join(value.split())
            return single_line[:160] + ("…" if len(single_line) > 160 else "")
    try:
        blob = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""
    return blob[:120] + ("…" if len(blob) > 120 else "")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


class ClaudeSession:
    """One Claude Code conversation, resumable across bot restarts.

    `model` and `effort` are per-conversation overrides. Both are part of the
    prompt cache key, so changing either mid-conversation costs a full uncached
    re-read of the history — `!model` and `!effort` say so before they switch.
    """

    def __init__(
        self,
        cwd: str,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        chrome: bool = False,
    ):
        self.cwd = cwd
        self.session_id = session_id
        self.model = model or config.CLAUDE_MODEL
        self.effort = effort or config.CLAUDE_EFFORT
        # Browser tools are only ever offered through one channel; the caller has
        # already reconciled `--chrome` against any MCP browser server.
        self.chrome = chrome
        self._process: asyncio.subprocess.Process | None = None

    @property
    def busy(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def cancel(self) -> bool:
        proc = self._process
        if proc is None or proc.returncode is not None:
            return False
        try:
            proc.kill()
        except ProcessLookupError:
            return False
        return True

    def _build_args(self, resume: bool) -> list[str]:
        args = [
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            config.CLAUDE_PERMISSION_MODE,
        ]
        if config.CLAUDE_PERMISSION_MODE == "bypassPermissions":
            args.append("--dangerously-skip-permissions")
        args.append("--chrome" if self.chrome else "--no-chrome")
        if config.CLAUDE_STABLE_PREFIX:
            args.append("--exclude-dynamic-system-prompt-sections")
        if config.MCP_CONFIG:
            args += ["--mcp-config", config.MCP_CONFIG]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if config.CLAUDE_MAX_BUDGET_USD > 0:
            args += ["--max-budget-usd", str(config.CLAUDE_MAX_BUDGET_USD)]
        if resume and self.session_id:
            args += ["--resume", self.session_id]
        return args

    async def run(self, prompt: str, on_event: EventCallback) -> RunResult:
        """Run one turn. The prompt goes over stdin so quoting can never bite us."""
        exe = claude_path()
        result = RunResult(session_id=self.session_id)

        async def attempt(resume: bool) -> RunResult:
            args = self._build_args(resume)
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe",
                "/c",
                exe,
                *args,
                cwd=self.cwd,
                env=config.clean_child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
            )
            self._process = proc
            run = RunResult(session_id=self.session_id)

            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            stderr_chunks: list[bytes] = []

            async def drain_stderr() -> None:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            stderr_task = asyncio.create_task(drain_stderr())

            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(event, run, on_event)
            except asyncio.CancelledError:
                await self.cancel()
                run.cancelled = True
                raise
            finally:
                await proc.wait()
                await stderr_task
                self._process = None

            run.returncode = proc.returncode
            run.stderr = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
            return run

        wants_resume = bool(self.session_id)
        try:
            result = await asyncio.wait_for(attempt(wants_resume), timeout=config.CLAUDE_TIMEOUT)
        except asyncio.TimeoutError:
            await self.cancel()
            result = RunResult(session_id=self.session_id, is_error=True, cancelled=True)
            result.stderr = f"Claude exceeded CLAUDE_TIMEOUT ({config.CLAUDE_TIMEOUT}s) and was stopped."
            return result

        # A stale session ID (e.g. history pruned) makes --resume fail; retry fresh once.
        if wants_resume and result.returncode not in (0, None) and not result.text:
            if "resume" in result.stderr.lower() or "session" in result.stderr.lower():
                await on_event({"kind": "notice", "text": "Previous session expired — starting a fresh one."})
                self.session_id = None
                result = await attempt(False)

        if result.session_id:
            self.session_id = result.session_id
        return result

    async def _handle_event(self, event: dict, run: RunResult, on_event: EventCallback) -> None:
        etype = event.get("type")

        if sid := event.get("session_id"):
            run.session_id = sid
            if self.session_id != sid:
                self.session_id = sid
                await on_event({"kind": "session", "session_id": sid})

        if etype == "system" and event.get("subtype") == "init":
            await on_event(
                {
                    "kind": "init",
                    "model": event.get("model", ""),
                    "cwd": event.get("cwd", self.cwd),
                    "tools": event.get("tools", []),
                }
            )
            return

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        await on_event({"kind": "text", "text": text})
                elif btype == "thinking":
                    await on_event({"kind": "thinking"})
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    run.tools_used.append(name)
                    await on_event(
                        {
                            "kind": "tool",
                            "name": name,
                            "summary": _summarize_tool(name, block.get("input", {})),
                        }
                    )
            return

        if etype == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    body = _text_of(block.get("content"))
                    await on_event(
                        {
                            "kind": "tool_result",
                            "is_error": bool(block.get("is_error")),
                            "preview": " ".join(body.split())[:160],
                        }
                    )
            return

        if etype == "result":
            run.subtype = event.get("subtype", "")
            run.is_error = bool(event.get("is_error")) or run.subtype not in ("success", "")
            run.text = event.get("result") or ""
            run.num_turns = int(event.get("num_turns") or 0)
            run.duration_ms = int(event.get("duration_ms") or 0)
            run.cost_usd = float(event.get("total_cost_usd") or 0.0)
            usage = event.get("usage") or {}
            run.cache_read = int(usage.get("cache_read_input_tokens") or 0)
            run.cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            run.input_tokens = int(usage.get("input_tokens") or 0)
            run.output_tokens = int(usage.get("output_tokens") or 0)
            await on_event({"kind": "result", "result": run})
