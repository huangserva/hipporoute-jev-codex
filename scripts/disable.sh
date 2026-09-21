#!/bin/zsh
# Hide/disable the Codex Router catalog route; Codex config remains managed.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${CODEX_ROUTER_HOME:-}" ]]; then
  CR_BIN="$CODEX_ROUTER_HOME/bin/codex-router"
elif [[ -x "$REPO/../codex-router/bin/codex-router" ]]; then
  CR_BIN="$REPO/../codex-router/bin/codex-router"
elif command -v codex-router >/dev/null 2>&1; then
  CR_BIN="$(command -v codex-router)"
else
  CR_BIN=""
fi
LABEL="com.jev.codex-jev-router"
STOP_SERVICE=0

for argument in "$@"; do
  case "$argument" in
    --stop-service) STOP_SERVICE=1 ;;
    *) print -u2 "usage: $0 [--stop-service]"; exit 2 ;;
  esac
done

if [[ -x "$CR_BIN" ]]; then
  "$CR_BIN" control picker set jev/auto hide 2>/dev/null || true
  "$CR_BIN" providers generic disable jev 2>/dev/null || true
fi

if (( STOP_SERVICE )); then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi

print "Codex + Jev Router is hidden and provider jev is disabled"
print "shadow sentinel was preserved"
