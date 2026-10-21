#!/usr/bin/env python3
"""
netdiag-web — Friendly web dashboard for network diagnostics
Run: python3 netdiag-web.py
Opens: http://localhost:8765
"""

import json
import os
import re
import subprocess
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

def run_netdiag(args):
    try:
        result = subprocess.run(
            ["./netdiag"] + args,
            capture_output=True, text=True, timeout=60,
            cwd=str(Path(__file__).parent),
        )
        if "report" in args and "--json" in args:
            return json.loads(result.stdout)
        return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "Timed out after 60s"}
    except FileNotFoundError:
        return {"error": "netdiag not found — place netdiag-web.py next to netdiag"}
    except json.JSONDecodeError:
        return {"error": "Could not parse output", "raw": result.stdout}
    except Exception as e:
        return {"error": str(e)}

NETTOP_COLS = "interface,state,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx,rtt_avg"
CAPTURE_DIR = Path.home() / ".netdiag" / "captures"

def _int(s):
    try: return int(s)
    except (TypeError, ValueError): return 0

def fmt_bytes(n):
    return f"{n / 1e6:.1f} MB" if n > 1e6 else f"{n / 1e3:.1f} KB" if n > 1e3 else f"{n} B"

def primary_interface():
    out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "interface:" in line:
            return line.split(":", 1)[1].strip()
    return "en0"

def default_gateway():
    out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True).stdout
    m = re.search(r"gateway:\s*(\S+)", out)
    return m.group(1) if m else None

def iface_counters(iface):
    """Ipkts, Ierrs, Opkts, Oerrs from the link-level row of netstat -ibn."""
    out = subprocess.run(["netstat", "-ibn", "-I", iface], capture_output=True, text=True).stdout
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 10 and f[2].startswith("<Link"):
            return {"ipkts": _int(f[4]), "ierrs": _int(f[5]), "opkts": _int(f[7]), "oerrs": _int(f[8])}
    return {}

