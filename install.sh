#!/bin/bash
# Install Vector Status widget (Debian / Ubuntu / Raspberry Pi OS / labwc / Wayland)
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root:  sudo ./install.sh"
  exit 1
fi

if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="${INSTALL_USER:-}"
  if [[ -z "$TARGET_USER" ]]; then
    echo "Could not detect non-root user. Run:"
    echo "  sudo INSTALL_USER=yourusername ./install.sh"
    exit 1
  fi
fi

if ! id "$TARGET_USER" &>/dev/null; then
  echo "User not found: $TARGET_USER"
  exit 1
fi

TARGET_GROUP="$(id -gn "$TARGET_USER")"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

if [[ -z "$TARGET_HOME" || ! -d "$TARGET_HOME" ]]; then
  echo "Could not resolve home directory for $TARGET_USER"
  exit 1
fi

echo "=== Vector Status installer ==="
echo "  User:   $TARGET_USER"
echo "  Home:   $TARGET_HOME"
echo "  Prefix: $PREFIX"
echo

export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  echo "Installing dependencies..."
  apt-get update -qq
  apt-get install -y -qq \
    python3 \
    python3-gi \
    python3-cairo \
    gir1.2-gtk-3.0 \
    gir1.2-gdk-3.0 \
    gir1.2-gdkpixbuf-2.0 \
    gir1.2-ayatanaappindicator3-0.1 \
    libayatana-appindicator3-1 \
    gir1.2-gtklayershell-0.1 \
    libgtk-layer-shell0 \
    openssh-client \
    >/dev/null || {
      echo "Note: retrying with a smaller package set..."
      apt-get install -y -qq \
        python3 python3-gi python3-cairo \
        gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
        libgtk-layer-shell0 openssh-client 2>/dev/null || true
    }
else
  echo "Warning: apt-get not found. Install manually:"
  echo "  python3-gi python3-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-gtklayershell-0.1"
fi

LIBDIR="$PREFIX/lib/vector-status"
DOCDIR="$PREFIX/share/doc/vector-status"
APPDIR="$PREFIX/share/applications"
CFGDIR="$TARGET_HOME/.config/vector-status"

echo "Installing files..."
install -d "$LIBDIR/assets" "$DOCDIR" "$APPDIR" \
  "$TARGET_HOME/.config/autostart" \
  "$TARGET_HOME/.local/share/applications" \
  "$CFGDIR"

install -m 755 "$SCRIPT_DIR/bin/vector-status.py" "$LIBDIR/vector-status.py"
if [[ -d "$SCRIPT_DIR/bin/assets" ]]; then
  install -m 644 "$SCRIPT_DIR/bin/assets/"*.png "$LIBDIR/assets/"
fi
install -m 644 "$SCRIPT_DIR/README.md" "$DOCDIR/README.md"
install -m 644 "$SCRIPT_DIR/LICENSE" "$DOCDIR/LICENSE" 2>/dev/null || true
install -m 644 "$SCRIPT_DIR/VERSION" "$DOCDIR/VERSION" 2>/dev/null || true
install -m 644 "$SCRIPT_DIR/QUICKSTART.txt" "$DOCDIR/QUICKSTART.txt" 2>/dev/null || true

sed -e "s|@PREFIX@|$PREFIX|g" \
  "$SCRIPT_DIR/desktop/vector-status.desktop.in" \
  > "$APPDIR/vector-status.desktop"
chmod 644 "$APPDIR/vector-status.desktop"

install -m 644 "$APPDIR/vector-status.desktop" \
  "$TARGET_HOME/.local/share/applications/vector-status.desktop"
install -m 644 "$APPDIR/vector-status.desktop" \
  "$TARGET_HOME/.config/autostart/vector-status.desktop"

chown -R "$TARGET_USER:$TARGET_GROUP" \
  "$CFGDIR" \
  "$TARGET_HOME/.local/share/applications/vector-status.desktop" \
  "$TARGET_HOME/.config/autostart/vector-status.desktop"

# ---------------------------------------------------------------------------
# Optional Vector SSH unlock key (CPU / LOAD / RAM meters)
# Never shipped in the package — copied from a path the user provides.
# ---------------------------------------------------------------------------
is_ssh_private_key() {
  local f="$1"
  [[ -f "$f" && -r "$f" ]] || return 1
  grep -qE '^-----BEGIN ((OPENSSH|RSA|EC|DSA|ED25519) )?PRIVATE KEY-----' "$f"
}

