"""
constants.py — Shared constants for the consolidated `fyers_live` branch.

LIVE ONLY (no demo). See ARCHITECTURE.md for the full design.

Groups:
  • File paths on the C: drive
  • IST-aware time helpers (the old code used naive datetime.now())
  • Market-hours table (INDEX / COMMODITY)
  • Fyers API rate-limit table + endpoint→category map (Standard plan)
  • NSE F&O symbol-CSV column layout
"""

from __future__ import annotations

import os
from datetime import datetime, time as dt_time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo            # py3.9+
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                            # pragma: no cover
    IST = None  # falls back to naive; install tzdata on Windows if this hits


# ═══════════════════════════════════════════════════════════════════════════════
#  IST-AWARE TIME HELPERS  (fixes old naive-clock bug — see ARCHITECTURE §7)
# ═══════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Current time in IST, regardless of the host's local timezone."""
    return datetime.now(IST) if IST else datetime.now()

def today_ist():
    return now_ist().date()


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS  (single live tree — demo removed)
# ═══════════════════════════════════════════════════════════════════════════════

# OS-aware root: $BALLOM_DIR if set, else ~/Ballom_FYR on Linux / C:/Ballom_FYR on
# Windows. Token + cache + state all root off this one constant (so a Linux host
# never creates a stray "C:" folder). systemd can point it anywhere via Environment=.
_BALLOM_DEFAULT = "C:/Ballom_FYR" if os.name == "nt" else str(Path.home() / "Ballom_FYR")
BALLOM_DIR   = Path(os.environ.get("BALLOM_DIR", _BALLOM_DEFAULT))
TOKEN_DIR    = BALLOM_DIR
TOKEN_FILE   = BALLOM_DIR / "fyers_token.json"
CACHE_DIR    = BALLOM_DIR / "cache"
STATE_DIR    = BALLOM_DIR / "state"              # single dir (no demo/live split)

APP_STATUS_FILE     = STATE_DIR / "app_status.json"
API_USAGE_FILE      = STATE_DIR / "api_usage.json"      # written by api_meter
OPTION_PAIRS_JSON   = STATE_DIR / "option_pairs.json"
POSITION_STATE_FILE = STATE_DIR / "position_state.json"
SIGNAL_STATE_FILE   = STATE_DIR / "signal_state.json"   # per-symbol SHA/RSI/signal/pair
OHLC_STATE_FILE     = STATE_DIR / "ohlc_state.json"     # active-leg OHLC candles (dashboard price chart)
ACCOUNT_STATE_FILE  = STATE_DIR / "account_state.json"  # balance/equity/margin
STRATEGY_LOG_DIR    = STATE_DIR / "strategy_log"

SYMBOLS_JSON = Path(__file__).resolve().parent / "symbols.json"


# ═══════════════════════════════════════════════════════════════════════════════
#  SUPERVISOR / LOOP TIMING
# ═══════════════════════════════════════════════════════════════════════════════

SUPERVISOR_TICK      = 30                 # seconds between Loop-1 ticks
WORKER_IDLE_SLEEP    = 8                  # idle between worker ticks (was 15); lower = faster LTP-based profit-booking, entries are bar-based. 8 = Prime single-bot; keep >=15 if several bots share one Fyers account.
DAILY_MAINTENANCE_AT = dt_time(0, 10)     # ~00:10 IST → re-auth + re-download CSV


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET HOURS  (IST)  — see ARCHITECTURE §7
# ═══════════════════════════════════════════════════════════════════════════════
#  entry_cutoff = latest time a NEW position may be opened for the day.
#  COMMODITY close flips with US DST: 23:30 (Mar–Nov) / 23:55 (Nov–Mar).
#  TODO: compute DST-correct commodity close per date.

