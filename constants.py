"""
constants.py — Shared constants for the dev_updater_nifty branch.

Only values actively used by this branch.  Other branches maintain
their own constants.py with different content.
"""

from datetime import time as dt_time
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS
# ═══════════════════════════════════════════════════════════════════════════════

# option_pairs.json — written by dev_scanner, read by this branch
OPTION_PAIRS_JSON = Path("C:/Ballom_FYR/option_pairs.json")

# ── State file directories (mode-separated so demo & live never clash) ─────────
STATE_DIR_BASE = Path("C:/Ballom_FYR/state")
STATE_DIR_DEMO = STATE_DIR_BASE / "demo"
STATE_DIR_LIVE = STATE_DIR_BASE / "live"


def get_state_dir(mode: str) -> Path:
    """Return the state directory for the given mode."""
    return STATE_DIR_LIVE if mode == "live" else STATE_DIR_DEMO


# ── Token & cache (shared across all branches) ────────────────────────────────
TOKEN_DIR  = Path("C:/Ballom_FYR")
TOKEN_FILE = TOKEN_DIR / "fyers_token.json"
CACHE_DIR  = TOKEN_DIR / "cache"


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING TIME WINDOWS
# ═══════════════════════════════════════════════════════════════════════════════

INDICES_START = dt_time(9, 15)
INDICES_END   = dt_time(15, 30)


# ═══════════════════════════════════════════════════════════════════════════════
#  SHA INDICATOR PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

# Signal SHA — fast / short-term momentum indicator
SHA_LENGTH   = 3
SHA_MA_TYPE  = "RMA"

# Trend SHA — slower / longer-term trend indicator
SHA_TREND_LENGTH   = 6
SHA_TREND_MA_TYPE  = "RMA"


# ═══════════════════════════════════════════════════════════════════════════════
#  RSI PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

RSI_PERIOD = 14   # Wilder's look-back period


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMEFRAME CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════
# Fyers supported intraday resolutions:
#   Seconds: 5S, 10S, 15S, 30S, 45S
#   Minutes: 1, 2, 3, 5, 10, 15, 20, 30
#   Hours:   60, 120, 240
#   Daily:   D
#
# Market hours for INDEX: 9:15–15:30 = 375 min/day.
# Candle counts per day per timeframe:
#   1min  → 375 candles/day
#   5min  → 75  candles/day
#   15min → 25  candles/day
#   30min → 12  candles/day  (12.5 → 13 with partial last candle)
#   60min → 6   candles/day  (6.25 → 7 with partial last candle)
#
# To get N candles, Fyers API returns only candles from actual
# trading sessions — weekends, holidays are skipped automatically
# when we compute the date range.

# SHA is computed on 1min only — need enough candles for SHA warmup
SHA_TIMEFRAME = "1"
SHA_CANDLES   = 500

# RSI timeframes with candle counts
RSI_TIMEFRAMES = {
    "1m":  {"resolution": "1",  "candles": 500},
    "5m":  {"resolution": "5",  "candles": 200},
    "15m": {"resolution": "15", "candles": 100},
    "30m": {"resolution": "30", "candles": 100},
    "1h":  {"resolution": "60", "candles": 100},
}


# ═══════════════════════════════════════════════════════════════════════════════
#  GAP% PARAMETERS  (gap between Signal SHA and Trend SHA)
# ═══════════════════════════════════════════════════════════════════════════════

GAP_RANGE_LOW  = 0.5    # % — below this, SHAs nearly overlapping
GAP_RANGE_HIGH = 2.5    # % — above this, over-extended


# ═══════════════════════════════════════════════════════════════════════════════
#  UPDATER TIMING
# ═══════════════════════════════════════════════════════════════════════════════

UPDATE_INTERVAL = 3   # seconds between each full analysis cycle


# ═══════════════════════════════════════════════════════════════════════════════
#  SYMBOL FILTER — this branch ONLY processes NIFTY
# ═══════════════════════════════════════════════════════════════════════════════

ACTIVE_SYMBOLS = {"NIFTY"}
