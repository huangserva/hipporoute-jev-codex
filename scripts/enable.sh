#!/bin/zsh
# Health-first, reversible switch of Codex to the local shadow router.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HEALTH_URL="http://127.0.0.1:4319/health"
CONFIG_PATH="$HOME/.codex/config.toml"
STATE_PATH="$REPO/runtime/service/enable-state.json"
SHADOW_PATH="$HOME/.codex/codex-jev-router/router.shadow"

check_health() {
  curl --noproxy 127.0.0.1 -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null \
    | grep -q '"jev_key":true'
}

if ! check_health; then
  "$REPO/scripts/install-service.sh"
fi
if ! check_health; then
  print -u2 "router health check failed; Codex config was not changed"
  exit 1
fi

mkdir -p "$(dirname "$STATE_PATH")" "$(dirname "$SHADOW_PATH")" "$HOME/.codex"
touch "$SHADOW_PATH"
python3 "$REPO/scripts/configure_codex.py" enable \
  --config "$CONFIG_PATH" \
  --state "$STATE_PATH" \
  --backup-dir "$HOME/.codex"

print "shadow routing enabled"
print "health: $HEALTH_URL"
print "restore: $REPO/scripts/disable.sh"
