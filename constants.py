"""
constants.py — Shared enums, dataclasses, and column definitions for dev_trading.

Only contains values that are actively used across the codebase.
This branch focuses on trading logic — no SHA/RSI computations here.
Signal data is read from dev_updater_nifty's signal_state.json.
"""

from enum import Enum
from dataclasses import dataclass
from datetime import time as dt_time
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
#  FILE PATHS
# ═══════════════════════════════════════════════════════════════════════════════

SYMBOLS_JSON         = Path(__file__).resolve().parent / "symbols.json"
OPTION_PAIRS_JSON    = Path("C:/Ballom_FYR/option_pairs.json")

# ── Token (READ-ONLY from dev_scanner) ─────────────────────────────────────────
TOKEN_DIR            = Path("C:/Ballom_FYR")
TOKEN_FILE           = TOKEN_DIR / "fyers_token.json"

# ── Signal state (READ-ONLY from dev_updater_nifty) ───────────────────────────
# The updater writes this file; we read it for SHA + RSI indicators.
SIGNAL_STATE_DIR_BASE = Path("C:/Ballom_FYR/state")


def get_signal_state_file(mode: str) -> Path:
    """Return the signal_state.json path written by dev_updater_nifty."""
    d = SIGNAL_STATE_DIR_BASE / ("live" if mode == "live" else "demo")
    return d / "signal_state.json"


# ── State file directories (mode-separated so demo & live never clash) ─────────
STATE_DIR_BASE       = Path("C:/Ballom_FYR/state")
STATE_DIR_DEMO       = STATE_DIR_BASE / "demo"
STATE_DIR_LIVE       = STATE_DIR_BASE / "live"

# ── Cache (holiday data, CSV downloads) ────────────────────────────────────────
CACHE_DIR            = Path("C:/Ballom_FYR/cache")


def get_state_dir(mode: str) -> Path:
    """Return the state directory for the given mode."""
    return STATE_DIR_LIVE if mode == "live" else STATE_DIR_DEMO


# ═══════════════════════════════════════════════════════════════════════════════
#  TRADING TIME WINDOWS
# ═══════════════════════════════════════════════════════════════════════════════

INDICES_START   = dt_time(9, 15)
INDICES_END     = dt_time(15, 30)


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY TUNING PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

# Profit target (₹) — configurable per-symbol in symbols.json via "hedge" key.
STRATEGY_HEDGE_INDEX      = 500    # ₹ profit target for index option pairs
STRATEGY_PRODUCT_TYPE     = "MARGIN"
FIBO_SEQUENCE_LENGTH      = 25     # Length of fibonacci sequence for martingale
MAX_MARTINGALE_LEVEL      = 3      # Hard cap: max martingale adds (entry + 3 = close on 4th)

# GAP% parameters (gap between Signal SHA and Trend SHA)
GAP_RANGE_LOW       = 0.5     # below this → SHAs nearly overlapping
GAP_RANGE_HIGH      = 2.5     # above this → over-extended

# SHA Relationship filter for entry
ENTRY_RELATIONSHIP_STATUSES: set[str] = {"DIVERGING"}


# ═══════════════════════════════════════════════════════════════════════════════
#  RSI PARAMETERS  (used for martingale trigger)
# ═══════════════════════════════════════════════════════════════════════════════
# RSI values come from dev_updater_nifty's signal_state.json.
# No RSI computation in this branch.

RSI_OVERSOLD        = 30      # below this → trigger martingale add
RSI_OVERBOUGHT      = 70      # above this → reserved for future use


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP TIMING
# ═══════════════════════════════════════════════════════════════════════════════

INNER_LOOP_INTERVAL = 1   # seconds between each strategy evaluation cycle


# ═══════════════════════════════════════════════════════════════════════════════
#  COMMISSION / TAX ESTIMATION
# ═══════════════════════════════════════════════════════════════════════════════

BROKERAGE_PER_ORDER   = 20.0      # ₹ flat per executed order
STT_OPTIONS_RATE      = 0.000625  # 0.0625% on sell-side premium
EXCHANGE_TXN_RATE     = 0.000495  # ~0.0495% per side (NSE F&O)
GST_RATE              = 0.18      # 18% on (brokerage + exchange + SEBI)
STAMP_DUTY_RATE       = 0.00003   # ~0.003% on buy-side turnover
SEBI_PER_CRORE        = 10.0      # ₹10 per crore turnover


