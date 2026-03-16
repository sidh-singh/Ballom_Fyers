"""
constants.py — Shared constants for the dev_scanner branch.

This branch handles:
  • Fyers authentication (fyers_auth.py)
  • Hourly index option CE/PE pair scanning (fyers_api.py)

Only values used by auth + scanning are kept here.
"""

from datetime import time as dt_time
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_JSON      = Path(__file__).resolve().parent / "symbols.json"
OPTION_PAIRS_JSON = Path("C:/Ballom_FYR/option_pairs.json")

# ── State file directories (mode-separated so demo & live never clash) ─────────
STATE_DIR_BASE = Path("C:/Ballom_FYR/state")
STATE_DIR_DEMO = STATE_DIR_BASE / "demo"
STATE_DIR_LIVE = STATE_DIR_BASE / "live"


def get_state_dir(mode: str) -> Path:
    """Return the state directory for the given mode."""
    return STATE_DIR_LIVE if mode == "live" else STATE_DIR_DEMO


# ── Token file on C: drive (shared by all branches) ───────────────────────────
TOKEN_DIR  = Path("C:/Ballom_FYR")
TOKEN_FILE = TOKEN_DIR / "fyers_token.json"

# ── Cache directory for CSVs, holidays etc. ────────────────────────────────────
CACHE_DIR = TOKEN_DIR / "cache"


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING TIME WINDOWS  (index market hours for scan window)
# ═══════════════════════════════════════════════════════════════════════════════

INDICES_START = dt_time(9, 15)
INDICES_END   = dt_time(15, 30)


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

# Minimum 1min candles an option contract must have to be selected as a pair.
# 200 1min candles → 40 5min / 13 15min candles, sufficient for downstream
# SHA + RSI processing in the dev_updater branch.
MIN_CANDLES_FOR_ANALYSIS = 200

# Default hedge profit target (₹) for index option pairs.
# Can be overridden per-symbol in symbols.json via the "hedge" key.
STRATEGY_HEDGE_INDEX = 500


# ═══════════════════════════════════════════════════════════════════════════════
#  COLUMN DEFINITIONS  (Fyers public symbol CSV shape)
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_COLS = [
    'Fytoken', 'Symbol Details', 'Exchange Instrument type',
    'Minimum lot size', 'Tick size', 'ISIN', 'Trading Session',
    'Last update date', 'Expiry date', 'Symbol ticker', 'Exchange',
    'Segment', 'Scrip code', 'Underlying symbol', 'Underlying scrip code',
    'Strike price', 'Option type', 'Underlying FyToken',
    'Reserved column1', 'Reserved column2', 'Reserved column3',
]