def run_tcpdump(iface, seconds):
    """Full packet capture. Needs BPF access (root or Wireshark's ChmodBPF); returns None without it."""
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    pcap = CAPTURE_DIR / time.strftime("capture-%Y%m%d-%H%M%S.pcap")
    p = subprocess.Popen(["tcpdump", "-i", iface, "-nn", "-q", "-w", str(pcap)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(seconds)
    if p.poll() is not None:  # exited early: permission denied
        return None
    p.terminate()
    p.wait(timeout=5)
    out = subprocess.run(["tcpdump", "-r", str(pcap), "-nn", "-q"], capture_output=True, text=True).stdout
    hosts = {}
    lines = out.splitlines()
    for line in lines:
        f = line.split()
        if len(f) >= 5 and f[1] == "IP":
            for ep in (f[2], f[4].rstrip(":")):
                h = ep.rsplit(".", 1)[0]
                hosts[h] = hosts.get(h, 0) + 1
    top = sorted(hosts.items(), key=lambda kv: -kv[1])[:5]
    return {"pcap": str(pcap), "packets": len(lines), "top_hosts": [{"host": h, "packets": n} for h, n in top]}

def run_capture(seconds=10):
    """Watch live traffic for `seconds` and flag retransmits, reordering, slow RTT and interface errors.
    The nettop/netstat part needs no root; tcpdump is added when BPF is readable."""
    iface = primary_interface()
    before = iface_counters(iface)
    pcap_result = {}
    t = threading.Thread(target=lambda: pcap_result.update(r=run_tcpdump(iface, seconds)))
    t.start()
    out = subprocess.run(["nettop", "-m", "tcp", "-d", "-x", "-L", "2", "-s", str(seconds), "-J", NETTOP_COLS],
                         capture_output=True, text=True, timeout=seconds + 20).stdout
    t.join()
    after = iface_counters(iface)

    # nettop -d prints two samples; the second is the delta over the interval.
    blocks = out.split(",interface,")
    rows = blocks[-1].splitlines()[1:] if len(blocks) > 2 else []
    procs, conns, proc = {}, [], None
    for row in rows:
        f = row.split(",")
        if len(f) < 9:
            continue
        name, ifc, state = f[0], f[1], f[2]
        bin_, bout, dupe, ooo, retx = (_int(x) for x in f[3:8])
        rtt = float(f[8].split()[0]) if f[8].strip() else None
        if not ifc and not state:  # process row
            proc = name.rsplit(".", 1)[0]
            procs[proc] = procs.get(proc, 0) + bin_ + bout
        elif state == "Established":
            # nettop's rx_dupe / rx_ooo / re-tx columns are byte counts, not packets.
            conns.append({"proc": proc, "conn": name.split(" ", 1)[-1], "iface": ifc,
                          "bytes": bin_ + bout, "bytes_in": bin_, "bytes_out": bout,
                          "dupe": dupe, "ooo": ooo, "retx": retx, "rtt_ms": rtt})

    total_bytes = sum(procs.values())
    retx = sum(c["retx"] for c in conns)
    ooo = sum(c["ooo"] for c in conns)
    dupe = sum(c["dupe"] for c in conns)
    # Idle connections keep a stale RTT estimate, so only judge ones that moved data during the capture.
    active = [c for c in conns if c["bytes"] and c["rtt_ms"] is not None]
    rtts = [c["rtt_ms"] for c in active if c["iface"] == iface]
    avg_rtt = round(sum(rtts) / len(rtts), 1) if rtts else None
    slow = sorted((c for c in active if c["rtt_ms"] > 300), key=lambda c: -c["rtt_ms"])[:5]
    lossy = sorted((c for c in conns if c["retx"]), key=lambda c: -c["retx"])[:5]
    ierrs = after.get("ierrs", 0) - before.get("ierrs", 0)
    oerrs = after.get("oerrs", 0) - before.get("oerrs", 0)
    pkts = (after.get("ipkts", 0) - before.get("ipkts", 0)) + (after.get("opkts", 0) - before.get("opkts", 0))

    findings = []
    if ierrs or oerrs:
        findings.append(f"{ierrs} input / {oerrs} output errors on {iface} — likely a bad cable, port or WiFi interference.")
    sent = sum(c["bytes_out"] for c in conns)
    recvd = sum(c["bytes_in"] for c in conns)
    retx_rate = retx / sent if sent else 0
    ooo_rate = ooo / recvd if recvd else 0
    # Rates on a few KB of traffic are noise; require a meaningful amount before calling it loss.
    if retx_rate > 0.02 and retx >= 10_000:
        findings.append(f"{fmt_bytes(retx)} retransmitted ({retx_rate:.1%} of data sent) — packet loss somewhere on the path.")
    if ooo_rate > 0.02 and ooo >= 10_000:
        findings.append(f"{fmt_bytes(ooo)} arrived out of order ({ooo_rate:.1%} of data received) — the path is reordering traffic (often VPN or congested WiFi).")
    if slow:
        findings.append(f"{len(slow)} connection(s) with RTT over 300 ms — slowest: {slow[0]['proc']} ({slow[0]['rtt_ms']:.0f} ms).")
    if not total_bytes and not pkts:
        findings.append("No traffic seen during the capture — reproduce the problem while it runs.")

    return {
        "seconds": seconds, "iface": iface, "packets": pkts, "bytes": total_bytes,
        "retransmit_bytes": retx, "retransmit_rate": round(retx_rate, 4),
        "out_of_order_bytes": ooo, "out_of_order_rate": round(ooo_rate, 4),
        "duplicate_bytes": dupe, "avg_rtt_ms": avg_rtt,
        "iface_errors": {"in": ierrs, "out": oerrs},
        "top_processes": [{"proc": p, "bytes": b} for p, b in sorted(procs.items(), key=lambda kv: -kv[1])[:5] if b],
        "lossy_connections": lossy, "slow_connections": slow,
        "tcpdump": pcap_result.get("r"), "findings": findings,
    }

# ---------------------------------------------------------------------------
# Deep checks: things a one-shot ping/DNS test can't see, but that make VPNs
# (AnyConnect, IPsec, WireGuard) connect and disconnect. All run without root.
# ---------------------------------------------------------------------------

def _sh(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

def _ping_ok(size, target):
    # -D sets don't-fragment. IPv4 only: ping6's equivalent needs root. A timeout counts as "too big".
    args = ["ping", "-D", "-c", "1", "-t", "2", "-s", str(size), target]
    try:
        return subprocess.run(args, capture_output=True, timeout=4).returncode == 0
    except subprocess.TimeoutExpired:
        return False

def _curl(url, timeout=5, local=False):
    # System curl, not urllib: python.org / CLT Pythons often lack CA certs, and a LAN
    # address must bypass any configured HTTP proxy.
    args = ["curl", "-s", "-f", "-m", str(timeout)] + (["--noproxy", "*"] if local else []) + [url]
    return _sh(args, timeout + 2).strip() or None

def path_mtu():
    """Largest IPv4 packet that crosses the ISP unfragmented (binary search with DF pings)."""
    target, overhead = "1.1.1.1", 28
    try:
        if not _ping_ok(56, target):
            return None
        lo, hi = 1200 - overhead, 1500 - overhead
        if _ping_ok(hi, target):
            return 1500
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _ping_ok(mid, target): lo = mid
            else: hi = mid
        return lo + overhead
    except Exception:
        return None

def ipv6_churn(iface):
    """How often macOS re-applied IPv6 config in the last hour. VPN clients can see each one as a network change."""
    out = _sh(["/usr/bin/log", "show", "--last", "1h", "--style", "compact", "--predicate",
               f'process == "configd" AND eventMessage CONTAINS "AUTOMATIC-V6 {iface}: publish"'], 60)
    return sum(1 for l in out.splitlines() if "publish" in l)

def wifi_quality():
    """Summarize the last hour of airportd link-quality samples (every ~5s)."""
    out = _sh(["/usr/bin/log", "show", "--last", "1h", "--style", "compact", "--predicate",
               'process == "airportd" AND eventMessage CONTAINS "LQM: rssi"'], 60)
    def nums(key):
        return [int(m) for m in re.findall(rf"\b{key}=(-?\d+)", out)]
    rssi, noise = nums("rssi"), nums("noise")
    if not rssi:
        return None
    cca = [float(m) for m in re.findall(r"\bcca=([\d.]+)%", out)]
    recv, sched = sum(nums("beaconRecv")), sum(nums("beaconSched"))
    tx, fail, retr = sum(nums("txFrames")), sum(nums("txFail")), sum(nums("txRetrans"))
    return {
        "samples": len(rssi), "rssi_min": min(rssi), "rssi_avg": round(sum(rssi) / len(rssi)),
        "noise_avg": round(sum(noise) / len(noise)) if noise else None,
        "cca_max": max(cca) if cca else None,
        "beacon_loss": round(1 - recv / sched, 3) if sched else None,
        "tx_fail_rate": round(fail / tx, 4) if tx else None,
        "tx_retry_rate": round(retr / tx, 4) if tx else None,
    }

def wifi_link():
    out = _sh(["system_profiler", "SPAirPortDataType"], 30)
    block = out.split("Current Network Information:", 1)[-1].split("Other Local", 1)[0] if "Current Network" in out else ""
    info = {}
    for key in ("PHY Mode", "Channel", "Signal / Noise", "Transmit Rate", "Security"):
        m = re.search(rf"{re.escape(key)}:\s*(.+)", block)
        if m: info[key] = m.group(1).strip()
    return info

def isp_gateway(gw):
    """T-Mobile 5G Home Internet gateways expose an unauthenticated status API; others just return None."""
    if not gw:
        return None
    try:
        d = json.loads(_curl(f"http://{gw}/TMI/v1/gateway?get=all", 3, local=True) or "")
    except ValueError:
        return None
    dev, sig = d.get("device", {}), d.get("signal", {})
    radio = sig.get("5g") or sig.get("4g") or {}
    return {
        "isp": "T-Mobile 5G Home Internet", "model": f"{dev.get('manufacturer', '')} {dev.get('model', '')}".strip(),
        "uptime_h": round(d.get("time", {}).get("upTime", 0) / 3600, 1),
        "tech": "5G" if sig.get("5g") else "4G" if sig.get("4g") else "?",
        "bands": radio.get("bands"), "bars": radio.get("bars"),
        "rsrp": radio.get("rsrp"), "rsrq": radio.get("rsrq"), "sinr": radio.get("sinr"), "cid": radio.get("cid"),
    }

def public_ipv4():
    return _curl("https://api.ipify.org")

def power_state():
    pm = _sh(["pmset", "-g"])
    batt = _sh(["pmset", "-g", "batt"])
    get = lambda k: (re.search(rf"^\s*{k}\s+(\d+)", pm, re.M) or [None, None])[1]
    return {"source": "battery" if "Battery Power" in batt else "AC",
            "low_power_mode": get("lowpowermode") == "1", "sleep_min": get("sleep")}

def vpn_state():
    utun = []
    cur = None
    for line in _sh(["ifconfig"]).splitlines():
        if re.match(r"^\S", line):
            cur = line.split(":")[0]
        elif cur and cur.startswith(("utun", "ipsec", "ppp")) and line.strip().startswith("inet "):
            utun.append(cur)
    anyconnect = next((p for p in ("/opt/cisco/secureclient", "/opt/cisco/anyconnect") if Path(p).exists()), None)
    reconnects = None
    if anyconnect:
        out = _sh(["/usr/bin/log", "show", "--last", "24h", "--style", "compact", "--predicate",
                   'process BEGINSWITH "vpnagent" OR process BEGINSWITH "acvpnagent"'], 120)
        reconnects = sum(1 for l in out.splitlines() if re.search(r"reconnect", l, re.I))
    return {"tunnels": sorted(set(utun)), "anyconnect": anyconnect, "anyconnect_reconnects_24h": reconnects}

def run_deep_checks():
    iface, gw = primary_interface(), default_gateway()
    jobs = {
        "mtu4": path_mtu,
        "churn": lambda: ipv6_churn(iface), "wifi_q": wifi_quality, "wifi": wifi_link,
        "isp": lambda: isp_gateway(gw), "ip1": public_ipv4, "power": power_state, "vpn": vpn_state,
    }
    res = {}
    threads = [threading.Thread(target=lambda k=k, f=f: res.__setitem__(k, f())) for k, f in jobs.items()]
    for t in threads: t.start()
    for t in threads: t.join()
    ip2 = public_ipv4()
    r = {"iface": iface, "gateway": gw, "path_mtu_v4": res["mtu4"],
         "ipv6_changes_1h": res["churn"], "wifi_quality": res["wifi_q"], "wifi_link": res["wifi"],
         "isp_gateway": res["isp"], "public_ipv4": res["ip1"],
         "public_ipv4_stable": None if not (res["ip1"] and ip2) else res["ip1"] == ip2,
         "power": res["power"], "vpn": res["vpn"]}

    findings = []  # (severity, title, detail, fix)
    mtu = r["path_mtu_v4"]
    if mtu and mtu < 1500:
        vpn_mtu = min(1400, mtu - 16) // 10 * 10
        findings.append(("high" if mtu < 1450 else "medium", f"Reduced path MTU: {mtu} bytes (normal is 1500)",
            "Full-size packets are dropped instead of fragmented on this connection. VPN tunnels (AnyConnect DTLS, IPsec) "
            "size themselves for 1500 and stall, which looks like the VPN dropping and reconnecting.",
            f"On the VPN computer set the network adapter MTU to {vpn_mtu}. Windows (admin): "
            f"netsh interface ipv4 set subinterface \"Wi-Fi\" mtu={vpn_mtu} store=persistent · macOS: "
            f"sudo networksetup -setMTU Wi-Fi {vpn_mtu}. Or ask the VPN admin to lower the AnyConnect MTU to ~{vpn_mtu - 100}."))
    churn = r["ipv6_changes_1h"]
    if churn and churn > 12:
        findings.append(("high" if churn > 30 else "medium", f"IPv6 settings re-applied {churn}× in the last hour",
            "The router keeps re-announcing IPv6. AnyConnect and other VPN clients can treat each one as a network change and reconnect.",
            "Test by turning IPv6 off on the VPN computer for a day (Windows: untick IPv6 in the adapter properties; "
            "macOS: networksetup -setv6off Wi-Fi). If the drops stop, update the router firmware or disable IPv6 on it."))
    isp = r["isp_gateway"]
    if isp:
        findings.append(("medium", f"{isp['isp']} ({isp['model']}) — carrier-grade NAT",
            "IPv4 goes through T-Mobile's shared NAT, which drops idle UDP mappings quickly and can change your public IP on cell handoff. "
            f"Signal: {isp['tech']} {isp['bars']} bars, RSRP {isp['rsrp']}, SINR {isp['sinr']}; gateway up {isp['uptime_h']} h.",
            "Keep VPN keepalives/DPD under 30s. If the VPN supports it, prefer TCP 443 or an IPv6 server address. "
            "Move the gateway toward a window to improve SINR (>13 is good)."))
        if isp.get("sinr") is not None and isp["sinr"] < 5:
            findings.append(("high", f"Weak cellular signal (SINR {isp['sinr']})",
                "A low signal-to-noise ratio on the 5G/LTE link causes packet loss and tower handoffs.",
                "Move the gateway higher or nearer a window facing the tower."))
    if r["public_ipv4_stable"] is False:
        findings.append(("high", "Public IP address changed during the test",
            "Your internet-facing IPv4 changed within seconds. Any VPN session breaks when this happens.",
            "Contact the ISP; on CGNAT services this usually follows cell handoffs or gateway reconnects."))
    q = r["wifi_quality"]
    if q:
        if q["rssi_min"] < -75:
            findings.append(("medium", f"Weak Wi-Fi at times (worst {q['rssi_min']} dBm, avg {q['rssi_avg']} dBm)",
                "Signal dropped below -75 dBm in the last hour, where packet loss and roaming begin.",
                "Move closer to the access point or add a mesh node / use Ethernet."))
        # ~10-15% "missed" is normal on a healthy macOS link (the chip skips scheduled beacons); only flag well above that.
        if q["beacon_loss"] is not None and q["beacon_loss"] > 0.3:
            findings.append(("medium", f"Access point beacons missed: {q['beacon_loss']:.0%}",
                "The Mac is missing the AP's heartbeat frames — interference, AP overload or a failing AP. Enough misses causes a disconnect.",
                "Change the AP channel, reboot/update the AP, or test on Ethernet."))
        if q["tx_retry_rate"] is not None and q["tx_retry_rate"] > 0.15:
            findings.append(("medium", f"High Wi-Fi retry rate ({q['tx_retry_rate']:.0%})",
                "Many frames need resending — channel congestion or interference.", "Try a less crowded channel or the 5/6 GHz band."))
        if q["cca_max"] is not None and q["cca_max"] > 60:
            findings.append(("medium", f"Busy Wi-Fi channel (peaks at {q['cca_max']:.0f}% airtime)",
                "Other networks or devices are using most of the channel.", "Pick a less crowded channel on the AP."))
    p = r["power"]
    if p["source"] == "battery" or p["low_power_mode"]:
        findings.append(("low", "Running on battery / Low Power Mode",
            "macOS saves power on Wi-Fi when on battery, which can add latency and slow roaming.",
            "Test again plugged in with Low Power Mode off."))
    v = r["vpn"]
    if v["anyconnect_reconnects_24h"]:
        findings.append(("medium", f"AnyConnect logged {v['anyconnect_reconnects_24h']} reconnect events in 24h",
            "Matches the issues above; compare timestamps in the AnyConnect Message History.",
            "Collect a DART bundle from the AnyConnect client for the VPN admin."))
    r["findings"] = [{"severity": s, "title": t, "detail": d, "fix": f} for s, t, d, f in findings]
    return r

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>netdiag — Network Doctor</title>
<style>
  :root {
    --bg: #f5f5f7; --card: #fff; --text: #1d1d1f; --muted: #86868b;
    --accent: #0071e3; --accent-hover: #0077ed;
    --ok: #34c759; --ok-bg: #d1fadf; --ok-text: #0e8031;
    --warn: #ff9f0a; --warn-bg: #fff4d6; --warn-text: #965e00;
    --fail: #ff3b30; --fail-bg: #ffd6d6; --fail-text: #b42318;
    --radius: 16px; --radius-sm: 10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "SF Pro", sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 40px 20px 60px; }

  /* header */
  .logo { font-size: 40px; text-align: center; margin-bottom: 4px; }
  h1 { text-align: center; font-size: 26px; font-weight: 700; margin-bottom: 4px; }
  .tagline { text-align: center; color: var(--muted); font-size: 15px; margin-bottom: 36px; }

  /* stepper */
  .steps { display: flex; justify-content: center; gap: 0; margin-bottom: 32px; position: relative; }
  .step-item { display: flex; flex-direction: column; align-items: center; flex: 1; max-width: 140px; position: relative; z-index: 1; }
  .step-circle {
    width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; background: #e8e8ed; color: var(--muted); transition: all 0.3s;
  }
  .step-circle.active { background: var(--accent); color: white; }
  .step-circle.done { background: var(--ok); color: white; }
  .step-label { font-size: 11px; color: var(--muted); margin-top: 6px; font-weight: 500; text-align: center; }
  .step-label.active { color: var(--text); font-weight: 600; }
  .step-line { position: absolute; top: 18px; left: 15%; right: 15%; height: 2px; background: #e8e8ed; z-index: 0; }
  .step-line-fill { height: 2px; background: var(--accent); transition: width 0.4s; width: 0; }

  /* cards */
  .card { background: var(--card); border-radius: var(--radius); padding: 28px; box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 16px; }
  .card h2 { font-size: 18px; font-weight: 600; margin-bottom: 6px; }
  .card p { color: var(--muted); font-size: 14px; line-height: 1.5; }

  /* step panels */
  .panel { display: none; animation: fadeIn 0.3s; }
  .panel.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

  /* buttons */
  .btn {
    appearance: none; border: none; border-radius: var(--radius-sm); padding: 14px 28px;
    font-size: 15px; font-weight: 600; cursor: pointer; transition: all 0.15s;
  }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { background: var(--accent-hover); transform: translateY(-1px); }
  .btn-primary:disabled { background: #c7c7cc; cursor: not-allowed; transform: none; }
  .btn-outline { background: transparent; color: var(--accent); border: 1.5px solid var(--accent); }
  .btn-outline:hover { background: rgba(0,113,227,0.06); }
  .btn-row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }

  /* symptom cards */
  .symptom-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 16px 0; }
  .symptom-card {
    padding: 14px 16px; border: 1.5px solid #e8e8ed; border-radius: var(--radius-sm);
    cursor: pointer; transition: all 0.15s; font-size: 14px; text-align: center;
  }
  .symptom-card:hover { border-color: var(--accent); background: rgba(0,113,227,0.04); }
  .symptom-card.selected { border-color: var(--accent); background: rgba(0,113,227,0.08); font-weight: 600; }
  .symptom-card .emoji { font-size: 24px; display: block; margin-bottom: 6px; }

  /* custom input */
  .custom-input {
    width: 100%; padding: 14px 16px; border: 1.5px solid #d2d2d7; border-radius: var(--radius-sm);
    font-size: 15px; outline: none; margin-top: 12px;
  }
  .custom-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0,113,227,0.15); }

  /* scanning animation */
  .scan-container { text-align: center; padding: 40px 0; }
  .scan-icon { font-size: 64px; animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.1); } }
  .scan-status { font-size: 15px; color: var(--muted); margin-top: 16px; }
  .scan-checks { max-width: 360px; margin: 20px auto 0; text-align: left; }
  .check-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; font-size: 14px; }
  .check-icon { width: 22px; text-align: center; font-size: 16px; }
  .check-icon.pending { color: var(--muted); }
  .check-icon.running { color: var(--accent); animation: spin 0.8s linear infinite; }
  .check-icon.done { color: var(--ok); }
  .check-icon.fail { color: var(--fail); }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* results */
  .result-banner {
    display: flex; align-items: center; gap: 16px; padding: 20px;
    border-radius: var(--radius-sm); margin-bottom: 20px;
  }
  .result-banner.healthy { background: var(--ok-bg); }
  .result-banner.issue { background: var(--fail-bg); }
  .result-banner.warning { background: var(--warn-bg); }
  .result-banner .banner-icon { font-size: 36px; }
  .result-banner .banner-text h3 { font-size: 16px; font-weight: 600; }
  .result-banner .banner-text p { font-size: 13px; margin-top: 2px; }
  .result-banner.healthy .banner-text h3 { color: var(--ok-text); }
  .result-banner.issue .banner-text h3 { color: var(--fail-text); }
  .result-banner.warning .banner-text h3 { color: var(--warn-text); }

  .detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 16px 0; }
  .detail-item { padding: 14px; background: var(--bg); border-radius: var(--radius-sm); }
  .detail-item .label { font-size: 11px; color: var(--muted); text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }
  .detail-item .value { font-size: 15px; font-weight: 600; margin-top: 4px; }
  .detail-item .value.ok { color: var(--ok); }
  .detail-item .value.fail { color: var(--fail); }

  /* fix card */
  .fix-card {
    background: linear-gradient(135deg, #f0f4ff 0%, #e8f0fe 100%);
    border-radius: var(--radius-sm); padding: 20px; margin-top: 16px;
  }
  .fix-card h3 { font-size: 15px; font-weight: 600; margin-bottom: 8px; }
  .fix-card p { font-size: 14px; line-height: 1.6; color: #333; }
  .fix-card code { background: rgba(0,0,0,0.06); padding: 2px 6px; border-radius: 4px; font-size: 13px; }

  .footer { text-align: center; margin-top: 32px; color: var(--muted); font-size: 12px; }
  .footer a { color: var(--accent); text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">🔧</div>
  <h1>Network Doctor</h1>
  <p class="tagline">Diagnose WiFi &amp; Ethernet issues in 3 simple steps</p>

  <!-- Stepper -->
  <div class="steps">
    <div class="step-line"><div class="step-line-fill" id="stepLineFill"></div></div>
    <div class="step-item"><div class="step-circle active" id="sc1">1</div><div class="step-label active" id="sl1">Describe</div></div>
    <div class="step-item"><div class="step-circle" id="sc2">2</div><div class="step-label" id="sl2">Scan</div></div>
    <div class="step-item"><div class="step-circle" id="sc3">3</div><div class="step-label" id="sl3">Results</div></div>
    <div class="step-item"><div class="step-circle" id="sc4">4</div><div class="step-label" id="sl4">Fix</div></div>
  </div>

  <!-- STEP 1: Describe -->
  <div class="panel active" id="panel1">
    <div class="card">
      <h2>What's happening?</h2>
      <p>Pick the issue that best describes your problem, or type your own.</p>
      <div class="symptom-grid">
        <div class="symptom-card" onclick="selectSymptom(this, 'WiFi keeps disconnecting')">
          <span class="emoji">📶</span> WiFi keeps disconnecting
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'Internet is very slow')">
          <span class="emoji">🐌</span> Internet is very slow
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'No internet after sleep')">
          <span class="emoji">😴</span> No internet after sleep
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'Cannot load any website')">
          <span class="emoji">🚫</span> Can't load any website
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'Ethernet not working')">
          <span class="emoji">🔌</span> Ethernet not working
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'Connection drops during calls')">
          <span class="emoji">📞</span> Drops during calls
        </div>
        <div class="symptom-card" onclick="selectSymptom(this, 'VPN issues')">
          <span class="emoji">🔒</span> VPN Issues
        </div>
      </div>
      <input type="text" class="custom-input" id="customSymptom" placeholder="Or describe your issue here...">
      <div class="btn-row">
        <button class="btn btn-primary" id="startBtn" onclick="startScan()">Start Diagnosis →</button>
      </div>
    </div>
  </div>

  <!-- STEP 2: Scanning -->
  <div class="panel" id="panel2">
    <div class="card">
      <div class="scan-container">
        <div class="scan-icon">📡</div>
        <h2 style="margin-top:16px;">Scanning your network…</h2>
        <p class="scan-status" id="scanStatus">Starting diagnostics</p>
        <div class="scan-checks">
          <div class="check-row" id="chk-wifi"><span class="check-icon pending" id="ico-wifi">○</span> Checking WiFi interface</div>
          <div class="check-row" id="chk-eth"><span class="check-icon pending" id="ico-eth">○</span> Checking Ethernet</div>
          <div class="check-row" id="chk-gw"><span class="check-icon pending" id="ico-gw">○</span> Pinging gateway</div>
          <div class="check-row" id="chk-dns"><span class="check-icon pending" id="ico-dns">○</span> Testing DNS resolution</div>
          <div class="check-row" id="chk-http"><span class="check-icon pending" id="ico-http">○</span> Reaching the internet</div>
          <div class="check-row" id="chk-trace"><span class="check-icon pending" id="ico-trace">○</span> Tracing route</div>
        </div>
      </div>
    </div>
  </div>

  <!-- STEP 3: Results -->
  <div class="panel" id="panel3">
    <div class="card">
      <h2>Diagnosis Complete</h2>
      <div id="resultBanner"></div>
      <div class="detail-grid" id="detailGrid"></div>
      <div id="captureStatus" style="display:none;"></div>
    </div>
  </div>

  <!-- STEP 4: Fix -->
  <div class="panel" id="panel4">
    <div class="card">
      <h2>Recommended Fix</h2>
      <div id="fixContent"></div>
      <div class="btn-row">
        <button class="btn btn-primary" onclick="startOver()">↻ Run Again</button>
        <button class="btn btn-outline" onclick="copyReport()">📋 Copy Report</button>
      </div>
    </div>
  </div>

  <p class="footer">netdiag v1.0.0 · <a href="https://github.com/jgonzalez007/netdiag">GitHub</a></p>
