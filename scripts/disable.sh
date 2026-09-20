#!/bin/zsh
# Restore the pre-router Codex config and optionally stop the service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.jev.codex-jev-router"
CONFIG_PATH="$HOME/.codex/config.toml"
STATE_PATH="$REPO/runtime/service/enable-state.json"
SHADOW_PATH="$HOME/.codex/codex-jev-router/router.shadow"
STOP_SERVICE=0
FORCE=0

for argument in "$@"; do
  case "$argument" in
    --stop-service) STOP_SERVICE=1 ;;
    --force) FORCE=1 ;;
    *) print -u2 "usage: $0 [--stop-service] [--force]"; exit 2 ;;
  esac
done

if (( FORCE )); then
  python3 "$REPO/scripts/configure_codex.py" restore \
    --config "$CONFIG_PATH" --state "$STATE_PATH" --force
else
  python3 "$REPO/scripts/configure_codex.py" restore \
    --config "$CONFIG_PATH" --state "$STATE_PATH"
fi
rm -f "$SHADOW_PATH"

if (( STOP_SERVICE )); then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
print "Codex config restored; shadow sentinel removed"
