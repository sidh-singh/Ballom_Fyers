"""
probe_debug.py — live probe viewer for diagnosing UP/DOWN detection.

Run this ON THE MONITOR HOST (the box running monitor.py) while you reboot a test server. It
prints, every few seconds, the RAW ping and service-port result for each server — so you can see
EXACTLY what the monitor sees. In particular: does ICMP keep answering while the guest OS is down?
If `ping=UP` stays green while the box is rebooting, something upstream (the host / hypervisor) is
answering ping and the real reboot is hidden — the fix is UP_POLICY=port (if `port=OPEN` when the
box is healthy). If both go DOWN/CLOSED during the reboot, the probe layer is fine.

Usage (on the monitor host):
    python probe_debug.py                      # every server in ip_list.txt
    python probe_debug.py 198.24.151.216       # one IP (RDP 3389 assumed)
    python probe_debug.py 198.24.151.216:3389  # explicit port
Ctrl+C to stop.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

from health import _ping, _tcp
from servers import load_servers
from constants import SERVICE_PORT, PING_TIMEOUT_SEC, TCP_TIMEOUT_SEC, UP_POLICY


def _targets(argv: list[str]) -> list[dict]:
    if argv:
        out = []
        for a in argv:
            ip, _, p = a.partition(":")
            out.append({"name": ip, "ip": ip, "port": int(p) if p else 3389})
        return out
    return [{"name": s["name"], "ip": s["ip"],
             "port": SERVICE_PORT.get(s.get("os_type", "linux"), 22)} for s in load_servers()]


def main() -> None:
    targets = _targets(sys.argv[1:])
    print(f"probe_debug — {len(targets)} target(s), UP_POLICY={UP_POLICY}, "
          f"ping timeout {PING_TIMEOUT_SEC}s / tcp {TCP_TIMEOUT_SEC}s. Ctrl+C to stop.\n"
          f"Watch during a reboot: if ping stays UP while port goes CLOSED, ICMP is answered "
          f"upstream — use UP_POLICY=port.\n")
    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        for t in targets:
            reach, pms = _ping(t["ip"])
            popen, tms = _tcp(t["ip"], t["port"])
            ping_s = f"UP ({pms}ms)" if reach else "DOWN"
            port_s = f"OPEN ({tms}ms)" if popen else "CLOSED"
            print(f"{ts}  {t['name']:<16} {t['ip']:<16}  ping={ping_s:<14}  port{t['port']}={port_s}")
        print("-" * 76)
        time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