</div>

<script>
let selectedSymptom = '';
let lastReport = {};

function selectSymptom(el, symptom) {
  document.querySelectorAll('.symptom-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  selectedSymptom = symptom;
  document.getElementById('customSymptom').value = '';
}

function getSymptom() {
  const custom = document.getElementById('customSymptom').value.trim();
  return custom || selectedSymptom || 'network issue';
}

function goStep(n) {
  for (let i = 1; i <= 4; i++) {
    document.getElementById('panel' + i).classList.toggle('active', i === n);
    const sc = document.getElementById('sc' + i);
    const sl = document.getElementById('sl' + i);
    sc.classList.remove('active', 'done');
    sl.classList.remove('active');
    if (i < n) { sc.classList.add('done'); sc.textContent = '✓'; }
    else if (i === n) { sc.classList.add('active'); sc.textContent = i; sl.classList.add('active'); }
    else { sc.textContent = i; }
  }
  const fill = document.getElementById('stepLineFill');
  fill.style.width = ((n - 1) / 3 * 100) + '%';
}

const checks = ['wifi', 'eth', 'gw', 'dns', 'http', 'trace'];
function setCheckState(id, state) {
  const ico = document.getElementById('ico-' + id);
  ico.className = 'check-icon ' + state;
  if (state === 'pending') ico.textContent = '○';
  else if (state === 'running') ico.textContent = '◌';
  else if (state === 'done') ico.textContent = '✓';
  else if (state === 'fail') ico.textContent = '✗';
}

async function startScan() {
  const symptom = getSymptom();
  goStep(2);

  // Reset checks
  checks.forEach(c => setCheckState(c, 'pending'));
  document.getElementById('scanStatus').textContent = 'Running diagnostics…';

  // Animate checks one by one while waiting for the real result
  const scanAnim = animateChecks();

  try {
    // Fire both requests: human report + JSON
    const [checkResp, jsonResp] = await Promise.all([
      fetch('/api/check', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({symptom}) }),
      fetch('/api/json',  { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({symptom}) }),
    ]);
    const checkData = await checkResp.json();
    const jsonData = await jsonResp.json();
    lastReport = { check: checkData, json: jsonData, symptom };

    // Finish animation
    clearInterval(scanAnim);
    checks.forEach(c => setCheckState(c, 'done'));

    // Mark failures
    if (jsonData.interfaces) {
      if (!jsonData.interfaces.wifi) setCheckState('wifi', 'fail');
      if (!jsonData.interfaces.ethernet) setCheckState('eth', 'fail');
      if (!jsonData.interfaces.gateway_reachable) setCheckState('gw', 'fail');
      if (!jsonData.interfaces.dns) setCheckState('dns', 'fail');
      if (!jsonData.interfaces.http) setCheckState('http', 'fail');
    }

    document.getElementById('scanStatus').textContent = 'Done!';
    await sleep(600);
    showResults(checkData, jsonData);
  } catch (e) {
    clearInterval(scanAnim);
    document.getElementById('scanStatus').textContent = 'Error: ' + e.message;
  }
}