def estimate_trade_charges(
    qty: int,
    ltp: float,
    num_orders: int = 2,
) -> float:
    """
    Estimate total broker charges for a trade cycle (₹).

    Parameters
    ──────────
    qty         : Position quantity being closed.
    ltp         : Last traded price of the option.
    num_orders  : Total orders in the cycle (entry + martingale adds + close).

    Returns
    ───────
    Estimated total ₹ charges (brokerage + STT + exchange + GST + stamp).
    """
    if qty <= 0 or ltp <= 0:
        return 0.0

    turnover = abs(qty) * ltp

    brokerage = BROKERAGE_PER_ORDER * num_orders
    stt       = STT_OPTIONS_RATE * turnover                 # sell side
    exchange  = EXCHANGE_TXN_RATE * turnover * 2            # both sides
    sebi      = (turnover * 2 / 1_00_00_000) * SEBI_PER_CRORE
    gst       = GST_RATE * (brokerage + exchange + sebi)
    stamp     = STAMP_DUTY_RATE * turnover                  # buy side

    return round(brokerage + stt + exchange + gst + stamp + sebi, 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTIVE SYMBOLS  (which symbols this branch trades — NIFTY only)
# ═══════════════════════════════════════════════════════════════════════════════

ACTIVE_SYMBOLS: set[str] = {"NIFTY"}


# ═══════════════════════════════════════════════════════════════════════════════
#  TRANSACTION ENUM
# ═══════════════════════════════════════════════════════════════════════════════

class Transaction(Enum):
    BUY = 1
    SELL = -1
    BUY_WITH_SPECIFIC_VOLUME = 40
    SELL_WITH_SPECIFIC_VOLUME = 41
    CLOSE = 0
    CLOSE_BUY = 21
    CLOSE_SELL = 22
    DO_NOTHING = 2
    RESET = 8


# ═══════════════════════════════════════════════════════════════════════════════
#  COLUMN DEFINITIONS  (Fyers API response shapes)
# ═══════════════════════════════════════════════════════════════════════════════

ORDER_COLS = [
    'id', 'exchOrdId', 'symbol', 'qty', 'remainingQuantity', 'filledQty',
    'status', 'slNo', 'message', 'segment', 'limitPrice', 'stopPrice',
    'productType', 'type', 'side', 'disclosedQty', 'orderValidity',
    'orderDateTime', 'parentId', 'tradedPrice', 'source', 'fytoken',
    'offlineOrder', 'pan', 'clientId', 'exchange', 'instrument',
    'discloseQty', 'orderTag',
]

TRADE_COLS = [
    'symbol', 'row', 'orderDateTime', 'orderNumber', 'tradeNumber',
    'tradePrice', 'tradeValue', 'tradedQty', 'side', 'productType',
    'exchangeOrderNo', 'segment', 'exchange', 'fyToken', 'orderTag',
]

POSITION_COL = [
    'symbol', 'id', 'buyAvg', 'buyQty', 'sellAvg', 'sellQty', 'netAvg',
    'netQty', 'side', 'qty', 'productType', 'realized_profit', 'pl',
    'crossCurrency', 'rbiRefRate', 'qtyMulti_com', 'segment', 'exchange',
    'unrealized_profit', 'slNo', 'ltp', 'fytoken', 'cfBuyQty', 'cfSellQty',
    'dayBuyQty', 'daySellQty',
]

SYMBOLS_COLS = [
    'Fytoken', 'Symbol Details', 'Exchange Instrument type',
    'Minimum lot size', 'Tick size', 'ISIN', 'Trading Session',
    'Last update date', 'Expiry date', 'Symbol ticker', 'Exchange',
    'Segment', 'Scrip code', 'Underlying symbol', 'Underlying scrip code',
    'Strike price', 'Option type', 'Underlying FyToken',
    'Reserved column1', 'Reserved column2', 'Reserved column3',
]


# ═══════════════════════════════════════════════════════════════════════════════
#  DATACLASSES  (API payloads & response wrappers)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlaceOrder:
    symbol: str
    qty: int
    type: int
    side: int
    productType: str
    limitPrice: float
    stopPrice: float
    validity: str
    disclosedQty: int
    stopLoss: float
    takeProfit: float
    offlineOrder: bool
    orderTag: str


@dataclass
class CloseBySymbol:
    id: list


@dataclass
class CloseBySection:
    segment: list
    side: list
    productType: list


@dataclass
class OverallPosition:
    count_total: int
    count_open: int
    pl_total: float
    pl_realized: float
    pl_unrealized: float
