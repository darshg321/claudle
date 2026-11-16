#!/usr/bin/env bash
# Installs Claudle as an always-on headless systemd service on a Raspberry Pi,
# plus a timer that pulls from git every minute and restarts on new commits.
#
#   git clone https://github.com/darshg321/claudle ~/claudle
#   cp .env.example .env && $EDITOR .env      # set HEADLESS=true
#   sudo ./deploy/install-pi.sh
#
# Safe to re-run. Paths assume user `dash` and ~/claudle (see claudle.service).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
OWNER=$(stat -c %U "$DIR")
as_owner() { runuser -u "$OWNER" -- "$@"; }

PKGS=(python3 python3-venv git xclip)
dpkg -s "${PKGS[@]}" &>/dev/null || { apt-get update -qq; apt-get install -y -qq "${PKGS[@]}" >/dev/null; }

[[ -x "$DIR/.venv/bin/python" ]] || as_owner python3 -m venv "$DIR/.venv"
if ! cmp -s "$DIR/requirements.txt" "$DIR/.venv/.requirements.txt"; then
  as_owner "$DIR/.venv/bin/pip" install -q --upgrade pip
  as_owner "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
  as_owner cp "$DIR/requirements.txt" "$DIR/.venv/.requirements.txt"
fi

[[ -f "$DIR/.env" ]] || echo "warning: $DIR/.env missing; copy .env.example and fill it in" >&2

install -m 644 "$DIR/deploy/claudle.service" "$DIR/deploy/claudle-update.service" \
  "$DIR/deploy/claudle-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable claudle claudle-update.timer >/dev/null
systemctl start claudle-update.timer
[[ "${1:-}" == "--no-restart" ]] || systemctl restart claudle
echo "claudle installed: journalctl -u claudle -f"