function animateChecks() {
  let idx = 0;
  return setInterval(() => {
    if (idx > 0) setCheckState(checks[idx - 1], 'done');
    if (idx < checks.length) {
      setCheckState(checks[idx], 'running');
      const labels = ['Checking WiFi…', 'Checking Ethernet…', 'Pinging gateway…', 'Testing DNS…', 'Reaching internet…', 'Tracing route…'];
      document.getElementById('scanStatus').textContent = labels[idx];
      idx++;
    }
  }, 800);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function showResults(checkData, jsonData) {
  goStep(3);
  const banner = document.getElementById('resultBanner');
  const grid = document.getElementById('detailGrid');

  const state = jsonData.state || 'unknown';
  const isHealthy = state === 'healthy';
  const isWarn = state === 'http_failure';

  let bannerClass = isHealthy ? 'healthy' : isWarn ? 'warning' : 'issue';
  let bannerIcon = isHealthy ? '✅' : isWarn ? '⚠️' : '❌';
  let bannerTitle = isHealthy ? 'Your network looks healthy'
    : isWarn ? 'Partial connectivity issue detected'
    : 'Network issue detected';
  let bannerDesc = isHealthy
    ? 'The quick tests passed. Running deep checks and a traffic capture next to look for problems they can miss.'
    : 'We found a problem — see below for details and a recommended fix.';

  banner.innerHTML = `
    <div class="result-banner ${bannerClass}">
      <span class="banner-icon">${bannerIcon}</span>
      <div class="banner-text"><h3>${bannerTitle}</h3><p>${bannerDesc}</p></div>
    </div>`;

  const ifaces = jsonData.interfaces || {};
  grid.innerHTML = `
    <div class="detail-item"><div class="label">WiFi</div><div class="value ${ifaces.wifi ? 'ok' : 'fail'}">${ifaces.wifi ? '✓ Connected' : '✗ Down'}</div></div>
    <div class="detail-item"><div class="label">Ethernet</div><div class="value ${ifaces.ethernet ? 'ok' : 'fail'}">${ifaces.ethernet ? '✓ Connected' : '— Not connected'}</div></div>
    <div class="detail-item"><div class="label">Gateway</div><div class="value ${ifaces.gateway_reachable ? 'ok' : 'fail'}">${ifaces.gateway_reachable ? '✓ ' + (ifaces.gateway || 'Reachable') : '✗ Unreachable'}</div></div>
    <div class="detail-item"><div class="label">DNS</div><div class="value ${ifaces.dns ? 'ok' : 'fail'}">${ifaces.dns ? '✓ Resolving' : '✗ Failed'}</div></div>
    <div class="detail-item"><div class="label">Internet</div><div class="value ${ifaces.http ? 'ok' : 'fail'}">${ifaces.http ? '✓ Reachable' : '✗ Unreachable'}</div></div>
    <div class="detail-item"><div class="label">State</div><div class="value">${state}</div></div>
  `;

  runExtras(checkData, jsonData, isHealthy);
}

const CAPTURE_SECONDS = 15;

async function postJson(url, body) {
  try {
    const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body || {}) });
    return await r.json();
  } catch (e) {
    return { error: e.message };
  }
}