expand_user_path() {
  local p="$1"
  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"
  # Drop quotes people type around paths with spaces
  if [[ "$p" == \"*\" || "$p" == \'*\' ]]; then
    p="${p:1:${#p}-2}"
  fi
  if [[ -z "$p" ]]; then
    echo ""
    return 0
  fi
  if [[ "$p" == "~" ]]; then
    p="$TARGET_HOME"
  elif [[ "$p" == "~/"* ]]; then
    p="$TARGET_HOME/${p#"~/"}"
  fi
  if [[ "$p" != /* ]]; then
    p="$TARGET_HOME/$p"
  fi
  if [[ -e "$p" ]]; then
    readlink -f "$p" 2>/dev/null || printf '%s\n' "$p"
  else
    printf '%s\n' "$p"
  fi
}

resolve_key_file() {
  local src="$1"
  if [[ -f "$src" ]]; then
    printf '%s\n' "$src"
    return 0
  fi
  if [[ -d "$src" ]]; then
    local name
    for name in ssh_root_key ssh_root_key.txt id_rsa_vector id_rsa; do
      if [[ -f "$src/$name" ]]; then
        printf '%s\n' "$src/$name"
        return 0
      fi
    done
    echo "Directory has no ssh_root_key / id_rsa: $src" >&2
    return 1
  fi
  echo "Path not found: $src" >&2
  return 1
}

install_ssh_key() {
  local src dest
  src="$(resolve_key_file "$1")" || return 1
  dest="$CFGDIR/ssh_root_key"
  if ! is_ssh_private_key "$src"; then
    echo "Not a private SSH key: $src" >&2
    return 1
  fi
  install -m 600 -o "$TARGET_USER" -g "$TARGET_GROUP" "$src" "$dest"
  echo "Using key file: $src"
  echo "Installed SSH key → $dest (mode 600, owner $TARGET_USER)"
}

ask_for_ssh_key_path() {
  local attempts=0
  while [[ $attempts -lt 3 ]]; do
    local key_in=""
    read -r -p "Path to your Vector SSH private key: " key_in < /dev/tty || key_in=""
    if [[ -z "$key_in" ]]; then
      echo "No path entered — skipping. You can add a key later."
      return 1
    fi
    if install_ssh_key "$(expand_user_path "$key_in")"; then
      return 0
    fi
    attempts=$((attempts + 1))
    echo "Try again ($attempts/3), or press Enter to skip."
  done
  return 1
}

SSH_KEY_INSTALLED=0
DEST_KEY="$CFGDIR/ssh_root_key"

if [[ -n "${VECTOR_SSH_KEY:-}" ]]; then
  if install_ssh_key "$(expand_user_path "$VECTOR_SSH_KEY")"; then
    SSH_KEY_INSTALLED=1
  else
    echo "VECTOR_SSH_KEY was set but could not be installed."
    echo "CPU/LOAD/RAM will be unavailable until you add a key."
  fi
elif [[ "${SKIP_SSH_KEY:-}" == "1" ]]; then
  echo "Skipping SSH key setup (SKIP_SSH_KEY=1)."
elif [[ -r /dev/tty ]]; then
  echo
  echo "CPU, LOAD, and RAM meters need Vector's SSH unlock key."
  echo "Battery, temperature, and voltage work without it."
  echo "The key is NOT included in this package — you provide your own."
  echo
  if [[ -f "$DEST_KEY" ]]; then
    echo "A key is already installed at $DEST_KEY"
    read -r -p "Replace it? [y/N] " WANT_KEY < /dev/tty || WANT_KEY=""
    WANT_KEY="${WANT_KEY:-n}"
    if [[ "$WANT_KEY" =~ ^[Yy]$ ]]; then
      if ask_for_ssh_key_path; then
        SSH_KEY_INSTALLED=1
      fi
    else
      SSH_KEY_INSTALLED=1
      echo "Keeping existing key at $DEST_KEY"
    fi
  else
    read -r -p "Install Vector SSH unlock key for CPU/LOAD/RAM? [Y/n] " WANT_KEY < /dev/tty || WANT_KEY=""
    WANT_KEY="${WANT_KEY:-Y}"
    if [[ "$WANT_KEY" =~ ^[Yy]$ ]]; then
      if ask_for_ssh_key_path; then
        SSH_KEY_INSTALLED=1
      fi
    else
      echo "Skipped SSH key. BAT / TMP / VOLT will still work."
    fi
  fi
else
  echo "No terminal available — skipped SSH key prompt."
  echo "Add a key later: copy it to $DEST_KEY && chmod 600 $DEST_KEY"
fi

if [[ -f "$DEST_KEY" ]]; then
  SSH_KEY_INSTALLED=1
fi

install -d "$PREFIX/bin"
cat > "$PREFIX/bin/vector-status" <<EOF
#!/bin/bash
exec /usr/bin/python3 "$LIBDIR/vector-status.py" "\$@"
EOF
chmod 755 "$PREFIX/bin/vector-status"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPDIR" 2>/dev/null || true
  update-desktop-database "$TARGET_HOME/.local/share/applications" 2>/dev/null || true
fi

echo
echo "=== Install complete ==="
echo "Start now:"
echo "  sudo -u $TARGET_USER $PREFIX/bin/vector-status &"
echo "  # or: Applications → Vector Status"
echo
echo "Controls:"
echo "  Drag          move the gauge"
echo "  Left-click    cycle BAT → TMP → VOLT → CPU → LOAD → RAM"
echo "  Right-click   menu (refresh, reset position, quit)"
echo
echo "Widget appears when Vector is online and hides when offline."
echo "Optional IP override: echo YOUR_IP > $CFGDIR/ip.txt"
if [[ "$SSH_KEY_INSTALLED" -eq 1 ]]; then
  echo "SSH key installed — CPU / LOAD / RAM meters are available."
else
  echo "No SSH key — BAT / TMP / VOLT work; CPU / LOAD / RAM need a key:"
  echo "  copy your unlock key to $CFGDIR/ssh_root_key && chmod 600 $CFGDIR/ssh_root_key"
fi
echo

if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
  if ! pgrep -u "$TARGET_USER" -f 'vector-status\.py' >/dev/null 2>&1; then
    echo "Starting Vector Status for $TARGET_USER..."
    sudo -u "$TARGET_USER" env \
      DISPLAY="${DISPLAY:-:0}" \
      WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" \
      XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u "$TARGET_USER")}" \
      "$PREFIX/bin/vector-status" >/dev/null 2>&1 &
    echo "Started (background)."
  else
    echo "Already running for $TARGET_USER."
  fi
fi
