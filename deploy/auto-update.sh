#!/usr/bin/env bash
# Pulls the latest commit and restarts Claudle if it changed. Run as root by
# claudle-update.timer (every minute). Git runs as the repo owner so it uses
# their GitHub credentials. If the new version doesn't stay up, rolls back.
set -euo pipefail

DIR=/home/dash/claudle
OWNER=dash
BRANCH="${CLAUDLE_BRANCH:-main}"
# setpriv instead of runuser/sudo: no PAM session, so no journal noise every minute.
g() { HOME="/home/$OWNER" setpriv --reuid="$OWNER" --regid="$OWNER" --init-groups git -C "$DIR" "$@"; }

g fetch --quiet origin "$BRANCH"
OLD=$(g rev-parse HEAD)
NEW=$(g rev-parse "origin/$BRANCH")
[[ "$OLD" == "$NEW" ]] && exit 0
[[ "$(g rev-parse -q --verify refs/claudle/bad || true)" == "$NEW" ]] && exit 0

echo "update $OLD -> $NEW: $(g log -1 --format=%s "$NEW")"

deploy() {
  g reset --hard --quiet "$1"
  "$DIR/deploy/install-pi.sh" --no-restart
  systemctl restart claudle
  local before; before=$(systemctl show -p NRestarts --value claudle)
  # Healthy = still running, without crash-restarts, after 30s.
  sleep 30
  systemctl is-active --quiet claudle && [[ $(systemctl show -p NRestarts --value claudle) == "$before" ]]
}

if deploy "$NEW"; then
  echo "deployed $NEW"
  exit 0
fi

echo "$NEW did not stay up, rolling back to $OLD" >&2
journalctl -u claudle -n 20 --no-pager >&2 || true
g update-ref refs/claudle/bad "$NEW"
deploy "$OLD" || true
exit 1
