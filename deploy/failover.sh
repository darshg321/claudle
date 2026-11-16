#!/usr/bin/env bash
# Pi failover for Claudle: the laptops are the primary hosts. The Pi runs the bot only
# while none of them is reachable over Tailscale, so only one copy answers DMs.
#
# Run every 30 s by claudle-failover.timer. Starts the bot after DOWN_CHECKS consecutive
# checks with every laptop unreachable; stops it as soon as any laptop is back.
# Override in /etc/claudle-failover.env:  LAPTOPS="pop-os ds-laptop"  DOWN_CHECKS=2
set -uo pipefail

LAPTOPS="${LAPTOPS:-pop-os ds-laptop}"
DOWN_CHECKS="${DOWN_CHECKS:-2}"
STATE=/run/claudle-failover.down

reachable() {
  # Online in the tailnet *and* answering a Tailscale ping (a sleeping laptop can still show
  # Online for a few minutes).
  local host=$1
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
host = sys.argv[1]
peers = json.load(sys.stdin).get("Peer", {}).values()
sys.exit(0 if any(p.get("HostName") == host and p.get("Online") for p in peers) else 1)' "$host" || return 1
  # Up to 3 tries: the first ping after a quiet period can be lost while the path is set up.
  timeout 12 tailscale ping -c 3 --timeout 3s "$host" >/dev/null 2>&1
}

if ! tailscale status >/dev/null 2>&1; then
  echo "tailscale not connected; leaving claudle as is"
  exit 0
fi

up=""
for h in $LAPTOPS; do
  if reachable "$h"; then up=$h; break; fi
done

if [[ -n "$up" ]]; then
  echo 0 > "$STATE"
  if systemctl is-active --quiet claudle; then
    echo "$up is reachable: stopping the Pi copy of claudle"
    systemctl stop claudle
  fi
  exit 0
fi

n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
if (( n >= DOWN_CHECKS )) && ! systemctl is-active --quiet claudle; then
  echo "no laptop reachable ($LAPTOPS) for $n checks: starting claudle on the Pi"
  systemctl start claudle
fi
