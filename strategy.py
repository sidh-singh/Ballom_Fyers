"""
strategy.py — Heiken-Ashi Martingale strategy for dev_trading branch.

Key rules
─────────
• Indices direction determines which leg is active:
    - Indices BULLISH (idx_list[0] == 1) → only CE trades
    - Indices BEARISH (idx_list[0] == 0) → only PE trades
• Entry: SHA momentum aligned with trend + RSI confirmation
• Exit:  profit target OR adverse signal
• Martingale:
    - Level 0 → Level 1:  RSI 1min  oversold  → BUY (fibonacci qty)
    - Level 1 → Level 2:  RSI 5min  oversold  → BUY (fibonacci qty)
    - Level 2 → Level 3:  RSI 15min oversold  → BUY (fibonacci qty)
    - Level 3 (MAX_MARTINGALE_LEVEL) → FORCE CLOSE position

FIX: Each RSI timeframe INDEPENDENTLY triggers its specific martingale
level.  Previously, all three RSI levels were checked with OR logic at
every level, causing premature martingale adds.  Now:
    - mg_level==0: ONLY fires when RSI 1min < OVERSOLD
    - mg_level==1: ONLY fires when RSI 5min < OVERSOLD
    - mg_level==2: ONLY fires when RSI 15min < OVERSOLD

All strategy decisions are logged to JSON state files —
no print/log statements.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from constants import (
    Transaction,
    STRATEGY_HEDGE_INDEX,
    STRATEGY_PRODUCT_TYPE,
    FIBO_SEQUENCE_LENGTH,
    MAX_MARTINGALE_LEVEL,
    GAP_RANGE_LOW,
    GAP_RANGE_HIGH,
    ENTRY_RELATIONSHIP_STATUSES,
    RSI_OVERSOLD,
    estimate_trade_charges,
)
from state_writer import log_strategy_event
from position_tracker import PositionTracker


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderAction:
    """What the strategy wants to do with a single leg (CE or PE)."""
    symbol: str
    status: Transaction     # BUY, SELL, CLOSE_BUY, BUY_WITH_SPECIFIC_VOLUME, DO_NOTHING …
    qty: int                # base lot qty (for new entries) or current qty (for exits)
    pl: float               # unrealised P&L (0 if no position)
    martingale_qty: int     # fibonacci-calculated qty (only for BUY_WITH_SPECIFIC_VOLUME)
    api_total_pl: float = 0.0   # raw `pl` from Fyers API (realized + unrealized)
    position_qty: int = 0       # actual current position qty from API
    ltp: float = 0.0            # last traded price
    avg_price: float = 0.0      # netAvg (blended avg price)

    @property
    def is_actionable(self) -> bool:
        return self.status not in (Transaction.DO_NOTHING, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY CLASS
# ═══════════════════════════════════════════════════════════════════════════════

class HeikenAshiMartingale:
    """
    Stateless strategy evaluator.

    Call `evaluate()` with the latest signal data + position data → get
    (ce_action, pe_action) back.  Then call `execute_orders()` to place
    them via the Fyers object.
    """

    PRODUCT_TYPE    = STRATEGY_PRODUCT_TYPE

    # Pre-compute fibonacci sequence once
    _FIBO = [0, 1]
    for _i in range(2, FIBO_SEQUENCE_LENGTH):
        _FIBO.append(_FIBO[-1] + _FIBO[-2])
    _FIBO = _FIBO[2:]  # [1, 2, 3, 5, 8, 13, 21, 34, 55, …]

    def __init__(self, mode: str = "demo", brake: bool = False,
                 max_balance_usage: float = 0,
                 tracker: PositionTracker | None = None) -> None:
        self.mode = mode
        self.brake = brake
        self.max_balance_usage = max_balance_usage
        self.tracker = tracker
        self._pending_close: dict[str, dict] = {}

    # ── pending-close helpers ──────────────────────────────────────────────

    def mark_pending_close(self, symbol: str, qty: int, pl: float,
                           api_total_pl: float, ltp: float = 0.0,
                           avg_price: float = 0.0) -> None:
        self._pending_close[symbol] = {
            "qty": qty, "pl": pl, "api_total_pl": api_total_pl,
            "ltp": ltp, "avg_price": avg_price,
        }

    def is_pending_close(self, symbol: str) -> bool:
        return symbol in self._pending_close

    def confirm_close(self, symbol: str,
                      current_api_total_pl: float | None = None) -> None:
        """
        Called when position for *symbol* confirmed netQty=0.
        NOW update the tracker's booked_profit.
        """
        info = self._pending_close.pop(symbol, None)
        if info and self.tracker:
            api_pl = (current_api_total_pl
                      if current_api_total_pl is not None
                      else info["api_total_pl"])
            # Guard: DemoFyers deletes closed positions → pl=0.  Fall back
            # to stale value.
            if api_pl == 0.0 and info["api_total_pl"] != 0.0:
                api_pl = info["api_total_pl"]
            effective_pl = self.tracker.get_effective_pl(symbol, api_pl)
            self.tracker.record_close(
                symbol, api_pl, info["qty"], effective_pl,
                ltp=info.get("ltp", 0.0),
                avg_price=info.get("avg_price", 0.0),
            )
            log_strategy_event(
                symbol, "CLOSE", "CLOSE_CONFIRMED",
                qty=info["qty"], pl=effective_pl,
                details=f"Position confirmed closed — tracker updated"
                        f" | api_pl={api_pl:.2f}")

    def cancel_pending_close(self, symbol: str) -> None:
        self._pending_close.pop(symbol, None)

    # ── fibonacci helpers ──────────────────────────────────────────────────────

    @classmethod
    def _fibo_threshold(cls, martingale_count: int, hedge: float) -> float:
        """Loss threshold = fibonacci[level]² × hedge."""
        try:
            multiplier = cls._FIBO[martingale_count]
        except IndexError:
            multiplier = cls._FIBO[-1]
        return (multiplier ** 2) * hedge

    @classmethod
    def _fibo_next_qty(cls, current_qty: int, lot_size: int) -> int:
        """Fibonacci-based next entry qty for martingale."""
        if current_qty <= 0 or lot_size <= 0:
            return lot_size
        current_lots = abs(current_qty) // lot_size
        try:
            idx = cls._FIBO.index(current_lots) + 1
            next_lots = cls._FIBO[idx] if idx < len(cls._FIBO) else cls._FIBO[-1]
        except (ValueError, IndexError):
            next_lots = current_lots + 1
        return int(next_lots * lot_size)

    def _get_martingale_count(self, symbol: str) -> int:
        if self.tracker:
            return self.tracker.get_martingale_count(symbol)
        return 0

    # ── position introspection ─────────────────────────────────────────────────

    @staticmethod
    def _read_position(position_df, symbol: str, product_type: str = "MARGIN"):
        """Return (qty, unrealized_pl, realized_pl, total_pl, ltp, avg_price)."""
        if position_df is None or position_df.empty:
            return 0, 0.0, 0.0, 0.0, 0.0, 0.0
        row = position_df[
            (position_df["symbol"] == symbol)
            & (position_df["productType"] == product_type)
        ]
        if row.empty:
            return 0, 0.0, 0.0, 0.0, 0.0, 0.0
        return (
            int(row["netQty"].iloc[0]),
            float(row["unrealized_profit"].iloc[0]),
            float(row["realized_profit"].iloc[0]),
            float(row["pl"].iloc[0]),
            float(row["ltp"].iloc[0]) if "ltp" in row.columns else 0.0,
            float(row["netAvg"].iloc[0]) if "netAvg" in row.columns else 0.0,
        )

    # ══════════════════════════════════════════════════════════════════════════
    #  EVALUATE — pure signal logic, no side effects
    # ══════════════════════════════════════════════════════════════════════════

    def evaluate(
        self,
        ce_symbol: str,
        pe_symbol: str,
        base_qty: int,
        signal_data: dict,
        position_df,
        hedge: float = STRATEGY_HEDGE_INDEX,
    ) -> tuple[OrderAction, OrderAction]:
        """
        Determine the trading action for CE and PE legs.

        Parameters
        ──────────
        ce_symbol    : e.g. "NFO:NIFTY26FEB26000CE"
        pe_symbol    : e.g. "NFO:NIFTY26FEB25800PE"
        base_qty     : lot size × qty_times
        signal_data  : dict from dev_updater_nifty's signal_state.json
                       Contains SHA power/list, GAP%, relationship, and
                       RSI values for all timeframes.
        position_df  : DataFrame from fyers.position()
        hedge        : ₹ profit target for this pair

        Returns
        ───────
        (ce_action, pe_action) — OrderAction dataclasses
        """
        # ── Extract signal data from dev_updater_nifty ────────────────────
        ce_data = signal_data.get("ce", {})
        pe_data = signal_data.get("pe", {})
        idx_data = signal_data.get("idx", {})

        ce_power = ce_data.get("power", 0)
        ce_list = ce_data.get("list", [0])
        pe_power = pe_data.get("power", 0)
        pe_list = pe_data.get("list", [0])
        idx_power = idx_data.get("power", 0)
        idx_list = idx_data.get("list", [0])

        # Trend SHA data
        ce_trend = signal_data.get("ce_trend", {})
        pe_trend = signal_data.get("pe_trend", {})
        idx_trend_sha = signal_data.get("idx_trend_sha", {})

        ce_t_list = ce_trend.get("list", [])
        pe_t_list = pe_trend.get("list", [])
        idx_t_list = idx_trend_sha.get("list", [])

        # ── GAP% data ─────────────────────────────────────────────────
        ce_gap_info = signal_data.get("ce_gap", {})
        pe_gap_info = signal_data.get("pe_gap", {})
        ce_gap_pct = ce_gap_info.get("gap_pct", 0.0) if isinstance(ce_gap_info, dict) else 0.0
        pe_gap_pct = pe_gap_info.get("gap_pct", 0.0) if isinstance(pe_gap_info, dict) else 0.0

        ce_gap_in_range = GAP_RANGE_LOW <= abs(ce_gap_pct) <= GAP_RANGE_HIGH
        pe_gap_in_range = GAP_RANGE_LOW <= abs(pe_gap_pct) <= GAP_RANGE_HIGH

        # ── SHA Relationship data ─────────────────────────────────────
        ce_rel = signal_data.get("ce_relationship", {})
        pe_rel = signal_data.get("pe_relationship", {})
        ce_rel_status = ce_rel.get("status", "UNKNOWN")
        pe_rel_status = pe_rel.get("status", "UNKNOWN")

        _rel_filter = ENTRY_RELATIONSHIP_STATUSES or set()
        ce_rel_ok = (ce_rel_status in _rel_filter) if _rel_filter else True
        pe_rel_ok = (pe_rel_status in _rel_filter) if _rel_filter else True

        # ── RSI data (from dev_updater_nifty) ─────────────────────────
        # 1min RSI
        ce_rsi = signal_data.get("ce_rsi")
        pe_rsi = signal_data.get("pe_rsi")
        ce_rsi = float(ce_rsi) if ce_rsi is not None else float('nan')
        pe_rsi = float(pe_rsi) if pe_rsi is not None else float('nan')

        # 5min RSI
        ce_rsi_5m = signal_data.get("ce_rsi_5m")
        pe_rsi_5m = signal_data.get("pe_rsi_5m")
        ce_rsi_5m = float(ce_rsi_5m) if ce_rsi_5m is not None else float('nan')
        pe_rsi_5m = float(pe_rsi_5m) if pe_rsi_5m is not None else float('nan')

        # 15min RSI
        ce_rsi_15m = signal_data.get("ce_rsi_15m")
        pe_rsi_15m = signal_data.get("pe_rsi_15m")
        ce_rsi_15m = float(ce_rsi_15m) if ce_rsi_15m is not None else float('nan')
        pe_rsi_15m = float(pe_rsi_15m) if pe_rsi_15m is not None else float('nan')

        # ── Read positions ────────────────────────────────────────────
        ce_qty, ce_unrealized, ce_realized, ce_total_pl, ce_ltp, ce_avg = \
            self._read_position(position_df, ce_symbol, self.PRODUCT_TYPE)
        pe_qty, pe_unrealized, pe_realized, pe_total_pl, pe_ltp, pe_avg = \
            self._read_position(position_df, pe_symbol, self.PRODUCT_TYPE)

        # Effective P&L for the CURRENT cycle
        if self.tracker:
            ce_pl = self.tracker.get_effective_pl(ce_symbol, ce_total_pl)
            pe_pl = self.tracker.get_effective_pl(pe_symbol, pe_total_pl)
        else:
            ce_pl = ce_total_pl
            pe_pl = pe_total_pl

        ce_mg_level = self._get_martingale_count(ce_symbol)
        pe_mg_level = self._get_martingale_count(pe_symbol)

        # Adjusted hedge: hedge + estimated broker charges
        ce_num_orders = 2 + ce_mg_level
        pe_num_orders = 2 + pe_mg_level
        ce_charges = estimate_trade_charges(abs(ce_qty), ce_ltp, ce_num_orders) if ce_qty != 0 else 0.0
        pe_charges = estimate_trade_charges(abs(pe_qty), pe_ltp, pe_num_orders) if pe_qty != 0 else 0.0
        ce_adj_hedge = hedge + ce_charges
        pe_adj_hedge = hedge + pe_charges

        # Defaults — do nothing
        ce_action = OrderAction(
            symbol=ce_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=ce_pl, martingale_qty=0,
            api_total_pl=ce_total_pl, position_qty=ce_qty,
            ltp=ce_ltp, avg_price=ce_avg,
        )
        pe_action = OrderAction(
            symbol=pe_symbol, status=Transaction.DO_NOTHING,
            qty=base_qty, pl=pe_pl, martingale_qty=0,
            api_total_pl=pe_total_pl, position_qty=pe_qty,
            ltp=pe_ltp, avg_price=pe_avg,
        )

        idx_trend = "BULLISH" if idx_list and idx_list[0] == 1 else "BEARISH"
        log_strategy_event(
            ce_symbol.split(":")[1] if ":" in ce_symbol else ce_symbol,
            "EVAL", "ANALYSIS",
            details=f"Idx={idx_trend} "
                    f"CE_pwr={ce_power}/7 PE_pwr={pe_power}/7 "
                    f"GAP: CE={ce_gap_pct:.2f}% PE={pe_gap_pct:.2f}%"
                    f" | REL: CE={ce_rel_status} PE={pe_rel_status}"
                    f" | RSI_1m: CE={ce_rsi:.1f} PE={pe_rsi:.1f}"
                    f" | RSI_5m: CE={ce_rsi_5m:.1f} PE={pe_rsi_5m:.1f}"
                    f" | RSI_15m: CE={ce_rsi_15m:.1f} PE={pe_rsi_15m:.1f}"
                    f" | mg_level: CE={ce_mg_level} PE={pe_mg_level}",
        )

        # ─────────────────────────────────────────────────────────────────────
        #  PENDING-CLOSE HANDLING
        # ─────────────────────────────────────────────────────────────────────

        if self.is_pending_close(ce_symbol):
            if ce_qty == 0:
                self.confirm_close(ce_symbol, current_api_total_pl=ce_total_pl)
            else:
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "RETRY_CLOSE",
                                   qty=ce_qty, pl=ce_pl,
                                   details="Pending close not yet filled — retrying")
                if self.is_pending_close(pe_symbol):
                    if pe_qty == 0:
                        self.confirm_close(pe_symbol, current_api_total_pl=pe_total_pl)
                    else:
                        pe_action.status = Transaction.CLOSE_BUY
                        pe_action.qty = pe_qty
                return ce_action, pe_action

        if self.is_pending_close(pe_symbol):
            if pe_qty == 0:
                self.confirm_close(pe_symbol, current_api_total_pl=pe_total_pl)
            else:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "RETRY_CLOSE",
                                   qty=pe_qty, pl=pe_pl,
                                   details="Pending close not yet filled — retrying")
                return ce_action, pe_action

        # ─────────────────────────────────────────────────────────────────────
        #  RSI OVERSOLD GUARDS FOR ENTRY
        # ─────────────────────────────────────────────────────────────────────
        # Block new entries when ANY RSI timeframe is oversold.
        _ce_any_rsi_oversold = (
            (not math.isnan(ce_rsi) and ce_rsi < RSI_OVERSOLD)
            or (not math.isnan(ce_rsi_5m) and ce_rsi_5m < RSI_OVERSOLD)
            or (not math.isnan(ce_rsi_15m) and ce_rsi_15m < RSI_OVERSOLD)
        )
        _pe_any_rsi_oversold = (
            (not math.isnan(pe_rsi) and pe_rsi < RSI_OVERSOLD)
            or (not math.isnan(pe_rsi_5m) and pe_rsi_5m < RSI_OVERSOLD)
            or (not math.isnan(pe_rsi_15m) and pe_rsi_15m < RSI_OVERSOLD)
        )

        # ─────────────────────────────────────────────────────────────────────
        #  MARTINGALE RSI FLAGS — FIXED: each timeframe is INDEPENDENT
        #
        #  mg_level 0 → 1:  ONLY triggered by RSI 1min  oversold
        #  mg_level 1 → 2:  ONLY triggered by RSI 5min  oversold
        #  mg_level 2 → 3:  ONLY triggered by RSI 15min oversold
        #
        #  We check BOTH legs' RSI (cross-leg) for each timeframe because
        #  if you're holding CE and PE RSI goes oversold, it indicates
        #  broad market stress that affects the active position.
        # ─────────────────────────────────────────────────────────────────────
        _ce_1m_oversold = (not math.isnan(ce_rsi) and ce_rsi < RSI_OVERSOLD)
        _pe_1m_oversold = (not math.isnan(pe_rsi) and pe_rsi < RSI_OVERSOLD)
        _any_1m_oversold = _ce_1m_oversold or _pe_1m_oversold

        _ce_5m_oversold = (not math.isnan(ce_rsi_5m) and ce_rsi_5m < RSI_OVERSOLD)
        _pe_5m_oversold = (not math.isnan(pe_rsi_5m) and pe_rsi_5m < RSI_OVERSOLD)
        _any_5m_oversold = _ce_5m_oversold or _pe_5m_oversold

        _ce_15m_oversold = (not math.isnan(ce_rsi_15m) and ce_rsi_15m < RSI_OVERSOLD)
        _pe_15m_oversold = (not math.isnan(pe_rsi_15m) and pe_rsi_15m < RSI_OVERSOLD)
        _any_15m_oversold = _ce_15m_oversold or _pe_15m_oversold

        # ─────────────────────────────────────────────────────────────────────
        #  ENTRY / EXIT / MARTINGALE
        # ─────────────────────────────────────────────────────────────────────

        if ce_qty == 0 and pe_qty == 0:
            # ── ENTRY LOGIC ───────────────────────────────────────────
            if (ce_list and ce_list[0] == 1) and (idx_list and idx_list[0] == 1) \
                    and (ce_t_list and ce_t_list[0] == 1) \
                    and ce_gap_in_range and ce_rel_ok:
                if _ce_any_rsi_oversold:
                    log_strategy_event(ce_symbol, "CE", "ENTRY_BLOCKED_RSI_OVERSOLD",
                                       qty=base_qty,
                                       details=f"Entry blocked: RSI oversold "
                                               f"(1m={ce_rsi:.1f} 5m={ce_rsi_5m:.1f} 15m={ce_rsi_15m:.1f})")
                else:
                    ce_action.status = Transaction.BUY
                    log_strategy_event(ce_symbol, "CE", "ENTRY_BUY",
                                       qty=base_qty,
                                       details=f"Signal+Trend bullish, IDX bullish, "
                                               f"GAP={ce_gap_pct:.2f}% REL={ce_rel_status}")

            elif (pe_list and pe_list[0] == 1) and (idx_list and idx_list[0] == 0) \
                    and (pe_t_list and pe_t_list[0] == 1) \
                    and pe_gap_in_range and pe_rel_ok:
                if _pe_any_rsi_oversold:
                    log_strategy_event(pe_symbol, "PE", "ENTRY_BLOCKED_RSI_OVERSOLD",
                                       qty=base_qty,
                                       details=f"Entry blocked: RSI oversold "
                                               f"(1m={pe_rsi:.1f} 5m={pe_rsi_5m:.1f} 15m={pe_rsi_15m:.1f})")
                else:
                    pe_action.status = Transaction.BUY
                    log_strategy_event(pe_symbol, "PE", "ENTRY_BUY",
                                       qty=base_qty,
                                       details=f"Signal+Trend bullish, IDX bearish, "
                                               f"GAP={pe_gap_pct:.2f}% REL={pe_rel_status}")

        elif ce_qty > 0:
            # ── CE EXIT / MARTINGALE ──────────────────────────────────
            if ce_pl > ce_adj_hedge:
                # TAKE PROFIT
                ce_action.status = Transaction.CLOSE_BUY
                ce_action.qty = ce_qty
                log_strategy_event(ce_symbol, "CE", "EXIT_PROFIT",
                                   qty=ce_qty, pl=ce_pl,
                                   details=f"P&L {ce_pl:.2f} > adj_target {ce_adj_hedge:.2f}"
                                           f" (hedge={hedge} + charges={ce_charges:.2f})")

            # ── MARTINGALE LEVEL 0 → 1: RSI 1min ONLY ────────────────
            elif ce_mg_level == 0 and _any_1m_oversold:
                mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                ce_action.qty = ce_qty
                ce_action.martingale_qty = mg_qty
                _trigger = (f"CE_1m={ce_rsi:.1f}" if _ce_1m_oversold
                            else f"PE_1m={pe_rsi:.1f}")
                log_strategy_event(
                    ce_symbol, "CE", "MARTINGALE_BUY_L0_RSI_1M",
                    qty=mg_qty, pl=ce_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={ce_mg_level} → {ce_mg_level+1}, fibo_qty={mg_qty})")

            # ── MARTINGALE LEVEL 1 → 2: RSI 5min ONLY ────────────────
            elif ce_mg_level == 1 and _any_5m_oversold:
                mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                ce_action.qty = ce_qty
                ce_action.martingale_qty = mg_qty
                _trigger = (f"CE_5m={ce_rsi_5m:.1f}" if _ce_5m_oversold
                            else f"PE_5m={pe_rsi_5m:.1f}")
                log_strategy_event(
                    ce_symbol, "CE", "MARTINGALE_BUY_L1_RSI_5M",
                    qty=mg_qty, pl=ce_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={ce_mg_level} → {ce_mg_level+1}, fibo_qty={mg_qty})")

            # ── MARTINGALE LEVEL 2 → 3: RSI 15min ONLY ───────────────
            elif ce_mg_level == 2 and _any_15m_oversold:
                mg_qty = self._fibo_next_qty(ce_qty, base_qty)
                ce_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                ce_action.qty = ce_qty
                ce_action.martingale_qty = mg_qty
                _trigger = (f"CE_15m={ce_rsi_15m:.1f}" if _ce_15m_oversold
                            else f"PE_15m={pe_rsi_15m:.1f}")
                log_strategy_event(
                    ce_symbol, "CE", "MARTINGALE_BUY_L2_RSI_15M",
                    qty=mg_qty, pl=ce_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={ce_mg_level} → {ce_mg_level+1}, fibo_qty={mg_qty})")

        elif pe_qty > 0:
            # ── PE EXIT / MARTINGALE ──────────────────────────────────
            if pe_pl > pe_adj_hedge:
                pe_action.status = Transaction.CLOSE_BUY
                pe_action.qty = pe_qty
                log_strategy_event(pe_symbol, "PE", "EXIT_PROFIT",
                                   qty=pe_qty, pl=pe_pl,
                                   details=f"P&L {pe_pl:.2f} > adj_target {pe_adj_hedge:.2f}"
                                           f" (hedge={hedge} + charges={pe_charges:.2f})")

            # ── MARTINGALE LEVEL 0 → 1: RSI 1min ONLY ────────────────
            elif pe_mg_level == 0 and _any_1m_oversold:
                mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                pe_action.qty = pe_qty
                pe_action.martingale_qty = mg_qty
                _trigger = (f"CE_1m={ce_rsi:.1f}" if _ce_1m_oversold
                            else f"PE_1m={pe_rsi:.1f}")
                log_strategy_event(
                    pe_symbol, "PE", "MARTINGALE_BUY_L0_RSI_1M",
                    qty=mg_qty, pl=pe_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={pe_mg_level} → {pe_mg_level+1}, fibo_qty={mg_qty})")

            # ── MARTINGALE LEVEL 1 → 2: RSI 5min ONLY ────────────────
            elif pe_mg_level == 1 and _any_5m_oversold:
                mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                pe_action.qty = pe_qty
                pe_action.martingale_qty = mg_qty
                _trigger = (f"CE_5m={ce_rsi_5m:.1f}" if _ce_5m_oversold
                            else f"PE_5m={pe_rsi_5m:.1f}")
                log_strategy_event(
                    pe_symbol, "PE", "MARTINGALE_BUY_L1_RSI_5M",
                    qty=mg_qty, pl=pe_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={pe_mg_level} → {pe_mg_level+1}, fibo_qty={mg_qty})")

            # ── MARTINGALE LEVEL 2 → 3: RSI 15min ONLY ───────────────
            elif pe_mg_level == 2 and _any_15m_oversold:
                mg_qty = self._fibo_next_qty(pe_qty, base_qty)
                pe_action.status = Transaction.BUY_WITH_SPECIFIC_VOLUME
                pe_action.qty = pe_qty
                pe_action.martingale_qty = mg_qty
                _trigger = (f"CE_15m={ce_rsi_15m:.1f}" if _ce_15m_oversold
                            else f"PE_15m={pe_rsi_15m:.1f}")
                log_strategy_event(
                    pe_symbol, "PE", "MARTINGALE_BUY_L2_RSI_15M",
                    qty=mg_qty, pl=pe_pl,
                    details=f"RSI {_trigger} < {RSI_OVERSOLD} "
                            f"(level={pe_mg_level} → {pe_mg_level+1}, fibo_qty={mg_qty})")

        return ce_action, pe_action

    # ══════════════════════════════════════════════════════════════════════════
    #  BALANCE LIMIT HELPERS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _get_utilized_balance(fyers) -> tuple:
        try:
            funds_response = fyers.funds()
            fund_limit = funds_response.get("fund_limit", [])
            utilized_equity = 0
            for item in fund_limit:
                title = item.get("title", "").lower()
                if "utilized" in title or "used" in title or item.get("id") == 2:
                    utilized_equity += abs(item.get("equityAmount", 0))
            return utilized_equity, 0, utilized_equity
        except Exception:
            return 0, 0, 0

    def _check_balance_limit(self, fyers, order_type: Transaction) -> tuple:
        if self.max_balance_usage <= 0:
            return True, 0
        exit_types = (
            Transaction.CLOSE, Transaction.CLOSE_BUY, Transaction.CLOSE_SELL,
            Transaction.DO_NOTHING, Transaction.RESET,
        )
        if order_type in exit_types:
            return True, 0

        _, _, total_utilized = self._get_utilized_balance(fyers)
        if total_utilized >= self.max_balance_usage:
            log_strategy_event(
                "BALANCE", "CHECK", "LIMIT_EXHAUSTED",
                details=f"Utilized ₹{total_utilized:,.2f} >= Limit ₹{self.max_balance_usage:,.2f}",
            )
            return False, total_utilized
        return True, total_utilized

    # ══════════════════════════════════════════════════════════════════════════
    #  FORCE-CLOSE — fallback when martingale is blocked
    # ══════════════════════════════════════════════════════════════════════════

    def _force_close(self, fyers, action: OrderAction, label: str, reason: str) -> None:
        qty = action.position_qty or action.qty
        if qty == 0:
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_SKIP",
                               details=f"No position to close | {reason}")
            return

        if qty > 0:
            resp = fyers.sell(action.symbol, abs(qty))
        else:
            resp = fyers.buy(action.symbol, abs(qty))

        order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
        if order_ok:
            self.mark_pending_close(
                action.symbol, abs(qty), action.pl, action.api_total_pl,
                ltp=action.ltp, avg_price=action.avg_price)
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_SENT",
                               qty=abs(qty), pl=action.pl,
                               details=f"Position closed — {reason} | {str(resp)}")
        else:
            log_strategy_event(action.symbol, label, "FORCE_CLOSE_REJECTED",
                               qty=abs(qty), pl=action.pl,
                               details=f"Close REJECTED — will retry | {reason} | {str(resp)}")

    # ══════════════════════════════════════════════════════════════════════════
    #  EXECUTE ORDERS — side-effect: calls fyers.buy() / fyers.sell()
    # ══════════════════════════════════════════════════════════════════════════

    def execute_orders(self, fyers, ce_action: OrderAction, pe_action: OrderAction) -> None:
        """Place orders for CE and PE legs. Exits first, then entries."""
        exit_types = (Transaction.CLOSE_BUY, Transaction.CLOSE_SELL)

        # Phase 1: exits first
        if ce_action.status in exit_types:
            self._execute_single(fyers, ce_action, "CE")
        if pe_action.status in exit_types:
            self._execute_single(fyers, pe_action, "PE")

        # Phase 2: entries / martingale
        if ce_action.status not in exit_types:
            self._execute_single(fyers, ce_action, "CE")
        if pe_action.status not in exit_types:
            self._execute_single(fyers, pe_action, "PE")

    def _execute_single(self, fyers, action: OrderAction, label: str) -> None:
        s = action.status
        sym = action.symbol

        if s == Transaction.DO_NOTHING:
            return

        if self.brake and s in (Transaction.BUY, Transaction.SELL):
            log_strategy_event(sym, label, "BRAKE_BLOCKED",
                               details=f"{s.name} blocked — brake is ON")
            return

        entry_types = (
            Transaction.BUY, Transaction.SELL,
            Transaction.BUY_WITH_SPECIFIC_VOLUME,
            Transaction.SELL_WITH_SPECIFIC_VOLUME,
        )
        martingale_types = (
            Transaction.BUY_WITH_SPECIFIC_VOLUME,
            Transaction.SELL_WITH_SPECIFIC_VOLUME,
        )
        if s in entry_types:
            can_trade, utilized = self._check_balance_limit(fyers, s)
            if not can_trade:
                log_strategy_event(
                    sym, label, "BALANCE_BLOCKED",
                    details=f"{s.name} blocked — utilized ₹{utilized:,.2f} >= limit ₹{self.max_balance_usage:,.2f}")
                if s in martingale_types:
                    self._force_close(fyers, action, label,
                                      reason=f"Martingale blocked by balance limit")
                return

        # ── BUY ────────────────────────────────────────────────────────────
        if s == Transaction.BUY:
            resp = fyers.buy(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, 1,
                                          ltp=action.ltp, avg_price=action.ltp)
            log_strategy_event(sym, label, "BUY_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── SELL ───────────────────────────────────────────────────────────
        elif s == Transaction.SELL:
            resp = fyers.sell(sym, action.qty)
            if self.tracker:
                self.tracker.record_entry(sym, action.qty, -1,
                                          ltp=action.ltp, avg_price=action.ltp)
            log_strategy_event(sym, label, "SELL_EXECUTED",
                               qty=action.qty, details=str(resp))

        # ── CLOSE_BUY ─────────────────────────────────────────────────────
        elif s == Transaction.CLOSE_BUY:
            resp = fyers.sell(sym, action.qty)
            order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
            if order_ok:
                self.mark_pending_close(
                    sym, action.qty, action.pl, action.api_total_pl,
                    ltp=action.ltp, avg_price=action.avg_price)
                log_strategy_event(sym, label, "CLOSE_BUY_SENT",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order accepted — pending fill | {str(resp)}")
            else:
                log_strategy_event(sym, label, "CLOSE_BUY_REJECTED",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order REJECTED — will retry | {str(resp)}")

        # ── CLOSE_SELL ─────────────────────────────────────────────────────
        elif s == Transaction.CLOSE_SELL:
            resp = fyers.buy(sym, action.qty)
            order_ok = isinstance(resp, dict) and resp.get("s") == "ok"
            if order_ok:
                self.mark_pending_close(
                    sym, action.qty, action.pl, action.api_total_pl,
                    ltp=action.ltp, avg_price=action.avg_price)
                log_strategy_event(sym, label, "CLOSE_SELL_SENT",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order accepted — pending fill | {str(resp)}")
            else:
                log_strategy_event(sym, label, "CLOSE_SELL_REJECTED",
                                   qty=action.qty, pl=action.pl,
                                   details=f"Order REJECTED — will retry | {str(resp)}")

        # ── BUY_WITH_SPECIFIC_VOLUME (martingale add long) ────────────────
        elif s == Transaction.BUY_WITH_SPECIFIC_VOLUME:
            current_mg = self.tracker.get_martingale_count(sym) if self.tracker else 0
            if current_mg >= MAX_MARTINGALE_LEVEL:
                log_strategy_event(sym, label, "MARTINGALE_BLOCKED",
                                   details=f"mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL} — closing")
                self._force_close(fyers, action, label,
                                  reason=f"Hard cap mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL}")
                return
            fibo_qty = action.martingale_qty
            resp = fyers.buy(sym, fibo_qty)
            if self.tracker:
                self.tracker.record_martingale(
                    sym, fibo_qty, abs(action.position_qty),
                    1, action.pl, action.api_total_pl)
            log_strategy_event(sym, label, "MARTINGALE_BUY_EXECUTED",
                               qty=fibo_qty,
                               details=f"fibo_qty={fibo_qty} | {str(resp)}")

        # ── SELL_WITH_SPECIFIC_VOLUME ─────────────────────────────────────
        elif s == Transaction.SELL_WITH_SPECIFIC_VOLUME:
            current_mg = self.tracker.get_martingale_count(sym) if self.tracker else 0
            if current_mg >= MAX_MARTINGALE_LEVEL:
                log_strategy_event(sym, label, "MARTINGALE_BLOCKED",
                                   details=f"mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL} — closing")
                self._force_close(fyers, action, label,
                                  reason=f"Hard cap mg_level={current_mg} >= MAX={MAX_MARTINGALE_LEVEL}")
                return
            fibo_qty = action.martingale_qty
            resp = fyers.sell(sym, fibo_qty)
            if self.tracker:
                self.tracker.record_martingale(
                    sym, fibo_qty, abs(action.position_qty),
                    -1, action.pl, action.api_total_pl)
            log_strategy_event(sym, label, "MARTINGALE_SELL_EXECUTED",
                               qty=fibo_qty,
                               details=f"fibo_qty={fibo_qty} | {str(resp)}")

        else:
            log_strategy_event(sym, label, "UNHANDLED",
                               details=f"Unhandled status: {s.name}")
