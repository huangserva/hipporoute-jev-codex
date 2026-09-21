#!/bin/zsh
# One-shot watchdog suitable for a user cron/periodic runner.
set -euo pipefail

LABEL="com.jev.codex-jev-router"
HEALTH_URL="http://127.0.0.1:4319/health"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/Library/Logs/codex-jev-router.watchdog.log"
JEV_ROUTER_HTTPS_PROXY="${JEV_ROUTER_HTTPS_PROXY:-${HTTPS_PROXY:-${https_proxy:-}}}"
JEV_ROUTER_HTTP_PROXY="${JEV_ROUTER_HTTP_PROXY:-${HTTP_PROXY:-${http_proxy:-$JEV_ROUTER_HTTPS_PROXY}}}"
healthy() {
  curl --noproxy 127.0.0.1 -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
}
if healthy; then
  exit 0
fi
print "[$(date '+%Y-%m-%dT%H:%M:%S%z')] unhealthy; restarting" >> "$LOG"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>>"$LOG" || true
sleep 2
if healthy; then
  exit 0
fi

# Fallback for a machine where the user launchd domain is unavailable.
cd "$REPO"
HTTPS_PROXY="$JEV_ROUTER_HTTPS_PROXY" \
HTTP_PROXY="$JEV_ROUTER_HTTP_PROXY" \
NO_PROXY=127.0.0.1,localhost \
nohup python3 -m codex_jev_router >> "$LOG" 2>&1 &
sleep 2
healthy
