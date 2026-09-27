#!/usr/bin/env bash
# oracle_logs.sh [service]  -- follow logs from the Oracle instance.
#   ./deploy/oracle_logs.sh            all services
#   ./deploy/oracle_logs.sh orb        just the ORB bot
set -euo pipefail
NAME="${NAME:-trading-bots}"
STATE_FILE="${STATE_FILE:-$HOME/.oci/${NAME}-provision.env}"
[ -f "$STATE_FILE" ] && . "$STATE_FILE"
IP="${IP:-${ORACLE_PUBLIC_IP:-}}"
KEY="${KEY:-${ORACLE_SSH_KEY:-$HOME/.ssh/oracle_bots}}"
[ -n "$IP" ] || { echo "no IP; run oracle_provision.sh first" >&2; exit 1; }
exec ssh -t -i "$KEY" "ubuntu@$IP" \
  "cd ~/trading-strategies/deploy && sg docker -c 'docker compose logs -f --tail=80 ${1:-}'"
