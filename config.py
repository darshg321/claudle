"""Configuration loading for the PC control bot."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
STATE_DIR.mkdir(exist_ok=True)


# Environment variables that mean "you are running inside a Claude Code turn".
# The bot must never inherit these: they belong to whatever session launched it,
# not to the sessions it spawns. See clean_child_env.
INHERITED_CLAUDE_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_PID",
)


def _load_dotenv() -> None:
    """Load .env, letting the file win over anything already in the environment.

    This used to be setdefault, i.e. the ambient environment won. That is the
    wrong way round for a bot that is regularly launched *from* a Claude Code
    session (a terminal inside one, or `!self` → `!relaunch`): a session that
    exports CLAUDE_EFFORT=high hands the bot an effort level its own .env never
    asked for, and the bot then quietly bills every prompt at that level. The
    .env beside bot.py is this program's configuration; nothing upstream of it
    gets a silent vote.
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_dotenv()


def clean_child_env() -> dict:
    """A copy of the environment safe to hand a `claude` subprocess.

    Strips the markers that identify a *parent* Claude Code session. Left in,
    the CLI the bot spawns can believe it is a nested child of whatever started
    the bot, and inherited CLAUDE_MODEL/CLAUDE_EFFORT would fight the flags the
    bot passes explicitly. Auth (ANTHROPIC_API_KEY, the OAuth store) is
    untouched — only session identity and settings the bot sets itself go.
    """
    env = dict(os.environ)
    for name in (*INHERITED_CLAUDE_VARS, "CLAUDE_MODEL", "CLAUDE_EFFORT"):
        env.pop(name, None)
    return env


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

# Claude Code's own default is xhigh, tuned for long autonomous coding runs.
# Most messages sent to a chat bot are not that, and effort drives thinking
# tokens, which bill as output. `medium` is the better default here; !effort
# raises it per conversation when a task actually needs it.
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "").strip() or "medium"
CLAUDE_TIMEOUT = _int("CLAUDE_TIMEOUT", 1800)

# Models offered by `!model`. Aliases resolve to the newest model in the family,
# so this list does not go stale when a new release lands.
MODELS = ("opus", "sonnet", "haiku", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Every prompt spawns a fresh `claude -p --resume`, which Claude Code treats as a
# *sequential session*. Its system prompt embeds the cwd, platform, shell, OS
# version, memory paths and a git status snapshot (branch + recent commits), so
# any of those changing between two prompts invalidates the cached prefix and
# re-reads the whole conversation at full price. This flag moves those sections
# into the first user message — which is replayed verbatim on resume — leaving a
# byte-identical system prompt across invocations. Measured on this repo: ~50%
# less cache re-creation on a resume that followed a commit.
CLAUDE_STABLE_PREFIX = _bool("CLAUDE_STABLE_PREFIX", True)

# Hard ceiling per turn, in dollars. A runaway agentic loop is the one failure
# mode that can spend a whole 5-hour window on a single prompt. 0 disables.
CLAUDE_MAX_BUDGET_USD = float(os.environ.get("CLAUDE_MAX_BUDGET_USD", "0").strip() or 0)

# "Claude in Chrome": the CLI talks to the Claude Code browser extension over
# native messaging, so Claude drives the Chrome you already use, with the
# sessions you are already signed into. Needs the extension installed and
# Chrome running; harmless if not, you just get no browser tools.
CLAUDE_CHROME = _bool("CLAUDE_CHROME", True)

# Browser tools are the largest toolset the bot can hand Claude, and every tool
# definition sits in the cached prefix of *every* request in a conversation —
# paid in full on the first turn and on any cache miss. Most prompts never
# browse, so this is opt-out per conversation via `!chrome`.
CLAUDE_CHROME_DEFAULT_ON = _bool("CLAUDE_CHROME_DEFAULT_ON", False)

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
WINDOW_FILE = STATE_DIR / "window.json"

# --------------------------------------------------------------- usage windows

# Watch the subscription's rolling 5-hour window and DM when it resets.
WINDOW_WATCH = _bool("WINDOW_WATCH", True)

# On reset, fire one tiny prompt to anchor the next window at a known time.
# The window is rolling and starts at your *first* request, so an unanchored
# window drifts to whenever you happen to next be at the keyboard. Anchoring
# costs ~1.3k cached tokens on Haiku; the window is shared across models, so a
# Haiku anchor opens the same window Opus later draws on.
WINDOW_WARM = _bool("WINDOW_WARM", True)
WINDOW_WARM_MODEL = os.environ.get("WINDOW_WARM_MODEL", "").strip() or "haiku"
WINDOW_POLL_SECONDS = _int("WINDOW_POLL_SECONDS", 300)

# The bot's own source tree — where `!self` and `!commit` operate.
SELF_DIR = str(ROOT)


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


# ------------------------------------------------------------ one browser, not two
#
# `--chrome` (the extension, driving the Chrome you already use) and a
# chrome-devtools MCP server (driving an isolated profile) are two *complete*
# and overlapping browser toolsets. Enabling both doubles the browser tool
# definitions in every request's cached prefix and leaves Claude guessing which
# set to reach for — the README warns about it, but nothing used to enforce it.
#
# An MCP browser wins when one is configured, because dropping an mcp.json next
# to the bot is a deliberate act; otherwise the extension stands.

MCP_BROWSER_PROFILE = browser_profile_dir()

if MCP_BROWSER_PROFILE and CLAUDE_CHROME:
    CLAUDE_CHROME = False


def browser_mode() -> str:
    """Which browser toolset Claude actually gets: 'mcp', 'chrome' or 'none'."""
    if MCP_BROWSER_PROFILE:
        return "mcp"
    if CLAUDE_CHROME:
        return "chrome"
    return "none"


def validate() -> list[str]:
    problems = []
    if not TOKEN:
        problems.append("DISCORD_TOKEN is not set (put it in .env)")
    if not OWNER_ID:
        problems.append("DISCORD_OWNER_ID is not set (your numeric Discord user ID)")
    return problems
