#!/bin/zsh
# One-shot watchdog suitable for a user cron/periodic runner.
set -euo pipefail

LABEL="com.jev.codex-jev-router"
HEALTH_URL="http://127.0.0.1:4319/health"
if curl --noproxy 127.0.0.1 -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
  exit 0
fi
launchctl kickstart -k "gui/$(id -u)/$LABEL"
