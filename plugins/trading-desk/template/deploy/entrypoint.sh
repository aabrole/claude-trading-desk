#!/usr/bin/env bash
# Thin launcher so docker-compose can start any bot by name without a custom
# image per service. Restarts on crash are handled by compose, not here: if a
# bot dies we want the container to exit so the restart policy is visible in
# `docker ps`, rather than a silent internal loop hiding a broken bot.
set -euo pipefail
case "${BOT:-}" in
  orb)     exec python3 -u /app/orb_breakout/live.py "$@" ;;
  pead)    exec python3 -u /app/news_catalyst_options/live_pead.py "$@" ;;
  insider) exec python3 -u /app/news_catalyst_options/live_insider.py "$@" ;;
  lvl)     exec python3 -u /app/lvl_confluence/live_lvl.py "$@" ;;
  keepalive) exec python3 -u /app/deploy/keepalive.py ;;
  *) echo "set BOT to one of: orb pead insider lvl keepalive"; exit 64 ;;
esac
