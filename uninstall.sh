#!/bin/bash
# Remove Vector Status widget
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root:  sudo ./uninstall.sh"
  exit 1
fi

echo "Stopping running instances..."
pkill -f '/lib/vector-status/vector-status.py' 2>/dev/null || true
pkill -f 'vector-status/vector-status.py' 2>/dev/null || true
sleep 0.3

echo "Removing files..."
rm -rf "$PREFIX/lib/vector-status"
rm -rf "$PREFIX/share/doc/vector-status"
rm -f "$PREFIX/share/applications/vector-status.desktop"
rm -f "$PREFIX/bin/vector-status"

if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  UH="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
  rm -f "$UH/.local/share/applications/vector-status.desktop"
  rm -f "$UH/.config/autostart/vector-status.desktop"
  echo "Left saved position/mode in: $UH/.config/vector-status"
  echo "(delete that folder manually for a full cleanup)"
fi

echo "Uninstall complete."
