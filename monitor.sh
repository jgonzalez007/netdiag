#!/usr/bin/env bash
# =============================================================================
# netdiag monitor — Continuous background network & VPN watcher for macOS
# 
# Purpose: Run in the background, detect issues, alert when problems occur.
#          Designed to run as a standalone daemon from launchd or manually.
#
# Usage:
#   Background mode (daemon):
#     # Install as daemon:
#     curl -fsSL https://raw.githubusercontent.com/jgonzalez007/netdiag/main/get.sh | bash
#     
#     # Or run it directly (requires root for launchd):
#     sudo /path/to/netdiag/monitor.sh --daemon
# 
#   Manual mode:
#     /path/to/netdiag/monitor.sh --daemon &  # Start in background
#     /path/to/netdiag/monitor.sh              # Stop by name
#
# Logging: ~/Library/Logs/jgonzalez007/network-doctor.log
# Settings: ~/Library/Application Support/jgonzalez007/network-doctor.conf
# =============================================================================

set -u
VERSION="1.0.0"

# ---- configuration ----------------------------------------------------------

APP_NAME="Network Doctor"
AUTHOR="jgonzalez007"
APP_DIR="$HOME/Library/Application Support/${AUTHOR}/network-doctor"
LOG_DIR="$HOME/Library/Logs/jgonzalez007"
LOG_FILE="${LOG_DIR}/network-doctor.log"
STATE_FILE="${APP_DIR}/monitor.state"
CONF_FILE="${APP_DIR}/monitor.conf"

DEFAULT_INTERVAL=60         # seconds between checks
DEFAULT_ALERT_ENABLED=1     # show notifications
DEFAULT_LOG_LEVEL="INFO"   # DEBUG|INFO|WARN|ERROR

# ---- helpers ----------------------------------------------------------------

log() {
    local level="$1" msg="$2" ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[%s] [%s] %s\n' "$ts" "$level" "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

log_debug() { [ "$DEFAULT_LOG_LEVEL" = "DEBUG" ] && log "DEBUG" "$1"; }
log_info()   { log "INFO" "$1"; }
log_warn()   { log "WARN" "$1"; }
log_error()  { log "ERROR" "$1"; }

alert() {
    local msg="$1" level="${2:-WARNING}"
    if [ "${DEFAULT_ALERT_ENABLED:-1}" = "0" ]; then return 0; fi
    # Use macOS notifications
    osascript -e "display notification \"${msg}\" with title \"${APP_NAME}\" with description \"[\"${level}]\:"" 2>/dev/null || true
    log_error "[ALERT] ${msg}"
}

is_root_or_daemon() {
    # Returns true if running as root or from launchd (no TTY)
    [ "$(id -u)" = "0" ] || [ ! -t 1 ]
}

cleanup() {
    local name="$1" pid="$2"
    log_debug "Cleaning up: ${name} (${pid})"
    kill "$pid" 2>/dev/null || true
    # Remove stale PID files
    rm -f "${STATE_FILE}.d/*" 2>/dev/null || true
    log_info "Cleanup complete."
}

trap 'cleanup "monitor" $$' INT TERM

# ---- network checks ---------------------------------------------------------

check_network() {
    local status="healthy" details=""
    
    # Check WiFi/Ethernet
    local wifi_up=0 eth_up=0
    if have networksetup; then
        for svc in $(networksetup -listallnetworkservices 2>/dev/null | grep -v ".*:"); do
            case "$svc" in
                *Wi-Fi*|*WiFi*) [ "$(networksetup -getinfo "$svc" 2>/dev/null | grep -c 'IP address')" = "1" ] && wifi_up=1 ;;
                *Ethernet*|*"Thunderbolt Bridge"*) [ "$(networksetup -getinfo "$svc" 2>/dev/null | grep -c 'IP address')" = "1" ] && eth_up=1 ;;
            esac
        done
    fi
    
    if [ "$wifi_up" = "0" ] && [ "$eth_up" = "0" ]; then
        status="no_connection"
        details="No active interfaces (WiFi/Ethernet)"
    elif [ "$wifi_up" = "0" ]; then
        status="no_wifi"
        details="Wi‑Fi not available or disabled"
    elif [ "$eth_up" = "0" ]; then
        status="no_ethernet"
        details="Ethernet not available or disabled"
    fi
    
    # Check DNS resolution
    if ! have dig; then
        if [ "$(getent hosts google.com 2>/dev/null | cut -d' ' -f1)" = "google.com" ]; then
            :  # OK
        else
            status="dns_failure"
            details="${details:+$details; }DNS resolution failed"
        fi
    elif ! have dig || ! dig +time=3 google.com >/dev/null 2>&1; then
        if [ "$status" = "healthy" ]; then
            status="dns_failure"
            details="DNS resolution failed"
        else
            details="${details:+$details; }DNS resolution failed"
        fi
    fi
    
    # Check gateway
    local gw="$("/usr/sbin/netstat -rn | awk '\$1=="default" {print \$2}' | head -1)"
    if [ -n "$gw" ]; then
        if ! ping -c 2 -W 3 "$gw" >/dev/null 2>&1; then
            status="gateway_unreachable"
            details="${details:+$details; }Gateway ${gw} unreachable"
        elif [ -z "$status" ]; then
            # Check latency
            local gw_lat
            read -r gw_loss gw_lat < <(ping -c 3 -W 2000 "$gw" 2>/dev/null | grep 'round-trip' | awk '{split($3, a, /=/); print a[2]; exit}')
            if [ -n "$gw_lat" ] && (( $(echo "$gw_lat > 150" | bc -l 2>/dev/null || echo 0) )); then
                status="high_latency"
                details="${details:+$details; }Gateway latency: ${gw_lat}ms (high)"
            fi
        fi
    else
        status="no_gateway"
        details="${details:+$details; }No gateway configured"
    fi
    
    # Check HTTP/HTTPS
    if ! have curl; then
        if ! ping -c 1 -W 5 google.com >/dev/null 2>&1; then
            if [ "$status" = "healthy" ]; then
                status="http_failure"
                details="${details:+$details; }HTTP/HTTPS unreachable"
            else
                details="${details:+$details; }HTTP/HTTPS unreachable"
            fi
        fi
    elif ! curl -sS --connect-timeout 5 https://google.com >/dev/null 2>&1; then
        if [ "$status" = "healthy" ]; then
            status="http_failure"
            details="${details:+$details; }HTTP/HTTPS unreachable"
        else
            details="${details:+$details; }HTTP/HTTPS unreachable"
        fi
    fi
    
    echo "$status"
    echo "$details" >&2
}

