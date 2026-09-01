"""Windowless entry point for the Startup shortcut.

pythonw.exe has no console, so anything bot.py printed would vanish and a bad
.env would look like the bot simply never started. Point stdout/stderr at
startup.log, then hand off to bot.main().

Nothing else needs this file — use run.bat or `python bot.py` when you want a
console. Install the shortcut with install-startup.ps1.
"""

import os
import sys
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "startup.log")
MAX_LOG_BYTES = 1_000_000

# The shortcut sets this too, but a stray launch shouldn't miss .env / .state.
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG_BYTES:
    os.replace(LOG, LOG + ".old")

log = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
sys.stdout = log
sys.stderr = log

print(f"\n=== startup {datetime.now():%Y-%m-%d %H:%M:%S} ===")

try:
    import bot

    bot.main()
except SystemExit:
    raise
except BaseException:
    traceback.print_exc(file=log)
    raise
