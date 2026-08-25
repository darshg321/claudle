"""Configuration loading for the PC control bot."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
STATE_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
OWNER_ID = _int("DISCORD_OWNER_ID", 0)
PREFIX = os.environ.get("COMMAND_PREFIX", "!").strip() or "!"

DEFAULT_WORKDIR = os.environ.get("DEFAULT_WORKDIR", "").strip() or str(Path.home())
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "").strip()
CLAUDE_TIMEOUT = _int("CLAUDE_TIMEOUT", 1800)

# "Claude in Chrome": the CLI talks to the Claude Code browser extension over
# native messaging, so Claude drives the Chrome you already use, with the
# sessions you are already signed into. Needs the extension installed and
# Chrome running; harmless if not, you just get no browser tools.
CLAUDE_CHROME = _bool("CLAUDE_CHROME", True)

# Extra tools handed to Claude via MCP — browser control lives here. Defaults to
# mcp.json beside this file when it exists. A path that does not exist is
# ignored rather than fatal, so the bot still runs with plain Claude.
_mcp = os.environ.get("MCP_CONFIG", "").strip() or str(ROOT / "mcp.json")
MCP_CONFIG = _mcp if Path(_mcp).is_file() else ""

SHELL_TIMEOUT = _int("SHELL_TIMEOUT", 180)
AUTO_SCREENSHOT = _bool("AUTO_SCREENSHOT", True)
BARE_MESSAGE_IS_PROMPT = _bool("BARE_MESSAGE_IS_PROMPT", True)

# Discord free-tier upload ceiling is 10 MiB; stay comfortably under it.
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_BYTES", 7_500_000)

SESSION_FILE = STATE_DIR / "sessions.json"


def browser_profile_dir() -> str:
    """The Chrome profile the MCP browser server drives, read back out of MCP_CONFIG.

    Parsed rather than configured separately so `!browser` can never open a
    different profile than the one Claude actually sees.
    """
    if not MCP_CONFIG:
        return ""
    try:
        data = json.loads(Path(MCP_CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for server in (data.get("mcpServers") or {}).values():
        for arg in server.get("args") or []:
            if isinstance(arg, str) and arg.startswith("--user-data-dir="):
                return arg.split("=", 1)[1]
    return ""


def validate() -> list[str]:
    problems = []
    if not TOKEN:
        problems.append("DISCORD_TOKEN is not set (put it in .env)")
    if not OWNER_ID:
        problems.append("DISCORD_OWNER_ID is not set (your numeric Discord user ID)")
    return problems
