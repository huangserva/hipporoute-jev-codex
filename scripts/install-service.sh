#!/usr/bin/env bash
# Install or replace the current user's launchd service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.hippo.hipporoute"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_LABEL="com.hippo.codex-router-loopback-env"
ENV_PLIST="$HOME/Library/LaunchAgents/$ENV_LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
DOMAIN="gui/$(id -u)"
HEALTH_URL="http://127.0.0.1:4319/health"
SERVICE_CONFIG="${JEV_ROUTER_CONFIG:-$REPO/config.service.toml}"
JEV_ROUTER_HTTPS_PROXY="${JEV_ROUTER_HTTPS_PROXY:-${HTTPS_PROXY:-${https_proxy:-}}}"
JEV_ROUTER_HTTP_PROXY="${JEV_ROUTER_HTTP_PROXY:-${HTTP_PROXY:-${http_proxy:-$JEV_ROUTER_HTTPS_PROXY}}}"

# The interpreter is baked into the plist, so never trust `command -v python3`:
# an activated virtualenv from another project would be pinned there and the
# service would die as soon as that environment changes. Probe stable system
# locations in order and require Python >= 3.11.
python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1
}

detect_python() {
  local candidate
  if [[ -n "${HIPPOROUTE_PYTHON:-}" ]]; then
    if [[ ! -x "$HIPPOROUTE_PYTHON" ]]; then
      printf 'HIPPOROUTE_PYTHON is not executable: %s\n' "$HIPPOROUTE_PYTHON" >&2
      return 1
    fi
    if ! python_is_supported "$HIPPOROUTE_PYTHON"; then
      printf 'HIPPOROUTE_PYTHON must be Python >= 3.11: %s\n' "$HIPPOROUTE_PYTHON" >&2
      return 1
    fi
    printf '%s\n' "$HIPPOROUTE_PYTHON"
    return 0
  fi
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [[ -x "$candidate" ]] || continue
    if python_is_supported "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
    printf 'skipping %s: not a working Python >= 3.11\n' "$candidate" >&2
  done
  candidate="$(command -v python3 || true)"
  if [[ -n "$candidate" ]] && python_is_supported "$candidate"; then
    printf '%s\n' "$candidate"
    return 0
  fi
  printf 'no Python >= 3.11 found; set HIPPOROUTE_PYTHON to one\n' >&2
  return 1
}

if ! PYTHON_BIN="$(detect_python)"; then
  printf 'python interpreter detection failed\n' >&2
  exit 1
fi
printf 'using python interpreter: %s\n' "$PYTHON_BIN"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

if [[ ! -f "$SERVICE_CONFIG" ]]; then
  cp "$REPO/config.service.example.toml" "$SERVICE_CONFIG"
  chmod 600 "$SERVICE_CONFIG"
  printf 'created local service config: %s\n' "$SERVICE_CONFIG"
fi

case "$JEV_ROUTER_HTTPS_PROXY$JEV_ROUTER_HTTP_PROXY" in
  *[\<\>\&]*) printf 'proxy URL contains unsupported XML characters\n' >&2; exit 2 ;;
esac
PROXY_XML=""
if [[ -n "$JEV_ROUTER_HTTPS_PROXY" ]]; then
  PROXY_XML="$PROXY_XML
    <key>HTTPS_PROXY</key><string>$JEV_ROUTER_HTTPS_PROXY</string>"
fi
if [[ -n "$JEV_ROUTER_HTTP_PROXY" ]]; then
  PROXY_XML="$PROXY_XML
    <key>HTTP_PROXY</key><string>$JEV_ROUTER_HTTP_PROXY</string>"
fi

# GUI apps inherit launchd's environment, not an interactive shell's NO_PROXY.
# Persist the loopback bypass so ChatGPT.app never sends ports 4202/4319 to the
# machine's outbound proxy after the next login.
cat > "$ENV_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ENV_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/launchctl</string>
    <string>setenv</string>
    <string>NO_PROXY</string>
    <string>localhost,127.0.0.1,::1</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>
EOF
plutil -lint "$ENV_PLIST" >/dev/null
launchctl bootout "$DOMAIN/$ENV_LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$ENV_PLIST"
launchctl kickstart "$DOMAIN/$ENV_LABEL" 2>/dev/null || true
launchctl setenv NO_PROXY "localhost,127.0.0.1,::1"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>-m</string>
    <string>hipporoute</string>
    <string>--config</string>
    <string>$SERVICE_CONFIG</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    $PROXY_XML
    <key>NO_PROXY</key><string>127.0.0.1,localhost</string>
  </dict>
  <key>StandardOutPath</key><string>$LOG_DIR/hipporoute.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/hipporoute.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
# Only this label is replaced. An older `com.jev.codex-jev-router` job must be
# booted out first (see the migration section of README.md), otherwise it keeps
# the old path loaded and both jobs race for port 4319.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

for _ in {1..40}; do
  if curl --noproxy 127.0.0.1 -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null \
      | grep -q '"jev_key":true'; then
    printf 'hipporoute service healthy; jev_key=true; python=%s\n' "$PYTHON_BIN"
    exit 0
  fi
  sleep 0.5
done

printf 'service did not become healthy with jev_key=true\n' >&2
printf 'inspect: %s\n' "$LOG_DIR/hipporoute.err.log" >&2
exit 1
