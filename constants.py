"""
constants.py — configuration for the Ballom server health-check dashboard.

Standalone mini-app (NOT the fyers trading engine). It polls every server in
ip_list.txt for reachability and shows a live + historical dashboard.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path

APP_NAME = "Ballom Server Health"

# ── dashboard ───────────────────────────────────────────────────────────────
# Deliberately OUTSIDE the fyers dashboard range (8050–8056) so it never clashes
# with any of the 12 trading dashboards. Override with the first CLI arg or PORT=.
DASHBOARD_PORT = int(os.environ.get("PORT", "9000"))
DASHBOARD_REFRESH_MS = 10_000          # browser auto-refresh cadence

# ── monitor ─────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SEC = 30                # how often the monitor polls every server
PING_TIMEOUT_SEC = 2                   # per-server ICMP ping timeout
TCP_TIMEOUT_SEC = 3                    # per-server service-port connect timeout
RETENTION_DAYS = 7                     # keep exactly 1 week of history

# Service port probed per target OS — confirms the box is actually serving even if
# ICMP ping is firewalled. Windows → RDP, Linux → SSH.
SERVICE_PORT = {"windows": 3389, "linux": 22}

# ── data dir (OS-aware; override with HEALTH_DIR) ───────────────────────────
def _default_data_dir() -> Path:
    env = os.environ.get("HEALTH_DIR")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("C:/Ballom_HealthCheck")
    return Path.home() / "ballom-healthcheck"


DATA_DIR = _default_data_dir()
STATE_DIR = DATA_DIR / "state"
HEALTH_DIR = STATE_DIR / "health"             # per-day history + current snapshot
CURRENT_FILE = HEALTH_DIR / "current.json"    # latest status of every server

# ip_list.txt lives in the repo next to this file (tracked, user-editable)
IP_LIST_FILE = Path(__file__).resolve().parent / "ip_list.txt"
