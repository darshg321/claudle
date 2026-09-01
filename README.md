# Claudle

A Discord bot you DM to control your Windows PC. Send it a message and it runs
that message as a **Claude Code** prompt in a persistent session, streaming tool
calls back to you live and posting a screenshot when it's done. It also exposes
direct commands for the shell, screen, mouse/keyboard, files, processes and power.

Only one Discord account — the ID you put in `.env` — can talk to it. Everyone
else is ignored before a single byte is parsed.

---

## Setup

### Requirements

- **Windows.** Screen capture, input control, windows and power all use Windows
  APIs. The Discord and Claude parts are portable; the rest is not.
- **Python 3.10+** on your PATH (`python --version`). 3.10 is the floor — the
  code uses `X | Y` type syntax.
- **Claude Code CLI**, for the `!claude` commands only:
  ```powershell
  npm install -g @anthropic-ai/claude-code
  claude          # run once and log in
  ```
  The bot shells out to `claude` and reuses whatever login it already has, so
  there is no API key to configure here. Everything except the Claude commands
  works fine without it.

### 1. Get the code

```powershell
git clone https://github.com/<your-username>/claudle.git
cd claudle
python -m pip install -r requirements.txt
```

### 2. Create the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is your `DISCORD_TOKEN`, and
   it is shown exactly once.
3. On the same tab, scroll to **Privileged Gateway Intents** and enable
   **MESSAGE CONTENT INTENT**. The bot cannot read your messages without it.
4. **Installation** (or **OAuth2 → URL Generator**) → scope `bot` → invite it to
   any server, even an empty one you make yourself.
   Discord only lets a bot DM you if you share a server with it.

### 3. Get your user ID

Discord → **Settings → Advanced → Developer Mode** on. Then right-click your own
name anywhere → **Copy User ID**. This is your `DISCORD_OWNER_ID` — a 17–19
digit number, not your username.

### 4. Configure

```powershell
copy .env.example .env
notepad .env
```

Two keys are required; everything else has a working default:

| Key | What goes in it |
|---|---|
| `DISCORD_TOKEN` | The bot token from step 2. |
| `DISCORD_OWNER_ID` | Your numeric user ID from step 3. |

