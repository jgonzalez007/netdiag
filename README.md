# Netdiag — Portable Network Troubleshooting Agent

A zero-dependency macOS (and mostly Linux) CLI tool for diagnosing WiFi/Ethernet connectivity, detecting drops, and proposing fixes.

## Quick Start

**Install on any Mac:** open **Terminal** (Spotlight → "Terminal"), paste this line, press Return:

```bash
curl -fsSL https://raw.githubusercontent.com/jgonzalez007/netdiag/main/get.sh | bash
```

Network Doctor opens in your browser. Next time, open **Network Doctor** from Spotlight or Launchpad.
No admin password needed. The first time, macOS may ask to install its Command Line Tools:
click **Install**, wait, then paste the line again. Running it again also updates to the latest version.

Uninstall: `launchctl bootout gui/$(id -u)/com.jgonzalez007.network-doctor; rm -rf ~/.netdiag "$HOME/Applications/Network Doctor.app"`

**Command line only** (for scripting):

```bash
git clone https://github.com/jgonzalez007/netdiag.git && cd netdiag && ./install.sh
netdiag check "WiFi drops after sleep"
```

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

### Web Dashboard (Network Doctor)

```bash
netdiag-web        # or: python3 netdiag-web.py   → http://127.0.0.1:8765
```

Pick a symptom (including **VPN Issues**) and it runs the quick test, then:

- **Deep checks** (always, ~11s, no root): path MTU, IPv6 re-configuration rate,
  last-hour Wi-Fi quality (RSSI, beacon loss, retries, channel load), ISP gateway
  (T-Mobile 5G Home Internet status API: model, signal, uptime), public-IP
  stability, power state, VPN tunnels and Cisco AnyConnect reconnects.
- **Traffic capture** (only when the quick test passes, 15s): retransmits,
  out-of-order data, slow connections and interface errors via `nettop`/`netstat`.
  Started with `sudo`, it also saves a `.pcap` to `~/.netdiag/captures/`.

Typical VPN finding on 5G/CGNAT ISPs: path MTU below 1500 and frequent IPv6
re-configuration, both of which make AnyConnect reconnect.
Run the dashboard **on the computer that has the problem**: Wi-Fi, power and VPN checks are local.
macOS only; needs `python3` (Xcode Command Line Tools).

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

### The installer stops with "needs Apple's Command Line Tools"

Click **Install** in the window macOS opens, wait for it to finish (a few minutes),
then paste the install line again.

### Network Doctor doesn't open

Check the server log: `cat ~/.netdiag/web.log`. Port 8765 must be free
(`lsof -nP -iTCP:8765`). Starting it by hand shows errors directly:
`python3 ~/.netdiag/app/netdiag-web.py`.

### The username in URLs

The GitHub account is `jgonzalez007`. Older copies of this README used `josegonzalez007`, which 404s.

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