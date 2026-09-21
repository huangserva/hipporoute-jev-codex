#!/bin/zsh
# Install or replace the current user's launchd service.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
LABEL="com.jev.codex-jev-router"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ENV_LABEL="com.jev.codex-router-loopback-env"
ENV_PLIST="$HOME/Library/LaunchAgents/$ENV_LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
DOMAIN="gui/$(id -u)"
HEALTH_URL="http://127.0.0.1:4319/health"
SERVICE_CONFIG="${JEV_ROUTER_CONFIG:-$REPO/config.service.toml}"
JEV_ROUTER_HTTPS_PROXY="${JEV_ROUTER_HTTPS_PROXY:-${HTTPS_PROXY:-${https_proxy:-}}}"
JEV_ROUTER_HTTP_PROXY="${JEV_ROUTER_HTTP_PROXY:-${HTTP_PROXY:-${http_proxy:-$JEV_ROUTER_HTTPS_PROXY}}}"

[[ -x "$PYTHON_BIN" ]] || { print -u2 "python3 not found"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

if [[ ! -f "$SERVICE_CONFIG" ]]; then
  cp "$REPO/config.service.example.toml" "$SERVICE_CONFIG"
  chmod 600 "$SERVICE_CONFIG"
  print "created local service config: $SERVICE_CONFIG"
fi

case "$JEV_ROUTER_HTTPS_PROXY$JEV_ROUTER_HTTP_PROXY" in
  *[\<\>\&]*) print -u2 "proxy URL contains unsupported XML characters"; exit 2 ;;
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
    <string>codex_jev_router</string>
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
  <key>StandardOutPath</key><string>$LOG_DIR/codex-jev-router.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/codex-jev-router.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

for _ in {1..40}; do
  if curl --noproxy 127.0.0.1 -fsS --max-time 2 "$HEALTH_URL" 2>/dev/null \
      | grep -q '"jev_key":true'; then
    print "codex-jev-router service healthy; jev_key=true"
    exit 0
  fi
  sleep 0.5
done

print -u2 "service did not become healthy with jev_key=true"
print -u2 "inspect: $LOG_DIR/codex-jev-router.err.log"
exit 1
