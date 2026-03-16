"""
constants.py — Shared constants for the dev_front_end (dashboard) branch.

This file contains ONLY what the dashboard needs:
  - File paths to state files on C: drive
  - Display settings (port, refresh interval)
  - SHA / RSI display parameters (for legend rendering)
  - symbols.json path for symbol list

No trading logic, no order types, no Fyers SDK usage.
"""

from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS  (all state is on C: drive, written by other branches)
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_JSON = Path(__file__).resolve().parent / "symbols.json"

# ── State directories (mode-separated: demo / live) ───────────────────────────
STATE_DIR_BASE = Path("C:/Ballom_FYR/state")
STATE_DIR_DEMO = STATE_DIR_BASE / "demo"
STATE_DIR_LIVE = STATE_DIR_BASE / "live"


def get_state_dir(mode: str) -> Path:
    """Return the state directory for the given mode."""
    return STATE_DIR_LIVE if mode == "live" else STATE_DIR_DEMO


# ═══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARD_PORT       = 8050
DASHBOARD_REFRESH_MS = 2000    # auto-refresh interval (milliseconds)


# ═══════════════════════════════════════════════════════════════════════════════
#  DISPLAY PARAMETERS  (for SHA / RSI legend rendering only — no computation)
# ═══════════════════════════════════════════════════════════════════════════════

# SHA lengths (used in card headers "SIGNAL SHA (3)" / "TREND SHA (6)")
SHA_LENGTH       = 3
SHA_TREND_LENGTH = 6

# RSI zones (used for badge coloring in the dashboard)
RSI_OVERSOLD     = 30
RSI_OVERBOUGHT   = 70

# GAP% thresholds (used for GAP badge coloring)
GAP_RANGE_LOW    = 0.5
GAP_RANGE_HIGH   = 2.5
