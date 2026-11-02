"""
symbol_worker.py — one trading thread per symbol (Loop-2). ARCHITECTURE §5.

Implements the HAM strategy on Fyers NETTING (buy-only), per TRADING_LOGIC.md:
  • SHA signal + trend and multi-timeframe RSI are computed on the INDEX.
  • Strategy BUY→CE leg, SELL→PE leg; BUY_MORE/SELL_MORE add+average; CLOSE sells.
  • Per-cycle P&L via effective_pl = api_pl − booked_profit (PositionTracker).
  • Balance-tiered units: entry = units×lot, adds = fib×units×lot.
  • Close when effective_pl > HEDGE_PER_UNIT×units + charges (tax-aware).

⚠️ Opening/closing places REAL orders when run live. Several edges are marked
TODO for live hardening (per-bar RSI caching, H6 resample, partial-fill retries).
"""

from __future__ import annotations

import logging
import threading

from constants import (
    MARKET_HOURS, WORKER_IDLE_SLEEP, now_ist,
    SHA_TIMEFRAME, SHA_CANDLE_COUNT, SHA_LENGTH, SHA_MA_TYPE,
    SHA_TREND_LENGTH, SHA_TREND_MA_TYPE,
    RSI_LENGTH, RSI_MA_TYPE, RSI_CANDLE_COUNT,
    RSI_MTF_TIMEFRAMES, RSI_DCA_LADDER_TIMEFRAMES, RSI_FINAL_CLOSE_TIMEFRAME,
    TIMEFRAME_TO_FYERS_RES,
    STEP_UP_BALANCE, UNIT_MAX_LIMIT, HEDGE_PER_UNIT, PRODUCT_TYPE,
    estimate_trade_charges,
)
from indicator import Indicator
from strategy import Strategy, Signal
from fyers_api import FyersAPI
import scanner
from state_writer import log_strategy_event

logger = logging.getLogger("worker")

# contracts-per-unit fib ladder (entry=fib[0], add at level L uses fib[L+1])
FIB = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]


