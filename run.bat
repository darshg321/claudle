@echo off
REM Launch the PC control bot. Keep this window open while you want remote access.
cd /d "%~dp0"
python bot.py
echo.
echo Bot exited. Press any key to close.
pause >nul