# ---- VPN checks (Cisco AnyConnect, OpenVPN, StrongSWAN, etc.) --------------

check_vpn() {
    local status="none" details="" count=0
    
    # Cisco AnyConnect
    if [ -d "/opt/cisco/AnyConnect" ] || [ -d "/Applications/AnyConnect.app" ]; then
        if ps aux | grep -i "anyconnect\|client" 2>/dev/null | grep -v grep >/dev/null; then
            status="anyconnect_active"
            count=$((count+1))
            # Check if tunnel is up (simplified check)
            local tun_up=0
            for iface in $(ls /var/run/tun* 2>/dev/null); do
                [ -c "$iface" ] && tun_up=1
            done
            if [ "$tun_up" = "1" ]; then
                details="${details:+$details}AnyConnect tunnel: UP (TUN device active)"
            else
                details="${details:+$details}AnyConnect process running, but TUN device not detected"
            fi
        fi
    fi
    
    # OpenVPN / StrongVPN / Other VPNs with tun/tap
    if ls /var/run/tun* 2>/dev/null | grep -q '/var/run'; then
        for tun_iface in $(ls /var/run/tun* 2>/dev/null); do
            [ -c "$tun_iface" ] || continue
            # Count active TUN interfaces
            count=$((count+1))
            if ps aux | grep "openvpn\|strongswan\|ipsec" >/dev/null 2>&1; then
                if [ "$status" != "anyconnect_active" ]; then
                    status="other_vpn_active"
                    details="${details:+$details}Other VPN active: ${count} TUN interface(s) found"
                fi
            else
                if [ "$status" = "none" ]; then
                    status="tun_device_detected"
                    details="${details:+$details}TUN device detected (${count}), but no associated VPN process"
                fi
            fi
        done
    fi
    
    echo "$status"
    echo "$details" >&2
}

# ---- main monitoring loop ---------------------------------------------------