MARKET_HOURS = {
    "INDEX": {
        "open":         dt_time(9, 15),
        "close":        dt_time(15, 30),
        "entry_cutoff": dt_time(15, 15),   # owner's "3:15" (confirm — ARCHITECTURE O1)
    },
    "COMMODITY": {
        "open":         dt_time(9, 0),
        "close":        dt_time(23, 30),   # 23:55 in Nov–Mar (DST) — TODO
        "entry_cutoff": dt_time(23, 0),    # placeholder (ARCHITECTURE O3)
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
#  FYERS API RATE LIMITS  (Standard plan, category-wise since 2026-10-16)
# ═══════════════════════════════════════════════════════════════════════════════
#  DATA day-limit (5,000) is the binding constraint. Exceed → -429.
#  Breaching per-minute >3×/day → blocked for the whole day: never breach.

API_LIMITS = {   # Standard plan (no active Fyers Prime)
    "DATA":     {"sec": 5,  "min": 50,  "day": 5_000},
    "ORDER":    {"sec": 10, "min": 200, "day": 100_000},
    "USERINFO": {"sec": 10, "min": 200, "day": 100_000},
}
API_LIMITS_PRIME = {   # Fyers Prime (paid subscription) — higher limits
    "DATA":     {"sec": 10, "min": 500, "day": 500_000},
    "ORDER":    {"sec": 10, "min": 500, "day": 500_000},
    "USERINFO": {"sec": 10, "min": 500, "day": 500_000},
}

# Plan is NOT exposed by the Fyers API — set at runtime from config (or an auto-probe).
# Default Standard (safe: lower limits avoid -429 if we guess wrong).
API_PLAN = "standard"   # "standard" | "prime"

def set_api_plan(plan: str) -> None:
    global API_PLAN
    API_PLAN = "prime" if str(plan).lower() == "prime" else "standard"

def active_api_limits() -> dict:
    return API_LIMITS_PRIME if API_PLAN == "prime" else API_LIMITS

def brokerage_per_order() -> float:
    """₹ per order — Prime lowers it ₹20→₹15 (the only tax/charge Prime changes)."""
    return BROKERAGE_PER_ORDER_PRIME if API_PLAN == "prime" else BROKERAGE_PER_ORDER

# FyersModel method name → rate-limit category.
ENDPOINT_CATEGORY = {
    # DATA
    "quotes": "DATA", "optionchain": "DATA", "history": "DATA", "depth": "DATA",
    # ORDER
    "place_order": "ORDER", "modify_order": "ORDER", "cancel_order": "ORDER",
    "exit_positions": "ORDER", "place_basket_orders": "ORDER",
    # USERINFO
    "get_profile": "USERINFO", "funds": "USERINFO", "holdings": "USERINFO",
    "positions": "USERINFO", "orderbook": "USERINFO", "tradebook": "USERINFO",
}
DEFAULT_CATEGORY = "DATA"   # unknown method → treat as DATA (most-restrictive)


# ═══════════════════════════════════════════════════════════════════════════════
#  SCANNER PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

MIN_CANDLES_FOR_ANALYSIS = 200
STRATEGY_HEDGE_INDEX     = 500     # ₹ default hedge (override per-symbol)
NSE_FO_URL  = "https://public.fyers.in/sym_details/NSE_FO.csv"
MCX_COM_URL = "https://public.fyers.in/sym_details/MCX_COM.csv"

# ── Option-pair scan tuning (carry-oriented; see scanner.py) ──
# Signals are index-driven (O2), so the scan no longer needs per-option candle
# history — it selects on liquidity + moneyness only (far fewer API calls).
SCAN_STRIKE_COUNT = 20
SCAN_MIN_DTE      = 7      # expiry window: not too close to expiry — want ≥ ~1 week …
SCAN_MAX_DTE      = 31     # … and ≤ ~1 month. Index weeklies land here; gold/silver (monthly)
                          # fall back to their nearest contract via the DTE-floor fallback.
SCAN_MIN_OI       = 10000  # skip illiquid strikes (unless they have volume)
SCAN_MAX_PREMIUM_PER_LOT = 30000   # ₹ — skip strikes whose premium × lot_size EXCEEDS this
                                   # (per-lot cost cap). 0 = no cap.
SCAN_MIN_PREMIUM_PER_LOT = 10000   # ₹ — skip strikes whose premium × lot_size is BELOW this,
                                   # so we buy near-ATM options with real value / history (not
                                   # cheap far-OTM). Band = [MIN, MAX] per lot. 0 = no floor.
                                   # Per-symbol override: "min_premium_per_lot" in symbols.json.
SCAN_ROUND_STRIKE_STEP = 1000      # ₹ — prefer strikes near a round multiple of this (the
                                   # liquid, long-history strikes, e.g. NIFTY 24000/25000/26000).
                                   # A scoring bonus, not a filter. Per-symbol: "round_strike_step".


# ═══════════════════════════════════════════════════════════════════════════════
#  NSE F&O SYMBOL-CSV COLUMN LAYOUT  (verified 21 cols, 2026-11-02)
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_COLS = [
    'Fytoken', 'Symbol Details', 'Exchange Instrument type',
    'Minimum lot size', 'Tick size', 'ISIN', 'Trading Session',
    'Last update date', 'Expiry date', 'Symbol ticker', 'Exchange',
    'Segment', 'Scrip code', 'Underlying symbol', 'Underlying scrip code',
    'Strike price', 'Option type', 'Underlying FyToken',
    'Reserved column1', 'Reserved column2', 'Reserved column3',
]


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY — HAM (Heiken-Ashi Martingale), ported 1:1 from Alcadeias
#  See TRADING_LOGIC.md. Signals computed on the INDEX (O2); bullish→CE, bearish→PE.
# ═══════════════════════════════════════════════════════════════════════════════

# ── SHA signal + trend (SHA-v3) ──
SHA_LENGTH        = 10
SHA_MA_TYPE       = "TEMA"
SHA_TREND_LENGTH  = 20
SHA_TREND_MA_TYPE = "DEMA"
STRATEGY_LOOKBACK      = 7     # candles analysed
STRATEGY_SHA_THRESHOLD = 0     # min SHA ratio to call a candle bullish
FIBO_SEQUENCE_LENGTH   = 25    # fib length (first 2 dropped → 1,2,3,5,8,13,…)

# SHA source candles (on the index)
SHA_TIMEFRAME    = "5"         # Fyers resolution (5-min) ≈ Alcadeias M5
SHA_CANDLE_COUNT = 120   # SHA DEMA-20/TEMA-10 warmup + 7-bar lookback (was 300; <~100 leaves the long smoothers unconverged)

# ── RSI (multi-timeframe) ──
RSI_LENGTH       = 14
RSI_MA_TYPE      = "RMA"
RSI_CANDLE_COUNT = 120   # RMA-14 + MACD-26 + the 20-bar pivot window all converge well within this (was 500)
RSI_OVERSOLD     = 35
RSI_OVERBOUGHT   = 65
RSI_MTF_OVERSOLD   = 35
RSI_MTF_OVERBOUGHT = 65

# Entry filter / DCA ladder / final-close timeframes (labels kept as Alcadeias names)
RSI_MTF_TIMEFRAMES        = ['TIMEFRAME_M1', 'TIMEFRAME_M5', 'TIMEFRAME_M15', 'TIMEFRAME_M30']
RSI_DCA_LADDER_TIMEFRAMES = ['TIMEFRAME_M1', 'TIMEFRAME_M5', 'TIMEFRAME_M15', 'TIMEFRAME_H1', 'TIMEFRAME_H4']
RSI_FINAL_CLOSE_TIMEFRAME = 'TIMEFRAME_H6'
RSI_DCA_MAX_POSITIONS     = 1

# label → Fyers history resolution. H6 (360) is NOT a native Fyers resolution →
# resample from 60-min in the worker (TODO Increment 2).
TIMEFRAME_TO_FYERS_RES = {
    'TIMEFRAME_M1': '1', 'TIMEFRAME_M5': '5', 'TIMEFRAME_M15': '15',
    'TIMEFRAME_M30': '30', 'TIMEFRAME_H1': '60', 'TIMEFRAME_H4': '240',
    'TIMEFRAME_H6': '360', 'TIMEFRAME_D1': 'D',
}

# ── Position sizing — balance-tiered units (Alcadeias floor+1, O1 ✓) ──
STEP_UP_BALANCE      = 100_000  # ₹ per unit tier: units = floor(bal/this)+1 (capped).
                                #   <1L→1 lot, 1L–2L→2 lots, 2L–3L→3 lots, …
                                #   (per-symbol override in symbols.json)
UNIT_MAX_LIMIT       = 10       # cap on units
HEDGE_PER_UNIT       = 300      # ₹ NET profit target per unit; total target = this×units + taxes,
                                #   so it scales with balance (unit tiers). ₹300 ≈ ~10–15 NIFTY pts
                                #   on 1 lot — clears taxes + noise. Per-symbol: "hedge_per_unit".
MAX_MARTINGALE_LEVEL = 5        # 5 RSI adds (M1/M5/M15/H1/H4), then H6 forced close
PRODUCT_TYPE         = "MARGIN" # carry / overnight

# Which funds() rows (title, lowercase substring, priority order) to read as the
# balance that drives unit tiers. Confirm against your Fyers funds screen.
BALANCE_SOURCE_TITLES = ["total balance", "available balance"]

# ── Demo / paper-trading (fyers_demo branch) ──
# Fyers has no demo account, so open/close are simulated on local disk while all
# read APIs stay real (auth/quotes/optionchain/history) — exercises real throttling.
DEMO_DIR           = BALLOM_DIR / "demo"
DEMO_BROKER_FILE   = DEMO_DIR / "demo_broker.json"
DEMO_START_BALANCE = 90_000    # ₹ simulated starting balance (drives unit tiers)

# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY — RSI+MACD slope engine  (ported from Alcadeias rsimacd_*_wd_exp_h)
#  SHA removed. Entry/force-close/ladder-add decided by per-section weighted slope
#  scores built from RSI + MACD-histogram latest-pivot slopes; profit-book differs
#  by branch (NETTING = book-score reversal; HEDGING = ₹100×units+charges target).
#  See TRADING_LOGIC.md / docs mirror of the 4H·1H·15M decision table.
# ═══════════════════════════════════════════════════════════════════════════════

# ── MACD (RSI+MACD slope entry engine) ──
MACD_FAST   = 12               # fast EMA length (TradingView ta.macd default)
MACD_SLOW   = 26               # slow EMA length
MACD_SIGNAL = 9                # signal EMA length

# ── Per-section score thresholds (RAW weighted-sum units; see *_SCORE_WEIGHTS) ──
# Each decision SECTION has its OWN timeframe set; its score is the RAW weighted sum,
# range ±(2*Σ|weight|). Thresholds below are in those RAW units — change a section's
# timeframes and re-check its threshold.
ENTRY_S       = 2              # |open_score| to OPEN. OPEN range ±4 (H4+M5, w1+w1); +2 = one TF fully aligned.
ENTRY_AGREE_S = 0              # agreement gate: force-close score must not oppose the open (BUY needs fc_score >= +0).
LADDER_S      = 2              # DCA ladder add blocked once OPEN score is this far adverse (BUY add needs open_score > -2).
FORCE_CLOSE_S = 1              # force-close on M15 when EITHER RSI or MACD slope reverses against the basket
                               # (range ±2 for {M15}; BUY close fc_score <= -1, i.e. net-adverse M15 slope).
BOOK_S        = 2              # profit-book (NETTING only): in profit, book when BOTH M5 slopes (RSI+MACD)
                               # reverse (range ±2 for {M5}; BUY book_score <= -2).

# ── Per-section timeframe weights (config-driven). Short keys M5/M15/H1/H4/D1. ──
#   OPEN        -> fresh entry + the DCA ladder-add gate               : H4 + M5   (grey_fill on)
#   BOOK        -> profit-book (NETTING only; HEDGING books via the ₹ target) : M5 (grey_fill on; BOOK_S=2 → both reverse)
#   FORCE_CLOSE -> force-close + the entry agreement-gate veto          : M15      (raw signs; FORCE_CLOSE_S=1 → either reverses)
OPEN_SCORE_WEIGHTS        = {'H4': 1, 'M5': 1}
BOOK_SCORE_WEIGHTS        = {'M5': 1}
FORCE_CLOSE_SCORE_WEIGHTS = {'M15': 1}

# Slope fetch list (auto-derived union of every TF named in any section above),
# mapped short-key -> 'TIMEFRAME_<key>'. Adding a TF to a section dict is enough.
# INDEX slope set (computed on the main index, e.g. NSE:NIFTY50-INDEX): drives OPEN (entry +
# ladder-add gate) and the entry agreement-gate veto (FORCE_CLOSE score at entry). With the
# current sections this is {H4, M5, M15}.
SLOPE_TIMEFRAMES = ['TIMEFRAME_' + _tf for _tf in dict.fromkeys(
    _k for _d in (OPEN_SCORE_WEIGHTS, BOOK_SCORE_WEIGHTS, FORCE_CLOSE_SCORE_WEIGHTS) for _k in _d
)]
# LEG slope set (computed on the ACTIVE CE/PE option's own candles, once a basket is open):
# drives the profit-BOOK and FORCE-CLOSE exits. Union of the two exit sections = {M5, M15}.
LEG_SLOPE_TIMEFRAMES = ['TIMEFRAME_' + _tf for _tf in dict.fromkeys(
    _k for _d in (BOOK_SCORE_WEIGHTS, FORCE_CLOSE_SCORE_WEIGHTS) for _k in _d
)]

# ── Pivot-slope params (RSI + MACD-histogram) ──
PEAK_LOOKBACK = 20             # candles scanned for the latest pivot
PIVOT_WIDTH   = 2              # bars required on each side to confirm a pivot (k)
RSI_PEAK_OB   = 60             # a HIGH pivot on RSI counts only if value >= this (MACD stays unbounded)
RSI_PEAK_OS   = 40             # a LOW  pivot on RSI counts only if value <= this
RSI_GRAPH_BARS = 40            # bars each dashboard RSI/MACD pivot mini-chart plots (> PEAK_LOOKBACK)

REENTRY_COOLDOWN_SECONDS = 300 # block a fresh entry for this long after any close
MIN_HOLD_SECONDS = 180         # after an entry, suppress BOOK + FORCE-CLOSE for this long so the
                               # basket can't open-then-close on a stale leg pivot (anti-vibration).
                               # Adds (DCA ladder) are still allowed; entry is state-gated anyway.

# ═══════════════════════════════════════════════════════════════════════════════
#  CHARGES / TAXES  (exact, from dev_trading constants.py)
# ═══════════════════════════════════════════════════════════════════════════════

# Rates verified against fyers.in/charges-list (2026-11-03). Edit here if Fyers revises.
BROKERAGE_PER_ORDER       = 20.0   # flat ₹20 per executed order (Standard, options)
BROKERAGE_PER_ORDER_PRIME = 15.0   # ₹15 per order with Fyers Prime
STT_OPTIONS_RATE    = 0.0015       # 0.15% on SELL premium (Fyers charges-list)
EXCHANGE_TXN_RATE   = 0.000355299  # 0.0355299% on premium, NSE (incl. IPFT) — per side
GST_RATE            = 0.18         # 18% on (brokerage + exchange txn + SEBI)
STAMP_DUTY_RATE     = 0.00003      # 0.003% on BUY premium
SEBI_PER_CRORE      = 10.0         # ₹10 per crore turnover


def estimate_trade_charges(qty, ltp, num_orders: int = 2) -> float:
    """Estimated round-trip charges (₹) for a Fyers options trade, per the Fyers
    charges list. num_orders = 2 + martingale_count (entry + exit + each add)."""
    turnover  = abs(qty) * ltp
    brokerage = brokerage_per_order() * num_orders    # ₹20 Standard / ₹15 Prime
    stt       = STT_OPTIONS_RATE * turnover          # sell side only
    exchange  = EXCHANGE_TXN_RATE * turnover * 2      # both sides
    sebi      = (turnover * 2 / 1_00_00_000) * SEBI_PER_CRORE
    gst       = GST_RATE * (brokerage + exchange + sebi)
    stamp     = STAMP_DUTY_RATE * turnover            # buy side only
    return round(brokerage + stt + exchange + gst + stamp + sebi, 2)
