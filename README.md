# Netdiag — Portable Network Troubleshooting Agent

A zero-dependency macOS (and mostly Linux) CLI tool for diagnosing WiFi/Ethernet connectivity, detecting drops, and proposing fixes.

## Quick Start

Deploy to any Mac (or Linux) in 30 seconds:

```bash
# Method 1: Download + run directly (recommended)
# Downloads the latest release tarball and runs from /tmp (no install needed)
curl -sSL https://github.com/josegonzalez007/netdiag/releases/download/v1.0.0/netdiag-1.0.0.tar.gz | tar xz -C /tmp
/tmp/netdiag doctor                    # verify tools
/tmp/netdiag check "WiFi drops after sleep"  # diagnose

# Method 2: Install to PATH (~/.local/bin)
curl -sSL https://github.com/josegonzalez007/netdiag/releases/download/v1.0.0/netdiag-1.0.0.tar.gz | tar xz -C ~/.local
~/.local/install.sh                    # sets PATH in ~/.zshrc
netdiag doctor                         # verify
netdiag check "WiFi drops"             # diagnose

# Method 3: Direct download (open browser first for auth)
curl -L https://github.com/josegonzalez007/netdiag/releases/download/v1.0.0/netdiag-1.0.0.tar.gz | tar xz -C /tmp
```

> **Note:** The repo is public so anonymous downloads work. If you had a 404 error,
> make sure you're using the github.com URL (not raw.githubusercontent.com) and the tag name (`v1.0.0`).

## Features

- **Zero dependencies** — uses only built-in macOS (and standard Linux) tools
- **Fast** — diagnoses in <30 seconds with key network layers
- **Portable** — single file, works across machines
- **Monitoring** — continuous watch for intermittent drops
- **State persistence** — logs and alerts on change
- **Installable** — PATH setup + optional launchd monitor

## Usage

### Quick Diagnosis

```bash
netdiag check "WiFi drops after sleep"
```

### JSON Report (for scripts)

```bash
netdiag report --json "network issue" | jq .
```

### Continuous Monitoring

```bash
netdiag watch --every 30
# Detects interface up/down, DNS, gateway, HTTP reachability changes
# Logs state changes, can notify via macOS Notification Center
```

### Tool Verification

```bash
netdiag doctor
# Verifies ping, networksetup, dig, traceroute, etc.
```

### Installation

```bash
netdiag install
# Copies to ~/.local/bin (default) or a custom path
# Optionally installs a launchd monitor (may require sudo)
```

### Monitoring as a Service (macOS)

If you want network changes detected even when idle:

```bash
# Installs to ~/Library/LaunchAgents/com.jose.netdiag.plist
netdiag install --service
```

You can start/stop it with:

```bash
launchctl start com.jose.netdiag
launchctl unload ~/Library/LaunchAgents/com.jose.netdiag.plist
```

## What Happens When an Issue Is Detected

### 1. Detection

`netdiag watch` polls a **7-bit state signature** every N seconds:

```
wifi:1 | eth:0 | dns:1 | gw:1 | http:1 | gw_ip:192.168.12.1
```

Any bit flipping means the network state changed.

### 2. Immediate Actions

| Action | What Happens |
|--------|-------------|
| **Log event** | `~/.local/var/log/netdiag.events` — JSON line: time, previous state, new state |
| **Append to log** | `~/.local/var/log/netdiag.log` — `[timestamp] CHANGE: old → new` |
| **Notification** | macOS Notification Center via `osascript` |
| **Voice alert** | `say "Network state change detected"` (when enabled) |
| **State update** | Previous signature is replaced so the next change is detected correctly |

### 3. Diagnosis

```bash
netdiag check "symptom"
```

Produces a report with root cause, confidence level, an evidence trail, and a
specific recommended fix.

### 4. Workboard Integration

On OpenClaw hosts (where `WB_HOOK` is set), netdiag emits a `[WORKBOARD-CMD]`
line that the Control UI turns into a card on the `network` board:

```
[WORKBOARD-CMD] {"action": "create_card", "board": "network", "status": "todo", "title": "symptom", "notes": "Root cause...\nFix...\nConfidence: ..."}
```

### 5. Resolution

Netdiag **diagnoses, notifies, and recommends** — it never auto-applies changes.
A human (or another agent) applies the fix, and the monitor confirms recovery
when the state signature returns to normal.

### Data files

```
~/.local/var/log/
├── netdiag.log       # every tick + every state change
└── netdiag.events    # JSON state changes (script-friendly)
```

## Troubleshooting

### `bash: 404: command not found`

The download URL returned GitHub's 404 HTML page and bash tried to execute it.
Causes and fixes:

| Cause | Fix |
|-------|-----|
| Repo was private | Repo is public now — retry the download |
| Wrong URL host | Use `github.com/.../releases/download/...`, not `raw.githubusercontent.com` |
| Wrong tag | Tag must exist: `v1.0.0` (with the `v` prefix) |
| No release asset | Confirm with `gh release view v1.0.0 --repo jgonzalez007/netdiag` |

Always test the URL first:

```bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -L \
  https://github.com/josegonzalez007/netdiag/releases/download/v1.0.0/netdiag-1.0.0.tar.gz
# Expect: HTTP 200
```