class SymbolWorker(threading.Thread):
    def __init__(self, cfg: dict, ctx) -> None:
        super().__init__(name=f"worker-{cfg['symbol']}", daemon=True)
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.asset_type = cfg.get("asset_type", "INDEX")
        self.underlying = cfg["underlying"]
        self.step_up = cfg.get("step_up_balance", STEP_UP_BALANCE)
        self.max_limit = cfg.get("unit_max_limit", UNIT_MAX_LIMIT)
        self.ctx = ctx
        self.indicator = Indicator()
        self.strategy = Strategy()
        self.units = 1
        self.active: dict | None = None     # {"side": "CE"|"PE", "symbol": leg_sym}
        self.pending_close = False
        self._last_qty = 0

    # ── thread main ─────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("[%s] worker started (%s)", self.symbol, self.asset_type)
        self._recover_active()
        while not self.ctx.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception("[%s] tick error: %s", self.symbol, e)
            self.ctx.stop_event.wait(WORKER_IDLE_SLEEP)
        logger.info("[%s] worker stopped", self.symbol)

    # ── one iteration ───────────────────────────────────────────────────────
    def _tick(self) -> None:
        if self.ctx.brake_event.is_set():
            return
        if not self.ctx.is_trading_day(now_ist().date()):
            return
        if not self._in_window():
            return

        # finalize a close in progress first (pt 6: close-first priority)
        if self.active and self.pending_close:
            self._confirm_close()
            if self.active:
                return

        source_df = self._index_candles()
        if source_df is None or len(source_df) < max(SHA_TREND_LENGTH * 3, 60):
            logger.info("[%s] insufficient index candles", self.symbol)
            return
        sha = self.indicator.calculate_sha_v3(source_df, SHA_LENGTH, SHA_MA_TYPE)
        sha_trend = self.indicator.calculate_sha_v3(source_df, SHA_TREND_LENGTH, SHA_TREND_MA_TYPE)

        buy_pos, sell_pos, close_threshold, count = self._leg_state_and_threshold()
        rsi_mtf = self._rsi_mtf(count)
        rsi_1m = rsi_mtf.get("TIMEFRAME_M1", 50.0)

        buy_signal, sell_signal, _ = self.strategy.calculate_signal(
            source_df, sha, sha_trend, buy_pos, sell_pos, self.units,
            close_threshold=close_threshold, rsi_value=rsi_1m, rsi_mtf=rsi_mtf,
        )
        self._execute(buy_signal, sell_signal)

    # ── leg state → strategy inputs ─────────────────────────────────────────
    def _leg_state_and_threshold(self):
        """Return (buy_positions, sell_positions, close_threshold, count)."""
        if self.active is None:
            # flat → re-derive units from balance (Alcadeias: only when flat)
            bal = self.ctx.api.get_balance()
            self.units = max(1, min(int(bal // self.step_up) + 1, self.max_limit))
            return None, None, HEDGE_PER_UNIT * self.units, 0

        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        net_qty = abs(int(pos.get("netQty", 0))) if pos else 0
        api_pl = float(pos.get("pl", 0.0)) if pos else 0.0
        ltp = float(pos.get("ltp", 0.0)) if pos else 0.0
        eff_pl = self.ctx.tracker.get_effective_pl(leg_sym, api_pl)
        mg = self.ctx.tracker.get_martingale_count(leg_sym)
        count = mg + 1
        charges = estimate_trade_charges(net_qty or 1, ltp or 1, num_orders=2 + mg)
        threshold = HEDGE_PER_UNIT * self.units + charges

        leg_dict = {
            "count": count, "total_profit": eff_pl, "total_volume": net_qty,
            "first_profit": eff_pl, "last_profit": eff_pl,
            "first_volume": net_qty, "last_volume": net_qty,
        }
        if self.active["side"] == "CE":
            return leg_dict, None, threshold, count
        return None, leg_dict, threshold, count

    # ── order execution ─────────────────────────────────────────────────────
    def _execute(self, buy_signal, sell_signal) -> None:
        # FLAT → fresh entry (BUY=CE, SELL=PE)
        if self.active is None:
            if buy_signal == Signal.BUY:
                self._open_leg("CE")
            elif sell_signal == Signal.SELL:
                self._open_leg("PE")
            return

        side = self.active["side"]
        if side == "CE":
            if buy_signal == Signal.BUY_MORE:
                self._add_leg()
            elif buy_signal == Signal.CLOSE_BUY:
                self._close_leg()
        else:  # PE
            if sell_signal == Signal.SELL_MORE:
                self._add_leg()
            elif sell_signal == Signal.CLOSE_SELL:
                self._close_leg()

    def _open_leg(self, side: str) -> None:
        pair = scanner.scan_best_pair(self.cfg, self.ctx.api, self.ctx.meter)
        if not pair:
            log_strategy_event(self.symbol, side, "SCAN_EMPTY", details="no pair for entry")
            return
        leg_sym = pair["CE_Symbol"] if side == "CE" else pair["PE_Symbol"]
        lot = self._lot_size(leg_sym)
        if not lot:
            return
        qty = self.units * lot
        resp = self.ctx.api.buy(leg_sym, qty)
        if not self._ok(resp):
            log_strategy_event(self.symbol, side, "ENTRY_FAIL", qty=qty, details=str(resp)[:160])
            return
        self.ctx.tracker.record_entry(leg_sym, qty, side=1 if side == "CE" else -1)
        self.active = {"side": side, "symbol": leg_sym}
        self._last_qty = qty
        log_strategy_event(self.symbol, side, "ENTRY", qty=qty,
                           details=f"{leg_sym} units={self.units} lot={lot}")

    def _add_leg(self) -> None:
        leg_sym = self.active["symbol"]
        lot = self._lot_size(leg_sym)
        if not lot:
            return
        mg = self.ctx.tracker.get_martingale_count(leg_sym)
        add_qty = FIB[min(mg + 1, len(FIB) - 1)] * self.units * lot
        pos = self.ctx.api.get_position(leg_sym)
        net_qty = abs(int(pos.get("netQty", 0))) if pos else 0
        api_pl = float(pos.get("pl", 0.0)) if pos else 0.0
        eff = self.ctx.tracker.get_effective_pl(leg_sym, api_pl)
        resp = self.ctx.api.buy(leg_sym, add_qty)
        if not self._ok(resp):
            log_strategy_event(self.symbol, self.active["side"], "ADD_FAIL",
                               qty=add_qty, details=str(resp)[:160])
            return
        self.ctx.tracker.record_martingale(leg_sym, add_qty, net_qty,
                                           1 if self.active["side"] == "CE" else -1,
                                           eff, api_pl)
        self._last_qty = net_qty + add_qty
        log_strategy_event(self.symbol, self.active["side"], "ADD", qty=add_qty,
                           details=f"{leg_sym} level={mg + 1}")

    def _close_leg(self) -> None:
        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        net_qty = abs(int(pos.get("netQty", 0))) if pos else 0
        if net_qty == 0:
            self.pending_close = True
            self._confirm_close()
            return
        resp = self.ctx.api.sell(leg_sym, net_qty)
        if not self._ok(resp):
            log_strategy_event(self.symbol, self.active["side"], "CLOSE_FAIL",
                               qty=net_qty, details=str(resp)[:160])
            return
        self._last_qty = net_qty
        self.pending_close = True
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_SENT", qty=net_qty,
                           details=leg_sym)

    def _confirm_close(self) -> None:
        """Finalize a close: snapshot booked_profit once netQty == 0."""
        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        net_qty = abs(int(pos.get("netQty", 0))) if pos else 0
        if net_qty > 0:
            # not flat yet → retry the close
            self.ctx.api.sell(leg_sym, net_qty)
            return
        api_pl = float(pos.get("pl", 0.0)) if pos else 0.0
        ltp = float(pos.get("ltp", 0.0)) if pos else 0.0
        avg = float(pos.get("netAvg", pos.get("buyAvg", 0.0))) if pos else 0.0
        eff = self.ctx.tracker.get_effective_pl(leg_sym, api_pl)
        self.ctx.tracker.record_close(leg_sym, api_pl, self._last_qty, eff,
                                      ltp=ltp, avg_price=avg)
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_BOOKED",
                           qty=self._last_qty, pl=round(eff, 2),
                           details=f"{leg_sym} booked={round(eff, 2)}")
        self.active = None
        self.pending_close = False

    # ── data helpers ────────────────────────────────────────────────────────
    def _index_candles(self):
        try:
            return self.ctx.api.fetch_historical_data(
                self.underlying, SHA_TIMEFRAME, SHA_CANDLE_COUNT,
                market_type=self.asset_type,
            )
        except Exception as e:
            logger.warning("[%s] index candle fetch failed: %s", self.symbol, e)
            return None

    def _rsi_mtf(self, count: int) -> dict:
        """Fetch ONLY the RSI timeframes needed this tick (budget-aware):
        flat → entry-filter set; active → the ladder TF for `count` (+H6 at max)."""
        if self.active is None:
            needed = list(RSI_MTF_TIMEFRAMES)
        elif count <= len(RSI_DCA_LADDER_TIMEFRAMES):
            needed = [RSI_DCA_LADDER_TIMEFRAMES[count - 1]]
        else:
            needed = [RSI_FINAL_CLOSE_TIMEFRAME]
        out: dict = {}
        for tf in needed:
            res = TIMEFRAME_TO_FYERS_RES.get(tf, "5")
            try:
                if res == "360":  # H6 not native → TODO resample from 60-min
                    res = "240"
                df = self.ctx.api.fetch_historical_data(
                    self.underlying, res, RSI_CANDLE_COUNT, market_type=self.asset_type)
                rsi = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
                out[tf] = float(rsi.iloc[-1]) if len(rsi) else 50.0
            except Exception:
                out[tf] = 50.0
        return out

    def _lot_size(self, leg_sym: str):
        try:
            return FyersAPI.get_lot_size(leg_sym, self.ctx.option_df)
        except Exception as e:
            log_strategy_event(self.symbol, "-", "LOT_ERROR", details=f"{leg_sym}: {e}")
            return None

    # ── misc ────────────────────────────────────────────────────────────────
    def _in_window(self) -> bool:
        hrs = MARKET_HOURS.get(self.asset_type, MARKET_HOURS["INDEX"])
        return hrs["open"] <= now_ist().time() <= hrs["close"]

    @staticmethod
    def _ok(resp) -> bool:
        return isinstance(resp, dict) and resp.get("s") == "ok"

    def _belongs(self, option_symbol_upper: str) -> bool:
        base = self.symbol.upper()
        return (base in option_symbol_upper
                and f"BANK{base}" not in option_symbol_upper
                and f"FIN{base}" not in option_symbol_upper
                and option_symbol_upper.startswith(("NSE:", "MCX:")))

    def _recover_active(self) -> None:
        """Adopt a carried (overnight) position on startup so it's managed, not re-scanned."""
        try:
            resp = self.ctx.api.positions()
            if not isinstance(resp, dict) or resp.get("s") != "ok":
                return
            for row in resp.get("netPositions", []):
                sym = str(row.get("symbol", "")).upper()
                if int(row.get("netQty", 0)) != 0 and self._belongs(sym):
                    side = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                    if side:
                        self.active = {"side": side, "symbol": row.get("symbol")}
                        self._last_qty = abs(int(row.get("netQty", 0)))
                        logger.info("[%s] adopted carried %s %s", self.symbol, side, row.get("symbol"))
                        return
        except Exception as e:
            logger.warning("[%s] recover_active failed: %s", self.symbol, e)