// Deep checks (MTU, IPv6 churn, Wi-Fi quality, ISP gateway, power, VPN) always run.
// If the quick test passed, also watch live traffic to catch what a one-shot check misses.
async function runExtras(checkData, jsonData, healthy) {
  const status = document.getElementById('captureStatus');
  status.style.display = 'block';
  let left = healthy ? CAPTURE_SECONDS : 0;
  status.innerHTML = extrasStatusHtml(healthy, left);
  const tick = setInterval(() => { left = Math.max(0, left - 1); status.innerHTML = extrasStatusHtml(healthy, left); }, 1000);
  const [deep, capture] = await Promise.all([
    postJson('/api/deep'),
    healthy ? postJson('/api/capture', {seconds: CAPTURE_SECONDS}) : Promise.resolve(null),
  ]);
  clearInterval(tick);
  status.style.display = 'none';
  lastReport.deep = deep;
  if (capture) lastReport.capture = capture;
  showFix(checkData, jsonData, capture, deep);
}

function extrasStatusHtml(healthy, left) {
  const what = healthy
    ? `Capturing network traffic… ${left}s`
    : 'Running deep checks…';
  const desc = healthy
    ? "The quick test found nothing wrong, so we're checking MTU, IPv6 stability, Wi-Fi quality and your ISP, and watching live traffic. Reproduce the problem now if you can."
    : 'Checking MTU, IPv6 stability, Wi-Fi quality, your ISP gateway and VPN state.';
  return `<div class="result-banner warning"><span class="banner-icon">📡</span><div class="banner-text">
    <h3>${what}</h3><p>${desc}</p></div></div>`;
}

