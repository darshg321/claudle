#!/usr/bin/env bash
# Linux counterpart of install-startup.ps1: run Claudle as a systemd user
# service tied to the graphical session, so it starts at login with a tray icon.
#
#   ./install-startup.sh            install + enable + start (default)
#   ./install-startup.sh status
#   ./install-startup.sh enable | disable | uninstall
#
# Output goes to the journal: journalctl --user -u claudle -f
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/claudle.service"
PYTHON="$HERE/.venv/bin/python"

install_unit() {
    if [[ ! -x "$PYTHON" ]]; then
        echo "No virtualenv at $HERE/.venv — create it first (see README)." >&2
        exit 1
    fi
    # The user manager's PATH is minimal; the bot needs `claude` and node on it.
    local path
    path="$(dirname "$(command -v claude)"):$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)"):/usr/local/bin:/usr/bin:/bin"
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT" <<EOF
[Unit]
Description=Claudle Discord PC-control bot
After=graphical-session.target network-online.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$HERE
Environment=PATH=$path
Environment=PYTHONUNBUFFERED=1
# glibc gives each thread its own malloc arena; two is plenty for this bot.
Environment=MALLOC_ARENA_MAX=2
ExecStart=$PYTHON $HERE/bot.py
# !relaunch exits 75 to be restarted; the tray's Quit exits 0 and stays down.
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
    systemctl --user daemon-reload
}

case "${1:-install}" in
    install)
        install_unit
        systemctl --user enable --now claudle.service
        systemctl --user --no-pager status claudle.service | head -5
        ;;
    enable)    systemctl --user enable --now claudle.service ;;
    disable)   systemctl --user disable --now claudle.service ;;
    status)    systemctl --user --no-pager status claudle.service ;;
    uninstall)
        systemctl --user disable --now claudle.service 2>/dev/null || true
        rm -f "$UNIT"
        systemctl --user daemon-reload
        ;;
    *) echo "usage: $0 [install|status|enable|disable|uninstall]" >&2; exit 2 ;;
esac
