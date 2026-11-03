"""
symbol_worker.py — one trading thread per symbol (Loop-2). ARCHITECTURE §5.

RSI+MACD slope strategy on Fyers NETTING (buy-only), per TRADING_LOGIC.md:
  • Per-section weighted slope scores (open / book / force-close) built from RSI +
    MACD-histogram latest-pivot slopes across M5/M15/H1/H4/D1, computed on the INDEX.
  • BUY→CE leg, SELL→PE leg; BUY_MORE/SELL_MORE add+average; CLOSE sells.
  • Per-cycle P&L via SELF-TRACKED cost basis (carry-safe):
        cycle_pl = ltp*net_qty - cost_basis   (PositionTracker)
  • Balance-tiered units; adds = fib×units×lot. Profit exit is branch-specific:
    NETTING books on a BOOK-score reversal above the ₹100×units+charges floor.

Live-hardening in this version:
  • accurate cost basis from fill prices (tradebook, fallback ltp),
  • per-bar RSI/slope frame caching (TTL = bar duration) to respect the DATA budget,
  • post-close re-entry cooldown (REENTRY_COOLDOWN_SECONDS),
  • carried-position adoption seeds cost basis from the broker avg.

⚠️ Places REAL orders when run live. Fill-price sourcing is the main thing to
confirm during the dry-run (LIVE_DRYRUN.md).
"""

from __future__ import annotations

import logging
import threading
from time import monotonic

from constants import (
    MARKET_HOURS, WORKER_IDLE_SLEEP, now_ist,
    RSI_LENGTH, RSI_MA_TYPE, RSI_CANDLE_COUNT,
    RSI_DCA_LADDER_TIMEFRAMES, TIMEFRAME_TO_FYERS_RES,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SLOPE_TIMEFRAMES, PEAK_LOOKBACK, PIVOT_WIDTH, RSI_PEAK_OB, RSI_PEAK_OS,
    REENTRY_COOLDOWN_SECONDS,
    STEP_UP_BALANCE, UNIT_MAX_LIMIT, HEDGE_PER_UNIT,
    estimate_trade_charges,
)
from indicator import Indicator
from strategy import Strategy, Signal
from fyers_api import FyersAPI
import scanner
from state_writer import log_strategy_event, write_signal_state

logger = logging.getLogger("worker")

FIB = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]       # contracts-per-unit ladder

# Frame cache TTL per timeframe = how long until a new bar forms (seconds)
_RES_SECONDS = {
    "TIMEFRAME_M1": 60, "TIMEFRAME_M5": 300, "TIMEFRAME_M15": 900,
    "TIMEFRAME_M30": 1800, "TIMEFRAME_H1": 3600, "TIMEFRAME_H4": 14400,
    "TIMEFRAME_H6": 21600, "TIMEFRAME_D1": 86400,
}