const SEV = { high: ['🔴', 'var(--fail-bg)'], medium: ['🟡', 'var(--warn-bg)'], low: ['⚪', 'var(--bg)'] };

function deepHtml(d) {
  if (!d) return '';
  if (d.error) return `<div class="fix-card" style="margin-top:16px;"><h3>🔬 Deep Checks</h3><p>Deep checks failed: ${escapeHtml(d.error)}</p></div>`;
  const items = d.findings.length
    ? d.findings.map(f => `
      <div style="background:${SEV[f.severity][1]};border-radius:10px;padding:12px 14px;margin-top:10px;">
        <div style="font-weight:600;font-size:14px;">${SEV[f.severity][0]} ${escapeHtml(f.title)}</div>
        <p style="font-size:13px;margin-top:4px;">${escapeHtml(f.detail)}</p>
        <p style="font-size:13px;margin-top:6px;"><b>Fix:</b> ${escapeHtml(f.fix)}</p>
      </div>`).join('')
    : '<p>No MTU, IPv6, Wi-Fi quality, ISP or power problems found.</p>';
  const q = d.wifi_quality || {}, isp = d.isp_gateway;
  const cell = (label, value, bad) => `<div class="detail-item"><div class="label">${label}</div><div class="value ${bad === undefined ? '' : bad ? 'fail' : 'ok'}">${value}</div></div>`;
  return `
    <div class="fix-card" style="margin-top:16px;">
      <h3>🔬 Deep Checks ${d.findings.length ? '— ' + d.findings.length + ' issue(s)' : '— all clear'}</h3>
      ${items}
      <div class="detail-grid">
        ${cell('Path MTU', d.path_mtu_v4 ? d.path_mtu_v4 + ' B' : '—', d.path_mtu_v4 ? d.path_mtu_v4 < 1500 : undefined)}
        ${cell('IPv6 changes/h', d.ipv6_changes_1h ?? '—', d.ipv6_changes_1h > 12)}
        ${cell('Wi-Fi signal', q.rssi_avg ? q.rssi_avg + ' dBm (min ' + q.rssi_min + ')' : '—', q.rssi_min ? q.rssi_min < -75 : undefined)}
        ${cell('Public IPv4', escapeHtml(d.public_ipv4 || '—'), d.public_ipv4_stable === false)}
        ${isp ? cell('ISP', escapeHtml(isp.isp)) : ''}
        ${isp ? cell(isp.tech + ' signal', `SINR ${isp.sinr} · RSRP ${isp.rsrp}`, isp.sinr < 5) : ''}
        ${cell('VPN tunnels', (d.vpn.tunnels || []).join(', ') || 'none')}
      </div>
    </div>`;
}

