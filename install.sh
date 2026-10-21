#!/usr/bin/env bash
# install.sh — helper installer for netdiag
# Usage: ./install.sh [target-dir]
# Default: ~/.local/bin

set -euo pipefail
VERSION="1.0.0"
TARGET="${1:-${HOME}/.local/bin}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Ensure target exists
mkdir -p "$TARGET"

# Copy the script
cp "${SCRIPT_DIR}/netdiag" "${TARGET}/netdiag"
chmod +x "${TARGET}/netdiag"

# Web dashboard (needs python3; it runs the netdiag installed next to it)
if [ -f "${SCRIPT_DIR}/netdiag-web.py" ]; then
    cp "${SCRIPT_DIR}/netdiag-web.py" "${TARGET}/netdiag-web"
    chmod +x "${TARGET}/netdiag-web"
fi

# Add to PATH if not already there
if ! echo "$PATH" | tr ':' '\n' | grep -qF "$TARGET"; then
    SHELL_RC=""
    case "$SHELL" in
        *zsh*)  SHELL_RC="${HOME}/.zshrc" ;;
        *bash*) SHELL_RC="${HOME}/.bashrc" ;;
        *)      SHELL_RC="${HOME}/.bashrc" ;;
    esac
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$SHELL_RC" 2>/dev/null || true
    echo "Added $TARGET to PATH in $SHELL_RC"
fi

echo "Installed netdiag v${VERSION} to $TARGET/netdiag"
echo "Run: netdiag check 'symptom'"
echo "Run: netdiag doctor"
[ -f "${TARGET}/netdiag-web" ] && echo "Run: netdiag-web   (opens the Network Doctor dashboard)"
true