daemon_mode() {
    local interval="${1:-$DEFAULT_INTERVAL}" run_num=0
    log_info "Starting daemon mode (interval: ${interval}s, alerting: enabled)"
    log_debug "PID $$ will run as daemon with interval=${interval}s"
    
    # Save PID for cleanup
    echo "$$" > "${STATE_FILE}.d/daemon.pid" 2>/dev/null || true
    
    local last_status="healthy" last_status_details=""
    local start_time=$(date '+%Y-%m-%d %H:%M:%S')
    
    log_info "Monitoring started: ${start_time}"
    
    while is_root_or_daemon; do
        sleep "$interval"
        run_num=$((run_num+1))
        
        local now_status status_details
        now_status=$(check_network)
        local last_status_out
        read -r last_status last_status_details <<< "$(echo "$now_status")"
        
        # Compare status
        if [ "$now_status" != "$last_status" ]; then
            local change_type=""
            case "$now_status:$last_status" in
                *healthy*:*no_*|"healthy":*) change_type="restored" ;;
                *no_*:healthy|*no_*:http_failure) change_type="lost" ;;
                http_failure:*healthy|"http_failure":*) change_type="restored" ;;
                dns_failure:*dns_failure|"dns_failure":*) change_type="changed" ;;
                *) change_type="changed" ;;
            esac
            
            log_info "Network status changed at $(date '+%Y-%m-%d %H:%M:%S'): ${last_status} → ${now_status}"
            
            # Alert on critical changes
            if [ "$change_type" = "lost" ]; then
                alert "⚠️  Network connectivity issue detected: ${now_status:-Unknown} — Previous: ${last_status:-healthy}" "CRITICAL"
                log_error "ALERT: Connectivity lost. New status: ${now_status}. Details: $status_details"
            elif [ "$change_type" = "restored" ]; then
                alert "✅  Network connectivity restored! Status: ${now_status:-healthy} — Previous: ${last_status:-unhealthy}" "OK"
                log_info "ALERT: Connectivity restored. New status: ${now_status}. Details: $status_details"
            elif [ "$change_type" = "changed" ]; then
                alert "🔍  Network status updated: ${now_status} — Previous: ${last_status}" "INFO"
                log_info "Status changed to ${now_status}: $status_details"
            fi
            
            last_status="$now_status"
            last_status_details="$status_details"
        else
            log_debug "tick #{run_num}: status unchanged (${now_status})"
        fi
    done
    
    # Cleanup on normal exit
    local end_time=$(date '+%Y-%m-%d %H:%M:%S')
    log_info "Daemon stopped: ${end_time} (ranks: ${run_num}, uptime: $(( ($(date +%s) - $(date -d "$start_time" '+%s')) / 60 )) minutes)"
    
    # Remove PID file
    rm -f "${STATE_FILE}.d/daemon.pid" 2>/dev/null || true
    
    log_info "Cleanup complete."
}

help_monitor() {
    cat <<'HELP'
netdiag monitor — Network & VPN watcher

USAGE:
  # Start as daemon (background):
  curl -fsSL https://raw.githubusercontent.com/jgonzalez007/netdiag/main/get.sh | bash
  
  # Or run directly with sudo (recommended for launchd):
  sudo /path/to/netdiag/monitor.sh --daemon [--interval N]
  
  # Stop by name (requires root or correct owner):
  sudo launchctl bootout gui/\$(id -u)/jgonzalez007.network-doctor

SETTINGS:
  ~/Library/Application Support/jgonzalez007/network-doctor/monitor.conf
  
LOGS:
  ~/Library/Logs/jgonzalez007/network-doctor.log

OPTIONS:
  --daemon         Run as background daemon (requires sudo)
  --interval N     Check every N seconds (default: 60, min: 10)
  --no-alert       Disable push notifications and sound
  --debug          Enable debug logging (shows in log file)

EXAMPLES:
  sudo /usr/local/bin/netdiag monitor.sh --daemon --interval 30
  sudo /usr/local/bin/netdiag monitor.sh --daemon --no-alert --interval 120

HELP
}

# ---- main entry point -------------------------------------------------------

is_root_or_daemon && shift || { echo "This script must run with sudo (for background daemon mode)"; exit 1; }

case "${1:-}" in
    --daemon|"")
        interval="${2:-$DEFAULT_INTERVAL}"
        alert_enabled="${3:-1}"
        shift 2 || true
        
        # Parse options
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --interval) interval="$2"; shift 2 ;;
                --no-alert) alert_enabled=0; shift ;;
                --debug) log_level="DEBUG"; shift ;;
                *) echo "Unknown option: $1"; exit 2 ;;
            esac
        done
        
        # Set defaults
        DEFAULT_INTERVAL="$interval"
        DEFAULT_ALERT_ENABLED="$alert_enabled"
        DEFAULT_LOG_LEVEL="${log_level:-INFO}"
        
        daemon_mode "$DEFAULT_INTERVAL"
        ;;
    --help|-h|help)
        help_monitor
        ;;
    *)
        echo "Unknown option: $1"; exit 2
        ;;
esac
