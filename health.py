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

from constants import PING_TIMEOUT_SEC, TCP_TIMEOUT_SEC, SERVICE_PORT

_LOCAL_WINDOWS = platform.system() == "Windows"


def _ping(ip: str, timeout: float = PING_TIMEOUT_SEC) -> tuple[bool, float | None]:
    """One ICMP echo. Returns (reachable, latency_ms). Ping flags depend on the LOCAL OS
    (where this monitor runs), not the target."""
    if _LOCAL_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), ip]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout + 3)
        if r.returncode == 0:
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

    reachable, ping_ms = _ping(ip)
    port_open, port_ms = _tcp(ip, port)
    up = reachable or port_open
    latency = ping_ms if ping_ms is not None else port_ms

    return {
        "name": server["name"],
        "ip": ip,
        "os_type": os_type,
        "port": port,
        "status": "UP" if up else "DOWN",
        "reachable": reachable,       # ICMP ping
        "port_open": port_open,       # service port (RDP/SSH)
        "latency_ms": latency,
    }
