#!/usr/bin/env bash
# run_local.sh -- the dashboard on this laptop, watching the deployed one.
#
# Reads the public IP that oracle_provision.sh recorded, so the header shows
# whether the Oracle box is answering without anything to configure by hand.
#
#   ./dashboard/run_local.sh
#   DASHBOARD_PORT=9000 ./dashboard/run_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${NAME:-trading-bots}"
STATE_FILE="${STATE_FILE:-$HOME/.oci/${NAME}-provision.env}"

if [ -z "${DASHBOARD_REMOTE:-}" ] && [ -f "$STATE_FILE" ]; then
  # shellcheck disable=SC1090
  . "$STATE_FILE"
  if [ -n "${ORACLE_PUBLIC_IP:-}" ]; then
    export DASHBOARD_REMOTE="http://${ORACLE_PUBLIC_IP}:${DASH_PORT:-8080}"
    export DASHBOARD_REMOTE_LABEL="${DASHBOARD_REMOTE_LABEL:-Oracle Cloud}"
  fi
fi

if [ -n "${DASHBOARD_REMOTE:-}" ]; then
  echo "watching $DASHBOARD_REMOTE"
else
  echo "no Oracle instance recorded yet; the remote indicator will stay hidden"
fi

exec python3 dashboard/server.py