Safer one-liner that fails loudly instead of piping a 404 into bash:

```bash
curl -fsSL https://github.com/josegonzalez007/netdiag/releases/download/v1.0.0/netdiag-1.0.0.tar.gz -o /tmp/netdiag.tar.gz || echo "download failed"
tar xzf /tmp/netdiag.tar.gz -C /tmp && /tmp/netdiag doctor
```

### `netdiag: command not found` after install

The PATH change only applies to new shells. Run `source ~/.zshrc`, or call the
full path: `~/.local/bin/netdiag doctor`.

### Missing optional tools

`dig`, `traceroute`, `arp`, and `curl` are optional. `netdiag doctor` reports
what's present. DNS falls back to `host`, then `getent`.

## Diagnostic Depth

Netdiag runs these probes in order:

1. **Interface detection** (`networksetup`, `ifconfig`)
2. **Service status** (WiFi, Ethernet, DHCP)
3. **Gateway resolution** (`default_gateway`, `ping`)
4. **DNS resolution** (`dig` + `host` + `getent`)
5. **Traceroute** (to 8.8.8.8)
6. **HTTP reachability** (HTTPS Google)
7. **Packet loss/latency** measurements

## State Signature

Netdiag tracks a 7-bit state signature:

```
wifi:     (service enabled?)
eth:      (service enabled?)
dns:      (DNS resolution?)
gw:       (default gateway reachable?)
http:     (HTTPS google.com?)
gw_ip:    (gateway IP)
```

Only changes that imply a *different* state generate a card/log entry.

## Root Cause Matrix

| Symptom | Diagnosis | Root Cause | Fix | Confidence |
|---------|-----------|------------|-----|------------|
| WiFi/down | No active interface | Both WiFi & Ethernet disabled | Enable WiFi or plug Ethernet | High |
| DNS fail | DNS lookup failed | Resolver problem | Flush cache, set 8.8.8.8, check router | High |
| Gateway unreachable | Cannot ping default route | Local link failure | Check cable, restart Wi-Fi, renew DHCP | High |
| HTTP fail | Ping works but HTTPS fails | Captive portal/firewall | VPN/test on other device | Medium |
| Packet loss | gw loss >0% | Signal/roaming/congestion | Move, 5GHz, restart Wi-Fi | High |
| No clear failure | All tests pass | Intermittent/application layer | Run watch to catch drops | Low |

## Workboard Integration

On OpenClaw hosts, netdiag emits a `[WORKBOARD-CMD]` line that can be processed by the Control UI:

```
[WORKBOARD-CMD] {"action": "create_card", "board": "network", "status": "todo", "title": "symptom", "notes": "Root cause...\nFix...\nConfidence: ..."}
```

## Limitations

- Requires root for `networksetup -getmacaddress` (not used)
- `system_profiler SPAirPortDataType` not used for lightweight RSSI
- No automated fix application (pure diagnostic)
- macOS-only monitoring launchd installation (may need sudo)

## Deployment Options

### 1. Single Machine

Copy `netdiag` into `~/.local/bin` and `chmod +x`.

### 2. Organization Wide

```bash
# Create a tarball
tar czf netdiag.tar.gz netdiag install.sh README.md com.jose.netdiag.plist
# Ship to users, who extract and run ./install.sh
```

### 3. System Deployment

Deploy via MDM, Jamf, or any asset management system.

### 4. GitHub Releases

Tag releases with `netdiag VERSION.tar.gz` and update README with download links.

## Development / Adding Platforms

The script uses a shell shebang; to add a new platform:

- Ensure required commands exist (`ping`, `dig`, `networksetup`)
- Adapt `probe_wifi`, `probe_ethernet` for service names
- For Linux, map OpenClaw-style `networksetup` to `nmcli`/`ip link` if needed
- Add a `--deep` flag that runs `system_profiler SPAirPortDataType` if available

## License

MIT

## About

Created by Dev-on for portable network troubleshooting.

## File Summary

- `netdiag` – main CLI script (single file)
- `install.sh` – helper installer (optional)
- `com.jose.netdiag.plist` – launchd template for continuous monitoring
- `README.md` – this guide

Note: This tool is actively developed. Contributions are welcome.

## Building a Tarball (optional)

```bash
mkdir -p dist && cp netdiag install.sh README.md com.jose.netdiag.plist dist/
cd dist && tar czf netdiag-${VERSION}.tar.gz *
cd ..
```

## Package Management (optional)

See `install.sh` for Homebrew / pkg package recipe ideas.

---

## Project Notes

**Path:** `/Users/josegonzalez/src/netdiag`
**Tags:** `network`, `diagnostics`, `troubleshooting`, `macos`, `bash`
**Memory:** `<!-- project: path:/Users/josegonzalez/src/netdiag -->`

Release process:
- Update version in the script header
- Tag in git
- Update README with new version download link
- Build tarball

Portability Testing:
- macOS: fully tested on 12.0+ (works on 10.9+ but optional tools may be missing)
- Linux: works if `networksetup` is aliased to `nmcli`, or install a shim
- Windows: not supported (needs Windows Subsystem for Linux, which is supported by WSL2).