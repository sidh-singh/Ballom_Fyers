"""
health.py — check whether a server is up.

No credentials required: each server is probed two ways and counted UP if EITHER
succeeds (so a box that firewalls ICMP but serves RDP/SSH still reads correctly):
  1. ICMP ping            → reachability
  2. TCP connect to its service port (Windows→RDP 3389, Linux→SSH 22) → service alive

That is enough to detect a shutdown / unreachable server. (If you later want deeper
checks — a specific service running, CPU/RAM — that needs login creds; this module is
the single place to extend, see check_server.)
"""
from __future__ import annotations

import platform
import socket
import subprocess
import time

from constants import (PING_TIMEOUT_SEC, TCP_TIMEOUT_SEC, SERVICE_PORT,
                       PROBE_ATTEMPTS, PROBE_RETRY_GAP_SEC)

_LOCAL_WINDOWS = platform.system() == "Windows"


def _ping(ip: str, timeout: float = PING_TIMEOUT_SEC) -> tuple[bool, float | None]:
    """One ICMP echo. Returns (reachable, latency_ms). Ping flags depend on the LOCAL OS
    (where this monitor runs), not the target.

    Reachability requires a GENUINE echo reply — verified by a 'TTL='/'ttl=' token in the output,
    NOT just a zero exit code. This matters on Windows: `ping` exits 0 even for
    'Reply from <gateway>: Destination host unreachable.' (an ICMP *error* — the box is actually
    DOWN), which would otherwise be read as UP. Every real IPv4 echo reply carries a TTL, while
    'timed out' / 'unreachable' lines do not, so this reads correctly on both Windows and Linux."""
    if _LOCAL_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        out = f"{r.stdout or ''}{r.stderr or ''}".lower()
        if r.returncode == 0 and "ttl=" in out:
            return True, round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        pass
    return False, None


def _tcp(ip: str, port: int, timeout: float = TCP_TIMEOUT_SEC) -> tuple[bool, float | None]:
    """TCP connect to a port. Returns (open, latency_ms)."""
    t0 = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return False, None


def check_server(server: dict) -> dict:
    """Probe one {name, ip, os_type} server. Returns a status record."""
    ip = server["ip"]
    os_type = server.get("os_type", "linux")
    port = SERVICE_PORT.get(os_type, 22)

    # Retry the probe a few times before concluding DOWN, so a single dropped ICMP echo or a
    # momentary TCP hiccup (common on internet VPS with rate-limited ICMP / firewalled RDP) does
    # not masquerade as a shutdown. Declared UP the instant EITHER probe answers on ANY attempt;
    # only an all-attempts failure counts as DOWN. (The monitor adds a second, cross-sweep guard.)
    reachable = port_open = False
    ping_ms = port_ms = None
    for attempt in range(max(1, PROBE_ATTEMPTS)):
        reachable, ping_ms = _ping(ip)
        port_open, port_ms = _tcp(ip, port)
        if reachable or port_open:
            break
        if attempt < PROBE_ATTEMPTS - 1:
            time.sleep(PROBE_RETRY_GAP_SEC)
    up = reachable or port_open
    latency = ping_ms if ping_ms is not None else port_ms

    return {
        "name": server["name"],
        "ip": ip,
        "os_type": os_type,
        "provider": server.get("provider", ""),
        "port": port,
        "status": "UP" if up else "DOWN",
        "reachable": reachable,       # ICMP ping
        "port_open": port_open,       # service port (RDP/SSH)
        "latency_ms": latency,
    }
