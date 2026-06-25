#!/usr/bin/env bash
# Install the carclaude + cloudflared systemd services so both start on boot.
# Run with sudo, as your normal (non-root) user:
#   sudo bash <appdir>/deploy/install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$(dirname "$HERE")"

# Run carclaude as the invoking, non-root user — the agent executes arbitrary code, so it
# must NOT run privileged.
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  echo "Refusing to install carclaude to run as root." >&2
  echo "Run with sudo while logged in as your normal user:  sudo bash $HERE/install.sh" >&2
  exit 1
fi
HOMEDIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
CFGDIR="$HOMEDIR/.cloudflared"

echo "Installing for user '$TARGET_USER', app dir '$APPDIR'…"

echo "Stopping any manually-run instances (ignore errors)…"
pkill -f "uvicorn server.main:app" 2>/dev/null || true
pkill -f "cloudflared tunnel run voiceclaude" 2>/dev/null || true

render() {  # $1 = unit filename; substitute placeholders, then install
  sed -e "s|__USER__|$TARGET_USER|g" \
      -e "s|__APPDIR__|$APPDIR|g" \
      -e "s|__CFGDIR__|$CFGDIR|g" \
      "$HERE/$1" > "/etc/systemd/system/$1"
}

echo "Installing unit files…"
render carclaude.service
render cloudflared-carclaude.service

systemctl daemon-reload
systemctl enable --now carclaude.service cloudflared-carclaude.service

echo
systemctl --no-pager --lines=0 status carclaude.service cloudflared-carclaude.service || true
echo
echo "Done. Follow logs with:  journalctl -u carclaude -u cloudflared-carclaude -f"