class SymbolWorker(threading.Thread):
    def __init__(self, cfg: dict, ctx) -> None:
        super().__init__(name=f"worker-{cfg['symbol']}", daemon=True)
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.asset_type = cfg.get("asset_type", "INDEX")
        self.underlying = cfg["underlying"]
        self.step_up = cfg.get("step_up_balance", STEP_UP_BALANCE)
        self.max_limit = cfg.get("unit_max_limit", UNIT_MAX_LIMIT)
        self.root = cfg.get("root", self.symbol)                 # commodity root (GOLD/SILVER)
        self.hedge_per_unit = cfg.get("hedge_per_unit", HEDGE_PER_UNIT)
        self._active_underlying = self.underlying                # resolved per tick for commodity
        self._session = None                                     # (open, close) from MCX CSV
        self.ctx = ctx
        self.indicator = Indicator()
        self.strategy = Strategy()
        self.units = 1
        self.active: dict | None = None     # {"side": "CE"|"PE", "symbol": leg_sym}
        self.pending_close = False
        self._frame_cache: dict = {}        # tf -> (monotonic_fetch_time, ohlc_df) — per bar
        self._last_close_time = 0.0         # monotonic of last close → post-close re-entry cooldown
        self._pair_ttl = 300                # display-scan cache TTL (s)
        self._resolved_on = None            # IST date the commodity future was last resolved
        self._last_pair = None              # pair of the open basket (for display)
        self._pair_cache = None             # (fetch_monotonic, pair) — cached display scan when flat

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
        self._resolve_underlying()
        if not self.ctx.is_trading_day(now_ist().date()):
            return
        if not self._in_window():
            return

        if self.active and self.pending_close:   # close-first priority (pt 6)
            self._confirm_close()
            if self.active:
                return

        buy_pos, sell_pos, close_threshold, count = self._leg_state_and_threshold()
        rsi_mtf, slopes = self._slopes_and_rsi()
        source_df = self._tf_frame("TIMEFRAME_M5")   # price feed / liveness (strategy metadata)
        if source_df is None or len(source_df) == 0:
            logger.info("[%s] insufficient index candles", self.symbol)
            return

        entry_allowed = (monotonic() - self._last_close_time) >= REENTRY_COOLDOWN_SECONDS
        buy_signal, sell_signal, ad = self.strategy.calculate_signal(
            source_df, buy_pos, sell_pos, self.units,
            close_threshold=close_threshold,
            rsi_value=rsi_mtf.get("TIMEFRAME_M1", 50.0), rsi_mtf=rsi_mtf,
            slopes=slopes, entry_allowed=entry_allowed,
        )
        self._execute(buy_signal, sell_signal)
        try:
            write_signal_state(self.symbol,
                               self._signal_payload(ad, buy_pos, sell_pos, buy_signal, sell_signal))
        except Exception as e:
            logger.debug("[%s] signal_state write failed: %s", self.symbol, e)

    # ── leg state → strategy inputs (cost-basis P&L) ────────────────────────
    def _leg_state_and_threshold(self):
        if self.active is None:
            bal = self.ctx.api.get_balance()
            self.units = max(1, min(int(bal // self.step_up) + 1, self.max_limit))
            return None, None, float(self.hedge_per_unit * self.units), 0

        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        ltp = float(pos.get("ltp", 0.0)) if pos else 0.0
        cycle = self.ctx.tracker.cycle_pl(leg_sym, ltp)
        net = self.ctx.tracker.get_net_qty(leg_sym)
        mg = self.ctx.tracker.get_martingale_count(leg_sym)
        count = mg + 1
        charges = estimate_trade_charges(net or 1, ltp or 1, num_orders=2 + mg)
        threshold = float(self.hedge_per_unit * self.units + charges)
        leg = {"count": count, "total_profit": cycle, "total_volume": net,
               "first_profit": cycle, "last_profit": cycle,
               "first_volume": net, "last_volume": net}
        if self.active["side"] == "CE":
            return leg, None, threshold, count
        return None, leg, threshold, count

    # ── execution ───────────────────────────────────────────────────────────
    def _execute(self, buy_signal, sell_signal) -> None:
        if self.active is None:
            if buy_signal == Signal.BUY:
                self._open_leg("CE")
            elif sell_signal == Signal.SELL:
                self._open_leg("PE")
            return
        if self.active["side"] == "CE":
            if buy_signal == Signal.BUY_MORE:
                self._add_leg()
            elif buy_signal == Signal.CLOSE_BUY:
                self._close_leg()
        else:
            if sell_signal == Signal.SELL_MORE:
                self._add_leg()
            elif sell_signal == Signal.CLOSE_SELL:
                self._close_leg()

    def _open_leg(self, side: str) -> None:
        lot_df = self.ctx.commodity_df if self.asset_type == "COMMODITY" else self.ctx.option_df
        pair = scanner.scan_best_pair(self.cfg, self.ctx.api, self.ctx.meter, lot_df=lot_df)
        if not pair:
            log_strategy_event(self.symbol, side, "SCAN_EMPTY", details="no pair for entry")
            return
        self._last_pair = pair
        leg_sym = pair["CE_Symbol"] if side == "CE" else pair["PE_Symbol"]
        premium = float(pair.get(f"{side}_Premium", 0) or 0)
        lot = self._lot_size(leg_sym)
        if not lot:
            return
        qty = self.units * lot
        if not self._ok(self.ctx.api.buy(leg_sym, qty)):
            log_strategy_event(self.symbol, side, "ENTRY_FAIL", qty=qty)
            return
        fill = self._fill_price(leg_sym, premium)
        self.ctx.tracker.record_entry(leg_sym, qty, fill, side=1 if side == "CE" else -1)
        self.active = {"side": side, "symbol": leg_sym}
        log_strategy_event(self.symbol, side, "ENTRY", qty=qty,
                           details=f"{leg_sym} units={self.units} fill~{fill}")

    def _add_leg(self) -> None:
        leg_sym = self.active["symbol"]
        lot = self._lot_size(leg_sym)
        if not lot:
            return
        mg = self.ctx.tracker.get_martingale_count(leg_sym)
        add_qty = FIB[min(mg + 1, len(FIB) - 1)] * self.units * lot
        pos = self.ctx.api.get_position(leg_sym)
        ltp = float(pos.get("ltp", 0.0)) if pos else 0.0
        if not self._ok(self.ctx.api.buy(leg_sym, add_qty)):
            log_strategy_event(self.symbol, self.active["side"], "ADD_FAIL", qty=add_qty)
            return
        fill = self._fill_price(leg_sym, ltp)
        self.ctx.tracker.record_add(leg_sym, add_qty, fill)
        log_strategy_event(self.symbol, self.active["side"], "ADD", qty=add_qty,
                           details=f"{leg_sym} level={mg + 1} fill~{fill}")

    def _close_leg(self) -> None:
        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        net = abs(int(pos.get("netQty", 0))) if pos else 0
        if net == 0:
            self.pending_close = True
            self._confirm_close()
            return
        if not self._ok(self.ctx.api.sell(leg_sym, net)):
            log_strategy_event(self.symbol, self.active["side"], "CLOSE_FAIL", qty=net)
            return
        self.pending_close = True
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_SENT", qty=net, details=leg_sym)

    def _confirm_close(self) -> None:
        leg_sym = self.active["symbol"]
        pos = self.ctx.api.get_position(leg_sym)
        net = abs(int(pos.get("netQty", 0))) if pos else 0
        if net > 0:
            self.ctx.api.sell(leg_sym, net)      # retry until flat
            return
        ltp = float(pos.get("ltp", 0.0)) if pos else 0.0
        exit_px = self._fill_price(leg_sym, ltp)
        mg = self.ctx.tracker.get_martingale_count(leg_sym)
        qty = self.ctx.tracker.get_net_qty(leg_sym)
        charges = estimate_trade_charges(qty or 1, exit_px or 1, num_orders=2 + mg)
        net_pl = self.ctx.tracker.record_close(leg_sym, exit_px, charges)
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_BOOKED",
                           pl=round(net_pl, 2),
                           details=f"{leg_sym} net=Rs{round(net_pl, 2)} tax=Rs{round(charges, 2)}")
        self.active = None
        self.pending_close = False
        self._last_pair = None
        self._last_close_time = monotonic()   # start the post-close re-entry cooldown

    def _fill_price(self, leg_sym: str, fallback_ltp: float) -> float:
        """Actual fill price for cost basis: tradebook last trade, else ltp fallback."""
        px = self.ctx.api.last_fill_price(leg_sym)
        if px:
            return px
        return float(fallback_ltp or 0.0)

    # ── data helpers ────────────────────────────────────────────────────────
    def _tf_frame(self, tf: str):
        """Cached per-bar OHLC frame for one timeframe (reused for RSI + MACD slopes).
        Refetching 500 candles every 60s tick is wasted work — this refreshes at most
        once per bar (TTL = bar duration), so the DATA budget is respected while every
        slope TF stays current. H6 (360) isn't a native Fyers resolution → resample from
        60-min; D1 maps to Fyers 'D'."""
        now = monotonic()
        cached = self._frame_cache.get(tf)
        ttl = _RES_SECONDS.get(tf, 300)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        res = TIMEFRAME_TO_FYERS_RES.get(tf, "5")
        resample = None
        if res == "360":                             # H6 → resample from 60-min
            res, resample = "60", "6h"
        try:
            df = self.ctx.api.fetch_historical_data(
                self._active_underlying, res, RSI_CANDLE_COUNT, market_type=self.asset_type)
            if resample is not None:
                df = self._resample(df, resample)
        except Exception as e:
            logger.warning("[%s] %s frame fetch failed: %s", self.symbol, tf, e)
            df = cached[1] if cached else None
        self._frame_cache[tf] = (now, df)
        return df

    def _slopes_and_rsi(self):
        """Per-TF RSI (ladder tiers) + RSI/MACD-histogram latest-pivot slopes (SLOPE_TIMEFRAMES).
        One fetch per timeframe (cached per bar), reused for both — mirrors Alcadeias app.py.
        The MACD pivot side is chosen by the signal line (signal>0→'high', <0→'low'). Returns
        (rsi_mtf: {TIMEFRAME_x: value}, slopes: {short_tf: {'rsi':…, 'macd':…}})."""
        needed = []
        for tf in (["TIMEFRAME_M1"] + list(RSI_DCA_LADDER_TIMEFRAMES) + list(SLOPE_TIMEFRAMES)):
            if tf not in needed:
                needed.append(tf)
        rsi_mtf, slopes = {}, {}
        for tf in needed:
            df = self._tf_frame(tf)
            if df is None or len(df) == 0:
                rsi_mtf[tf] = 50.0
                continue
            rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
            rsi_mtf[tf] = float(rsi_s.iloc[-1]) if len(rsi_s) else 50.0
            if tf in SLOPE_TIMEFRAMES:
                macd_df = self.indicator.calculate_macd(df["Close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                rsi_slope = self.indicator.latest_pivot_slope(
                    rsi_s, lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH,
                    ob_level=RSI_PEAK_OB, os_level=RSI_PEAK_OS)
                sig = float(macd_df["signal"].iloc[-1]) if len(macd_df) else 0.0
                macd_side = "high" if sig > 0 else ("low" if sig < 0 else None)
                macd_slope = self.indicator.latest_pivot_slope(
                    macd_df["hist"], lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, side=macd_side)
                slopes[tf.replace("TIMEFRAME_", "")] = {"rsi": rsi_slope, "macd": macd_slope}
        return rsi_mtf, slopes

    def _get_display_pair(self):
        """Best CE/PE pair for the dashboard: the open basket's pair if in a trade,
        else a cached speculative scan (refreshed per _pair_ttl to bound API cost)."""
        if self.active and self._last_pair:
            return self._last_pair
        now = monotonic()
        if self._pair_cache and (now - self._pair_cache[0]) < self._pair_ttl:
            return self._pair_cache[1]
        lot_df = self.ctx.commodity_df if self.asset_type == "COMMODITY" else self.ctx.option_df
        pair = scanner.scan_best_pair(self.cfg, self.ctx.api, self.ctx.meter, lot_df=lot_df)
        self._pair_cache = (now, pair)
        return pair

    def _signal_payload(self, ad, buy_pos, sell_pos, buy_signal, sell_signal) -> dict:
        """Build the per-symbol dashboard payload (signal + slope scores + RSI + pair + position)."""
        if self.active:
            side = self.active["side"]
            if buy_signal == Signal.CLOSE_BUY or sell_signal == Signal.CLOSE_SELL:
                sig = "CLOSE"
            elif buy_signal == Signal.BUY_MORE or sell_signal == Signal.SELL_MORE:
                sig = "ADD"
            else:
                sig = f"HOLD {side}"
        elif buy_signal == Signal.BUY:
            sig = "BUY"
        elif sell_signal == Signal.SELL:
            sig = "SELL"
        else:
            sig = "DO_NOTHING"
        legdict = buy_pos or sell_pos
        pos = None
        if self.active and legdict:
            pos = {"side": self.active["side"], "symbol": self.active["symbol"],
                   "netQty": legdict.get("total_volume"),
                   "cycle_pl": legdict.get("total_profit"),
                   "mg": legdict.get("count", 1) - 1}
        slopes_raw = ad.get("slopes", {}) or {}
        slope_signs = {
            tf: {"rsi": (v.get("rsi") or {}).get("sign", 0),
                 "macd": (v.get("macd") or {}).get("sign", 0)}
            for tf, v in slopes_raw.items()
        }
        return {
            "asset_type": self.asset_type,
            "underlying": self._active_underlying,
            "signal": sig,
            "units": self.units,
            "scores": {"open": ad.get("open_score", 0),
                       "book": ad.get("book_score"),
                       "fc": ad.get("fc_score", 0)},
            "entry_direction": ad.get("entry_direction", "WAIT"),
            "slope_direction": ad.get("slope_direction", "WAIT"),
            "slopes": slope_signs,
            "entry_allowed": ad.get("entry_allowed", True),
            "entry_chop_block": ad.get("entry_chop_block", False),
            "exit_reason": ad.get("exit_reason"),
            "variant": ad.get("variant", "netting"),
            "rsi": ad.get("rsi_mtf", {}),
            "pair": self._get_display_pair(),
            "position": pos,
        }

    @staticmethod
    def _resample(df, rule: str):
        d = (df.set_index("Timestamp")
               .resample(rule)
               .agg({"Open": "first", "High": "max", "Low": "min",
                     "Close": "last", "Volume": "sum"})
               .dropna()
               .reset_index())
        return d

    def _lot_size(self, leg_sym: str):
        df = self.ctx.commodity_df if self.asset_type == "COMMODITY" else self.ctx.option_df
        try:
            return FyersAPI.get_lot_size(leg_sym, df)
        except Exception as e:
            log_strategy_event(self.symbol, "-", "LOT_ERROR", details=f"{leg_sym}: {e}")
            return None

    def _resolve_underlying(self) -> None:
        """For COMMODITY, resolve the active FUTURE symbol + session from the MCX CSV
        (DST-correct). For INDEX, the configured underlying is used as-is."""
        if self.asset_type != "COMMODITY":
            self.cfg["_resolved_underlying"] = self.underlying
            return
        today = now_ist().date()
        if self._resolved_on == today and self._session is not None:
            return                               # resolved already today — skip the df scan
        df = getattr(self.ctx, "commodity_df", None)
        if df is None:
            return
        res = FyersAPI.resolve_commodity(self.root, df)
        if res:
            self._active_underlying, self._session = res
            self.cfg["_resolved_underlying"] = self._active_underlying
            self._resolved_on = today

    # ── misc ────────────────────────────────────────────────────────────────
    def _in_window(self) -> bool:
        if self.asset_type == "COMMODITY" and self._session:
            o, c = self._session
            return o <= now_ist().time() <= c
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
        """Adopt a carried position on startup; seed cost basis from broker avg if untracked."""
        try:
            resp = self.ctx.api.positions()
            if not isinstance(resp, dict) or resp.get("s") != "ok":
                return
            for row in resp.get("netPositions", []):
                sym = str(row.get("symbol", "")).upper()
                net = int(row.get("netQty", 0))
                if net != 0 and self._belongs(sym):
                    side = "CE" if sym.endswith("CE") else ("PE" if sym.endswith("PE") else None)
                    if not side:
                        continue
                    self.active = {"side": side, "symbol": row.get("symbol")}
                    avg = float(row.get("netAvg", row.get("buyAvg", 0.0)) or 0.0)
                    self.ctx.tracker.seed_from_broker(row.get("symbol"), abs(net), avg,
                                                      1 if side == "CE" else -1)
                    logger.info("[%s] adopted carried %s %s (avg~%s)",
                                self.symbol, side, row.get("symbol"), avg)
                    return
        except Exception as e:
            logger.warning("[%s] recover_active failed: %s", self.symbol, e)