Set `DEFAULT_WORKDIR` too if you want Claude to start somewhere other than your
home directory. See [Configuration](#configuration) for the rest.

### 5. Run it

```powershell
python bot.py
```

Or double-click `run.bat`. When it connects it DMs you "PC control online".
Leave the window open — closing it kills remote access.

### 6. Check it works

DM the bot `!ss`. You should get a screenshot of your desktop back. If that
works, try a bare message like `what version of python is installed?` to
exercise the Claude path.

### 7. Optional: start it with Windows

```powershell
powershell -ExecutionPolicy Bypass -File install-startup.ps1
```

This puts a `Claudle` shortcut in your Startup folder and registers it as
**disabled**, so it appears in **Task Manager > Startup apps** but does not run
until you select it and click **Enable**. Since the bot is remote control of
this machine, off is the only sane default — turning it on is a deliberate
click.

Once enabled it launches at sign-in through `pythonw.exe`, so there is no
console window; the tray icon is still your way to see and stop it. Anything it
would have printed goes to `startup.log` in this folder — check there first if
the tray icon never appears.

The script and Task Manager write the same registry state, so either can flip
it:

```powershell
.\install-startup.ps1 -Action Status      # installed? enabled or disabled?
.\install-startup.ps1 -Action Enable      # same as Task Manager's Enable
.\install-startup.ps1 -Action Disable
.\install-startup.ps1 -Action Uninstall   # remove the shortcut entirely
```

Everything is per-user under `HKCU` — no admin rights, nothing machine-wide.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Bot starts but never DMs you | You don't share a server with it. Redo step 2.4. |
| Bot ignores every message | `DISCORD_OWNER_ID` doesn't match your account, or you set it to your username instead of your numeric ID. |
| Bot sees messages but they're empty | **MESSAGE CONTENT INTENT** is off. Step 2.3. |
| `DISCORD_TOKEN is not set` on startup | No `.env`, or you edited `.env.example` instead of a copy named `.env`. |
| `!claude` fails, other commands work | `claude` isn't on your PATH or isn't logged in. Run `claude` in a terminal once. |
| Turn hangs, then times out | `CLAUDE_PERMISSION_MODE` is set to a mode that prompts, and you can't answer prompts over Discord. Use `bypassPermissions`. |

---

## Using it

DM the bot. Anything **not** starting with `!` is sent straight to Claude Code:

```
refactor the auth module in this repo and run the tests
```

You'll see a single message updating live with each tool call
(`🔧 Bash ls -la`, `🔧 Edit src/auth.py`, …), then Claude's final answer, then a
screenshot of your desktop.

The conversation persists per Discord channel — follow-ups resume the same
session, and it survives a bot restart. `!new` starts over.

### Commands

Run `!help` for the live list, or `!help <command>` for details.

**Claude Code**
| | |
|---|---|
| `!claude <prompt>` | Run a prompt (aliases `!c`, `!ask`). Any bare message does the same. |
| `!convo` | List named conversations (aliases `!chat`, `!convos`). |
| `!convo <label>` | Switch to one. |
| `!convo new <label> [dir]` | Start a new one, optionally in another directory. |
| `!convo rename <a> <b>` / `!convo drop <label>` | Housekeeping. |
| `!new` | Wipe the active conversation's history, keeping its label. |
| `!model [alias]` | Show or set the model for this conversation. |
| `!effort [level]` | Show or set reasoning effort for this conversation. |
| `!session [id]` | Show the session ID, or resume a specific one. |
| `!window [anchor]` | 5-hour window status, or open a new one now. |
| `!cancel` | Kill the turn in progress. |
| `!usage [hours]` | Plan limits and tokens spent locally (aliases `!limits`, `!quota`). |

**Self-editing**
| | |
|---|---|
| `!self <what to change>` | Run Claude against the bot's own source (alias `!edit`). |
| `!diff [path]` | Uncommitted changes to the bot. |
| `!log [n]` | Recent commits. |
| `!commit [message]` | Stage everything and commit. |
| `!relaunch` | Restart the bot process (alias `!reload`). |

> `!relaunch` restarts **the bot**. `!restart` is an alias of `!reboot` and
> restarts **the PC** — the two were deliberately not merged.

`!relaunch` refuses to restart if the source does not compile, so a syntax error
introduced by `!self` cannot take the bot off Discord with no way back in.

Anthropic reports subscription limits as a **percentage of a rolling window**
(5-hour and 7-day), never as a token quota — so `!usage` shows those percentages
with their reset times, plus extra-credit spend, plus the token counts this
machine actually consumed (read from the local session transcripts). There is no
"tokens remaining" number to report. The optional `hours` argument changes only
the local token window; it does not affect the plan percentages.

**Shell**
| | |
|---|---|
| `!sh <command>` | PowerShell, run in the working directory. |
| `!cmd <command>` | Legacy `cmd.exe`. |

**Screen**
| | |
|---|---|
| `!ss` | Screenshot the primary monitor. |
| `!monitors` | Show the primary monitor's geometry. |
| `!rec [secs] [fps]` | Record the primary monitor to an animated GIF (max 60s). |
| `!cam [index]` | Webcam still. |
| `!watch [secs]` / `!stop` | Stream a primary-monitor screenshot every N seconds until stopped. |

All screen capture is limited to the primary monitor — secondary displays are never captured.

**Input**
| | |
|---|---|
| `!click [x y] [left\|right\|middle] [double]` | Click, optionally at a point. |
| `!move <x> <y>` · `!drag <x1> <y1> <x2> <y2>` · `!scroll <amount>` | Move / drag / scroll. |
| `!type <text>` | Type into whatever has focus. |
| `!key <combo>` | `!key ctrl+s`, `!key alt+tab`; space-separated for a sequence (`!key win r`). |
| `!mouse` | Cursor position and screen size. |

**Files** — all paths resolve against the working directory
| | |
|---|---|
| `!cd <path>` · `!pwd` · `!ls [path]` | Navigate. Also sets Claude's working directory. |
| `!cat <path>` | Print a text file. |
| `!get <path>` | Download a file into Discord. |
| `!put [dir]` | Upload attachments to the PC. Attaching a file with no text does the same. |
| `!rm <path>` · `!mkdir <path>` | Delete a file / create a directory. |

**System**
| | |
|---|---|
| `!sys` | CPU, RAM, disks, uptime, battery. |
| `!procs [filter]` · `!kill <pid\|name> [force]` | Process list and termination. |
| `!windows` · `!focus <title>` | List windows / bring one to the front. |
| `!open <app\|file\|url>` | Open anything the way Explorer would. |
| `!browser [url]` | Open Chrome on the isolated automation profile, if you use one (alias `!chrome`). |
| `!clip [text]` | Read or set the clipboard. |
| `!vol <0-100\|up\|down\|mute>` | System volume. |

**Power**
| | |
|---|---|
| `!lock` · `!sleep` | Lock or suspend. |
| `!shutdown [secs]` · `!reboot [secs]` · `!abort` | Default 60s delay; `!abort` cancels. |

---

## While it's running

The bot is remote code execution on your machine, so it refuses to run
invisibly. Two pieces of local UI make that concrete.

**Tray icon.** Present the whole time the bot is up — blurple when idle, amber
while a prompt is running, with the current state in the tooltip. Right-click
gives you **Kill current task** and **Quit**. If the tray can't be created the
bot exits instead of starting; there is no configuration flag to turn this off,
because a headless copy of this bot is exactly the thing you don't want running.

> Windows hides new tray icons in the overflow (`^`) by default. Drag it onto
> the taskbar once, or **Settings → Personalisation → Taskbar → Other system
> tray icons**, so it's actually visible.

**Task HUD.** While a prompt runs, a small panel appears in the bottom-right
corner showing the tool call in flight — the same stream you see in Discord. It
has one button, **Kill**, which cancels the turn and kills the `claude` process
immediately, the same as `!cancel` but without needing your phone. It hides
itself the moment the turn ends.

Both act on whichever channel currently owns a turn, so the Kill button always
targets the thing you can see running.

---

## Browser control

On by default. Claude drives **the Chrome you already use**, with the sessions
you are already signed into, so prompts like this just work:

```
check my aliexpress cart and tell me what's in it
```

No per-site setup, no separate profile, no logging in again. If you're signed
into a site in Chrome, Claude can use it.

This is Claude Code's **Claude in Chrome** integration. The CLI talks over
native messaging to a Chrome extension running inside your normal browser, so
there is no remote-debugging port and nothing is copied out of your profile —
the extension is simply already inside it. The bot passes `--chrome` on every
turn; set `CLAUDE_CHROME=false` to turn it off.

### Setup

Run `claude` in a terminal, use `/chrome`, and follow the prompt to install the
extension. That's the whole thing — the bot picks it up automatically, because
it's a property of the CLI rather than of this repo. Chrome must be running.

### How it behaves

Claude works in **its own tab group**, created on first use. It won't read or
disturb the tabs you already have open; it opens its own and works there. You'll
see tool calls stream past as `🔧 mcp__claude-in-chrome__navigate` and
`🔧 mcp__claude-in-chrome__get_page_text`.

Because it reads page text rather than only screenshots, it's accurate and cheap
compared with driving the screen through `!click`.

> **This is the widest permission in the whole bot.** Every site you are signed
> into is reachable by any prompt you send — mail, bank, shopping, GitHub, the
> lot. Combined with the default `bypassPermissions`, Claude will not ask before
> clicking something on a page. Read the [Security](#security--read-this)
> section before you use this from your phone, and be specific in prompts that
> touch anything transactional.

### Alternative: an isolated profile

If you'd rather Claude *not* see your everyday logins, `mcp.json.example`
configures [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp)
against a dedicated Chrome profile instead. Copy it to `mcp.json`, point
`--user-data-dir` at a path you own, set `CLAUDE_CHROME=false`, and use
`!browser` to sign into just the sites you want reachable.

That route can't use your main profile: since Chrome 136, remote debugging is
ignored on the default `--user-data-dir`, which is the fix for a real
cookie-theft technique. Use one or the other — running both gives Claude two
browser toolsets and it will pick badly.

---

## Configuration

All optional settings live in `.env` — see `.env.example` for the annotated list.
The ones worth knowing:

| Key | Default | Effect |
|---|---|---|
| `DEFAULT_WORKDIR` | your home dir | Where Claude and the shell start. |
| `CLAUDE_MODEL` | your CLI default | `opus`, `sonnet`, `haiku`, `fable`, or a full model ID. `!model` overrides per conversation. |
| `CLAUDE_EFFORT` | `medium` | `low` … `max`. Effort drives thinking tokens, which bill as output. `!effort` overrides per conversation. |
| `CLAUDE_STABLE_PREFIX` | `true` | Keep the system prompt byte-identical between invocations. See [Token usage](#token-usage). |
| `CLAUDE_MAX_BUDGET_USD` | `0` (off) | Hard per-turn spend ceiling. |
| `WINDOW_WATCH` | `true` | DM when the 5-hour usage window resets. |
| `WINDOW_WARM` | `true` | Anchor the next window with a tiny probe on reset. |
| `CLAUDE_PERMISSION_MODE` | `bypassPermissions` | Set to `acceptEdits` or `plan` for a tighter leash. |
| `AUTO_SCREENSHOT` | `true` | Screenshot after every successful Claude turn. |
| `BARE_MESSAGE_IS_PROMPT` | `true` | Set `false` to require the `!claude` prefix. |
| `CLAUDE_TIMEOUT` | `1800` | Seconds before a Claude turn is force-stopped. |
| `CLAUDE_CHROME` | `true` | Let Claude drive your real Chrome. See [Browser control](#browser-control). |
| `MCP_CONFIG` | `mcp.json` beside `bot.py` | Extra MCP servers handed to Claude. |

---

## Token usage

A chat bot is an unusually bad shape for prompt caching, and it is worth knowing
why before tuning anything else.

Claude Code re-sends the whole conversation on every request and relies on the
API's prefix cache to make that cheap. The match is exact: **a change anywhere in
the prefix reprocesses everything after it.** Interactively that is fine, because
one long-lived process holds one stable prefix. This bot instead spawns a fresh
`claude -p --resume` per Discord message, which Claude Code treats as a
*sequential session* — and a sequential session only reuses the prefix if the
system prompt still matches byte for byte.

That system prompt embeds the working directory, platform, shell, OS version,
memory paths, **and a git status snapshot (branch + recent commits)**. So a
commit, a branch switch, or a `!cd` between two prompts invalidates the prefix
and re-reads the whole conversation at full input price.

`CLAUDE_STABLE_PREFIX=true` (the default) passes
`--exclude-dynamic-system-prompt-sections`, which moves those sections into the
first user message — replayed verbatim on resume, so the prefix stops moving.
Measured on this repo, resuming a session across a commit:

| | cache re-created | cache read |
|---|---:|---:|
| without the flag | 7,691 | 15,645 |
| with the flag | 3,739 | 19,419 |

The tradeoff is that Claude sees cwd and environment as user-turn context rather
than system-prompt context, so it weighs them slightly less heavily.

The other levers, roughly in order of effect:

- **Keep conversations short.** History is re-sent every turn, and a cache miss
  re-reads all of it. `!convo new` per task beats one immortal transcript; the
  bot nudges you at 40 turns.
- **Match effort to the task.** `CLAUDE_EFFORT` and `!effort` control thinking
  tokens, which bill as output. `high` on every "what's in this folder" is real
  money.
- **Don't switch model or effort mid-conversation.** Both are part of the cache
  key, so a switch re-reads the whole history. `!model` and `!effort` warn when
  the active conversation already has a session.
- **Watch the footer.** Every turn reports its cache hit rate, and flags a turn
  that re-cached more than 20k tokens on a resume — that is the symptom of a
  prefix that moved.

`AUTO_SCREENSHOT` costs no tokens; the screenshot goes to Discord, not to Claude.
Likewise `--verbose` and `stream-json` are local plumbing.

### Usage windows

Subscription limits are a **rolling 5-hour window** that opens on your first
request and closes five hours later. Left alone it drifts to whenever you next
happen to be at the keyboard, so a long session can hit a ceiling that arrived at
an unpredictable hour.

With `WINDOW_WATCH` on, the bot polls the usage endpoint (a plain HTTPS GET — no
tokens) and DMs you the moment the window rolls over. With `WINDOW_WARM` on it
also fires one minimal probe at that moment to pin the next window to a boundary
you know. Usage windows are shared across models, so a Haiku probe opens exactly
the window Opus later draws on; one anchor costs about 6.5k tokens, most of them
cache reads. `!window` shows the current state and `!window anchor` opens one on
demand.

---

## Security — read this

This bot is, by design, **unrestricted remote code execution on your PC**, and
the default `CLAUDE_PERMISSION_MODE=bypassPermissions` means Claude runs tools
without asking. That's what makes it useful unattended, and it's also the whole
risk. What protects you is exactly two things:

- **`DISCORD_OWNER_ID`** — every message from any other account is dropped
  immediately. Verify you pasted *your* ID.
- **`DISCORD_TOKEN` secrecy** — anyone with that token becomes the bot, and the
  bot's DM channel is a shell on your machine. `.env` is gitignored; keep it that
  way, and reset the token in the developer portal if it ever leaks.

Worth knowing:

- Turn on 2FA for the Discord account that owns the bot.
- With `CLAUDE_CHROME=true` (the default), the reach of a prompt is *every site
  you are signed into in Chrome*, not just this machine's files. That is the
  single biggest thing to think about here. `CLAUDE_CHROME=false` removes it.
- Screenshots and `!watch` send whatever is on screen — password managers,
  private messages, anything — to Discord's servers, where they persist in your
  DM history until you delete them.
- Discord DMs are not end-to-end encrypted.
- `!kill` and `!shutdown` need admin rights for elevated processes; run the bot
  as administrator only if you actually need that.
- For a tighter setup: set `CLAUDE_PERMISSION_MODE=acceptEdits` (file edits are
  automatic, shell commands still prompt — note that prompts will block a turn
  until it times out, since you can't answer them over Discord), and point
  `DEFAULT_WORKDIR` at a single project folder.

## Files

| | |
|---|---|
| `bot.py` | Discord client, command table, Claude progress UI. |
| `claude_runner.py` | Claude Code CLI subprocess + `stream-json` parsing. |
| `usage.py` | Plan rate limits (OAuth endpoint) + local token accounting. |
| `convos.py` | Named, switchable conversations; the `.state/sessions.json` format. |
| `window.py` | 5-hour window watcher, reset alerts, and the anchor probe. |
| `screen.py` | Screenshots, GIF recording, webcam. |
| `ui.py` | Tray icon and the live task HUD. |
| `sysctl.py` | Mouse/keyboard, processes, clipboard, volume, power, windows. |
| `config.py` | `.env` loading, MCP config discovery. |
| `mcp.json.example` | Template for the Chrome MCP server. Copy to `mcp.json`. |
| `startup.pyw` | Console-less entry point for the startup shortcut; logs to `startup.log`. |
| `install-startup.ps1` | Registers/removes the Task Manager startup entry. |
| `.state/sessions.json` | Per-channel named conversations: session IDs, directories, model/effort. |
| `.state/window.json` | Last-seen 5-hour window reset, so a restart doesn't re-announce it. |
