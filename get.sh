#!/bin/bash
# Network Doctor one-line installer for macOS:
#   curl -fsSL https://raw.githubusercontent.com/jgonzalez007/netdiag/main/get.sh | bash
# Installs to ~/.netdiag/app and creates ~/Applications/Network Doctor.app. No sudo needed.
# Uninstall: launchctl bootout gui/$(id -u)/com.jgonzalez007.network-doctor; rm -rf ~/.netdiag "$HOME/Applications/Network Doctor.app"
set -euo pipefail

REPO="${NETDIAG_REPO:-https://raw.githubusercontent.com/jgonzalez007/netdiag/main}"
DIR="$HOME/.netdiag/app"
APP="$HOME/Applications/Network Doctor.app"
LABEL=com.jgonzalez007.network-doctor

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
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
# The dashboard server runs as a launchd job, not as a child of the app: macOS kills
# whatever an app spawns once the app's own process exits. The plist lives in ~/.netdiag,
# not ~/Library/LaunchAgents, so nothing starts at login. The server exits after 60 idle minutes.
cat > "$HOME/.netdiag/$LABEL.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$DIR/netdiag-web.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>NETDIAG_NO_BROWSER</key><string>1</string>
    <key>NETDIAG_IDLE_EXIT</key><string>60</string>
  </dict>
  <key>StandardOutPath</key><string>$HOME/.netdiag/web.log</string>
  <key>StandardErrorPath</key><string>$HOME/.netdiag/web.log</string>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/network-doctor" <<LAUNCHER
#!/bin/bash
URL=http://127.0.0.1:8765
if ! curl -s -o /dev/null -m 1 --noproxy '*' "\$URL/"; then
    launchctl bootout "gui/\$(id -u)/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/\$(id -u)" "$HOME/.netdiag/$LABEL.plist"
    for _ in \$(seq 60); do curl -s -o /dev/null -m 1 --noproxy '*' "\$URL/" && break; sleep 0.25; done
fi
[ -n "\${NETDIAG_NO_BROWSER:-}" ] || open "\$URL"
LAUNCHER
chmod +x "$APP/Contents/MacOS/network-doctor"

# Stop a server from a previous install so the new code is what runs.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

say "Done. Opening Network Doctor…"
echo "Next time, open it from Spotlight or Launchpad: \"Network Doctor\"."
open "$APP"
