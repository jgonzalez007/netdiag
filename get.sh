#!/bin/bash
# Network Doctor one-line installer for macOS:
#   curl -fsSL https://raw.githubusercontent.com/jgonzalez007/netdiag/main/get.sh | bash
# Installs to ~/.netdiag/app and creates ~/Applications/Network Doctor.app. No sudo needed.
# Uninstall: rm -rf ~/.netdiag "$HOME/Applications/Network Doctor.app"
set -euo pipefail

REPO="${NETDIAG_REPO:-https://raw.githubusercontent.com/jgonzalez007/netdiag/main}"
DIR="$HOME/.netdiag/app"
APP="$HOME/Applications/Network Doctor.app"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ "$(uname -s)" = Darwin ] || { echo "Network Doctor runs on macOS only."; exit 1; }

# python3 ships with Apple's Command Line Tools. Without them /usr/bin/python3 is only a stub.
if ! xcode-select -p >/dev/null 2>&1; then
    say "One-time setup: Network Doctor needs Apple's Command Line Tools."
    echo "A window will open. Click Install, wait until it finishes, then run this same command again."
    xcode-select --install >/dev/null 2>&1 || true
    exit 1
fi

say "Installing Network Doctor…"
mkdir -p "$DIR"
curl -fsSL "$REPO/netdiag" -o "$DIR/netdiag"
curl -fsSL "$REPO/netdiag-web.py" -o "$DIR/netdiag-web.py"
chmod +x "$DIR/netdiag" "$DIR/netdiag-web.py"

# A minimal app bundle: starts the dashboard server if it isn't running, then opens it.
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Network Doctor</string>
  <key>CFBundleIdentifier</key><string>com.jgonzalez007.network-doctor</string>
  <key>CFBundleExecutable</key><string>network-doctor</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/network-doctor" <<LAUNCHER
#!/bin/bash
URL=http://127.0.0.1:8765
if ! curl -s -o /dev/null -m 1 --noproxy '*' "\$URL/"; then
    # setsid: own session, so the server outlives this launcher and whatever started it.
    NETDIAG_NO_BROWSER=1 nohup /usr/bin/python3 -c 'import os, sys; os.setsid(); os.execv(sys.executable, [sys.executable] + sys.argv[1:])' \\
        "$DIR/netdiag-web.py" >"$HOME/.netdiag/web.log" 2>&1 &
    for _ in \$(seq 40); do curl -s -o /dev/null -m 1 --noproxy '*' "\$URL/" && break; sleep 0.25; done
fi
open "\$URL"
LAUNCHER
chmod +x "$APP/Contents/MacOS/network-doctor"

say "Done. Opening Network Doctor…"
echo "Next time, open it from Spotlight or Launchpad: \"Network Doctor\"."
open "$APP"