function fmtBytes(n) {
  return n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n > 1e3 ? (n / 1e3).toFixed(1) + ' KB' : n + ' B';
}

function captureHtml(c) {
  if (!c) return '';
  if (c.error) return `<div class="fix-card" style="margin-top:16px;"><h3>📡 Traffic Capture</h3><p>Capture failed: ${escapeHtml(c.error)}</p></div>`;
  const ok = c.findings.length === 0;
  const findings = ok
    ? `<p>No retransmits, reordering, slow connections or interface errors in ${c.seconds}s of traffic on <code>${escapeHtml(c.iface)}</code>. The problem is probably intermittent — run again while it's happening.</p>`
    : '<ul style="margin:6px 0 0 18px;font-size:14px;line-height:1.6;">' + c.findings.map(f => `<li>${escapeHtml(f)}</li>`).join('') + '</ul>';
  const procs = (c.top_processes || []).map(p => `${escapeHtml(p.proc)} (${fmtBytes(p.bytes)})`).join(', ') || 'none';
  const pcap = c.tcpdump
    ? `Full capture: <code>${escapeHtml(c.tcpdump.pcap)}</code> (${c.tcpdump.packets} packets) — open it in Wireshark.`
    : 'Full packet capture (.pcap) skipped: tcpdump needs root. Start the server with <code>sudo</code> to save one.';
  return `
    <div class="fix-card" style="margin-top:16px;">
      <h3>📡 Traffic Capture ${ok ? '— nothing abnormal' : '— issues found'}</h3>
      ${findings}
      <div class="detail-grid">
        <div class="detail-item"><div class="label">Packets</div><div class="value">${c.packets}</div></div>
        <div class="detail-item"><div class="label">Retransmitted</div><div class="value ${c.retransmit_rate > 0.02 ? 'fail' : 'ok'}">${(c.retransmit_rate * 100).toFixed(1)}%</div></div>
        <div class="detail-item"><div class="label">Out of order</div><div class="value ${c.out_of_order_rate > 0.02 ? 'fail' : 'ok'}">${(c.out_of_order_rate * 100).toFixed(1)}%</div></div>
        <div class="detail-item"><div class="label">Avg RTT</div><div class="value">${c.avg_rtt_ms == null ? '—' : c.avg_rtt_ms + ' ms'}</div></div>
        <div class="detail-item"><div class="label">Iface errors</div><div class="value ${c.iface_errors.in + c.iface_errors.out ? 'fail' : 'ok'}">${c.iface_errors.in + c.iface_errors.out}</div></div>
      </div>
      <p style="font-size:13px;">Top talkers: ${procs}</p>
      <p style="font-size:13px;margin-top:6px;">${pcap}</p>
    </div>`;
}

