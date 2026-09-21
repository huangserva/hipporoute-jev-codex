#!/bin/zsh
# Publish the Jev provider in Codex Router without changing Codex's provider.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -n "${CODEX_ROUTER_HOME:-}" ]]; then
  CR_BIN="$CODEX_ROUTER_HOME/bin/codex-router"
elif [[ -x "$REPO/../codex-router/bin/codex-router" ]]; then
  CR_BIN="$REPO/../codex-router/bin/codex-router"
elif command -v codex-router >/dev/null 2>&1; then
  CR_BIN="$(command -v codex-router)"
else
  print -u2 "Codex Router not found; set CODEX_ROUTER_HOME or add codex-router to PATH"
  exit 1
fi
CODEX_ROUTER_HEALTH="http://127.0.0.1:4202/health"
JEV_ROUTER_HEALTH="http://127.0.0.1:4319/health"
SHADOW_PATH="$HOME/.codex/codex-jev-router/router.shadow"
USER_MODELS="$HOME/.codex/codex-router/user-models.json"

check_codex_router_health() {
  curl --noproxy 127.0.0.1 -fsS --max-time 2 "$CODEX_ROUTER_HEALTH" >/dev/null 2>&1
}

check_jev_router_health() {
  curl --noproxy 127.0.0.1 -fsS --max-time 2 "$JEV_ROUTER_HEALTH" 2>/dev/null \
    | grep -q '"jev_key":true'
}

[[ -x "$CR_BIN" ]] || {
  print -u2 "Codex Router is not executable: $CR_BIN"
  exit 1
}

if ! check_codex_router_health; then
  "$CR_BIN" start
fi
if ! check_codex_router_health; then
  print -u2 "Codex Router health check failed; provider was not published"
  exit 1
fi

if ! check_jev_router_health; then
  "$REPO/scripts/install-service.sh"
fi
if ! check_jev_router_health; then
  print -u2 "codex-jev-router health/key check failed; provider was not published"
  exit 1
fi

mkdir -p "$(dirname "$SHADOW_PATH")"
touch "$SHADOW_PATH"

"$CR_BIN" chatgpt-session enable
if "$CR_BIN" providers generic list --json | grep -q '"id": "jev"'; then
  "$CR_BIN" providers generic edit jev \
    --name "Codex + Jev Router" \
    --base-url http://127.0.0.1:4319/v1 \
    --adapter openai-responses \
    --allow-private
else
  "$CR_BIN" providers generic add jev \
    --name "Codex + Jev Router" \
    --base-url http://127.0.0.1:4319/v1 \
    --adapter openai-responses \
    --allow-private
fi
"$CR_BIN" providers generic enable jev
python3 "$REPO/scripts/codex_router_catalog.py" "$USER_MODELS"
"$CR_BIN" refresh-catalog
"$CR_BIN" control picker set jev/auto show

print "Codex + Jev Router published in shadow mode"
print "Fully quit and reopen ChatGPT.app, then select: Codex + Jev Router"
print "disable: $REPO/scripts/disable.sh"