function showFix(checkData, jsonData, capture, deep) {
  goStep(4);
  const fc = document.getElementById('fixContent');
  const raw = checkData.stdout || '';

  // Parse root cause and fix from raw output
  let rootCause = 'Unknown';
  let fix = 'Run netdiag watch to monitor for intermittent issues.';
  let confidence = 'low';

  const causeMatch = raw.match(/Cause:\s+(.+)/);
  const fixMatch = raw.match(/Recommended Fix ---\n\s*(.+)/);
  const confMatch = raw.match(/Confidence:\s+(\w+)/);
  if (causeMatch) rootCause = causeMatch[1].trim();
  if (fixMatch) fix = fixMatch[1].trim();
  if (confMatch) confidence = confMatch[1].trim();

  // The quick test only sees hard failures. If it found nothing but a deep check did, lead with that.
  const top = deep && deep.findings && deep.findings[0];
  if (top && jsonData.state === 'healthy') {
    rootCause = top.title + ' — ' + top.detail;
    fix = top.fix;
    confidence = top.severity === 'high' ? 'high' : 'medium';
  }

  const confClass = confidence === 'high' ? 'fail' : confidence === 'medium' ? 'warn' : 'muted';
  const confLabel = confidence === 'high' ? '🔴 High confidence' : confidence === 'medium' ? '🟡 Medium confidence' : '⚪ Low confidence';

  fc.innerHTML = `
    <div style="margin-bottom:16px;">
      <span style="font-size:13px;color:var(--muted);">${confLabel}</span>
    </div>
    <div class="fix-card" style="margin-bottom:16px;">
      <h3>🔍 Root Cause</h3>
      <p>${escapeHtml(rootCause)}</p>
    </div>
    <div class="fix-card">
      <h3>🛠 What To Do</h3>
      <p>${escapeHtml(fix)}</p>
    </div>
    ${deepHtml(deep)}
    ${captureHtml(capture)}
    <details style="margin-top:20px;">
      <summary style="cursor:pointer;color:var(--muted);font-size:13px;">View full report</summary>
      <pre style="background:#1d1d1f;color:#e8e8ed;padding:16px;border-radius:10px;margin-top:8px;overflow-x:auto;font-size:12px;white-space:pre-wrap;">${escapeHtml(raw)}</pre>
    </details>
  `;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function startOver() {
  selectedSymptom = '';
  document.querySelectorAll('.symptom-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('customSymptom').value = '';
  goStep(1);
}

function copyReport() {
  let raw = (lastReport.check || {}).stdout || JSON.stringify(lastReport.json, null, 2);
  if (lastReport.deep) raw += '\n--- Deep Checks ---\n' + JSON.stringify(lastReport.deep, null, 2);
  if (lastReport.capture) raw += '\n--- Traffic Capture ---\n' + JSON.stringify(lastReport.capture, null, 2);
  navigator.clipboard.writeText(raw).then(() => {
    const btn = event.target;
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = '📋 Copy Report'; }, 2000);
  });
}

document.getElementById('customSymptom').addEventListener('keydown', e => {
  if (e.key === 'Enter') startScan();
});
</script>
</body>
</html>"""

class NetdiagHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path).path
        if p == '/':
            self._html(HTML)
        elif p == '/api/signature':
            r = run_netdiag(['report', '--json', 'auto'])
            sig = "error"
            if 'error' not in r and 'interfaces' in r:
                i = r['interfaces']
                sig = f"wifi:{i.get('wifi',0)} eth:{i.get('ethernet',0)} dns:{i.get('dns',0)} gw:{i.get('gateway_reachable',0)} http:{i.get('http',0)}"
            self._json({"signature": sig})
        else:
            self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._body()
        if p == '/api/check':
            self._json(run_netdiag(['check', body.get('symptom', 'network issue')]))
        elif p == '/api/doctor':
            self._json(run_netdiag(['doctor']))
        elif p == '/api/json':
            self._json(run_netdiag(['report', '--json', body.get('symptom', 'network issue')]))
        elif p == '/api/deep':
            try:
                self._json(run_deep_checks())
            except Exception as e:
                self._json({"error": str(e)})
        elif p == '/api/capture':
            try:
                self._json(run_capture(max(3, min(int(body.get('seconds', 10)), 60))))
            except Exception as e:
                self._json({"error": str(e)})
        else:
            self.send_error(404)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        try: return json.loads(self.rfile.read(n)) if n else {}
        except: return {}

    def _json(self, obj):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _html(self, html):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *a): pass

def main():
    port = 8765
    srv = ThreadingHTTPServer(('127.0.0.1', port), NetdiagHandler)
    url = f'http://127.0.0.1:{port}'
    print(f'netdiag web → {url}')
    print('Press Ctrl+C to stop')
    if not os.environ.get('NETDIAG_NO_BROWSER'):  # the app launcher opens the page itself
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
        srv.shutdown()

if __name__ == '__main__':
    main()
