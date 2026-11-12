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
    MARKET_HOURS, WORKER_IDLE_SLEEP, now_ist, today_ist,
    RSI_LENGTH, RSI_MA_TYPE, RSI_CANDLE_COUNT,
    RSI_DCA_LADDER_TIMEFRAMES, TIMEFRAME_TO_FYERS_RES,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SLOPE_TIMEFRAMES, LEG_SLOPE_TIMEFRAMES, OPEN_LEG_TIMEFRAMES,
    PEAK_LOOKBACK, PIVOT_WIDTH, RSI_PEAK_OB, RSI_PEAK_OS, RSI_GRAPH_BARS,
    REENTRY_COOLDOWN_SECONDS, MIN_HOLD_SECONDS, PAIR_RELOCK_SECONDS,
    STEP_UP_BALANCE, UNIT_MAX_LIMIT, HEDGE_PER_UNIT,
    estimate_trade_charges,
)
from indicator import Indicator
from strategy import Strategy, Signal
from fyers_api import FyersAPI
import scanner
import auto_tune
from state_writer import log_strategy_event, write_signal_state, write_ohlc_state, backfill_ohlc_state

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
        # ₹ profit target per unit. The symbols.json value is the BASE/floor; the daily auto-tuner
        # (auto_tune.py, hedging) hill-climbs it up to CAP and persists — load the tuned value here.
        self._base_hedge_per_unit = cfg.get("hedge_per_unit", HEDGE_PER_UNIT)
        self.hedge_per_unit = auto_tune.current_amount(self.symbol, self._base_hedge_per_unit)
        self._tuned_date = None                                  # last date the auto-tuner ran
        self._active_underlying = self.underlying                # resolved per tick for commodity
        self._session = None                                     # (open, close) from MCX CSV
        self.ctx = ctx
        self.indicator = Indicator()
        self.strategy = Strategy()
        self.units = 1
        self.active: dict | None = None     # {"side": "CE"|"PE", "symbol": leg_sym}
        self.pending_close = False
        self.force_close = False            # set by the dashboard "force close" command (via supervisor)
        self._frame_cache: dict = {}        # (symbol, tf) -> (monotonic_fetch_time, ohlc_df) — per bar
        self._last_close_time = 0.0         # monotonic of last close → post-close re-entry cooldown
        self._entry_mono = None             # monotonic at first entry → post-entry min-hold (anti-vibration)
        self._last_exit_reason = None       # 'target'|'book'|'force_close' → written into the close log
        self._pair_ttl = 300                # display-scan cache TTL (s)
        self._resolved_on = None            # IST date the commodity future was last resolved
        self._last_pair = None              # pair of the open basket (for display)
        self._locked_pair = None            # CE/PE pair locked for the trading day (re-scanned per cycle)
        self._locked_at = 0.0               # monotonic when the current pair was locked
        self._locked_day = None             # IST date the pair was locked (re-scan on a new day)

    # ── thread main ─────────────────────────────────────────────────────────
    def run(self) -> None:
        logger.info("[%s] worker started (%s)", self.symbol, self.asset_type)
        self._recover_active()
        self._backfill_ohlc()        # seed the last week of past-day price charts (once, best-effort)
        while not self.ctx.stop_event.is_set():
            _loop_start = monotonic()
            try:
                self._tick()
            except Exception as e:
                logger.exception("[%s] tick error: %s", self.symbol, e)
            work = monotonic() - _loop_start             # this tick's fetch+trade work (excludes the idle wait)
            self.ctx.stop_event.wait(WORKER_IDLE_SLEEP)
            cycle = monotonic() - _loop_start            # full loop cycle = work + the WORKER_IDLE_SLEEP idle (ONE loop)
            print(f"[{self.symbol}] fetch+trade={work:.2f}s / cycle={cycle:.2f}s", flush=True)
        logger.info("[%s] worker stopped", self.symbol)

    # ── one iteration ───────────────────────────────────────────────────────
    def _tick(self) -> None:
        if self.ctx.brake_event.is_set():
            return
        self._resolve_underlying()
        if not self._should_trade_now():
            self._persist_ohlc_offhours()     # keep this symbol's chart populated after its market closes
            return

        if self.force_close:                  # dashboard "force close" — flatten this symbol's basket now
            self.force_close = False
            if self.active and not self.pending_close:
                self._close_leg()             # real sell (live) / simulated (demo); books P&L on confirm
            return                            # next tick's close-first path confirms until flat

        if self.active and self.pending_close:   # close-first priority (pt 6)
            self._confirm_close()
            if self.active:
                return

        self._maybe_auto_tune()                  # ~5 min before close: hill-climb the ₹ target (daily)
        buy_pos, sell_pos, close_threshold, count = self._leg_state_and_threshold()
        _idx_rsi, slopes, rsi_graphs, macd_graphs = self._slopes_and_rsi()          # INDEX: OPEN entry + add-gate
        leg_slopes, leg_rsi_mtf, leg_rsi_graphs, leg_macd_graphs, leg_slopes_raw = self._leg_slopes()  # LEG: ladder RSI + fc/book
        rsi_mtf = leg_rsi_mtf if self.active else _idx_rsi   # DCA ladder reads the ACTIVE leg's own RSI
        source_df = self._frame(self._active_underlying, "TIMEFRAME_M5")  # price feed / liveness
        if source_df is None or len(source_df) == 0:
            logger.info("[%s] insufficient index candles", self.symbol)
            return

        # Fresh-entry gate (while FLAT): the CANDIDATE leg's own slopes, chosen by the INDEX direction
        # — H4 decisive, else H1 (grey backup). Must match the strategy's idx_dir so the leg we FETCH
        # is the one the gate evaluates (dir > 0 -> CE candidate; dir < 0 -> PE candidate).
        cand_leg_slopes = {}
        if not self.active:
            _dir = self.strategy._decisive(slopes, "H4") or self.strategy._decisive(slopes, "H1")
            _cand = None
            if _dir > 0:
                _cand = (self._get_display_pair() or {}).get("CE_Symbol")
            elif _dir < 0:
                _cand = (self._get_display_pair() or {}).get("PE_Symbol")
            if _cand:
                cand_leg_slopes = self._candidate_leg_slopes(_cand)

        # Both pair legs' slope signs for the dashboard's index / CE / PE readout — reuse the ACTIVE
        # leg's already-fetched slopes; fetch only the OTHER leg (one extra set, cached per bar).
        _pair = self._get_display_pair() or {}
        _act = self.active["side"] if self.active else None
        ce_slopes = leg_slopes if _act == "CE" else self._display_leg_slopes(_pair.get("CE_Symbol"))
        pe_slopes = leg_slopes if _act == "PE" else self._display_leg_slopes(_pair.get("PE_Symbol"))

        entry_allowed = (monotonic() - self._last_close_time) >= REENTRY_COOLDOWN_SECONDS
        # Post-entry min-hold: suppress book + force-close until the basket is this old, so it
        # can't open-then-close on a stale leg pivot (anti-vibration). No position → always True.
        if self._entry_mono is None:
            exit_allowed, min_hold_left = True, 0
        else:
            held = monotonic() - self._entry_mono
            exit_allowed = held >= MIN_HOLD_SECONDS
            min_hold_left = max(0, int(MIN_HOLD_SECONDS - held))
        buy_signal, sell_signal, ad = self.strategy.calculate_signal(
            source_df, buy_pos, sell_pos, self.units,
            close_threshold=close_threshold,
            rsi_value=rsi_mtf.get("TIMEFRAME_M1", 50.0), rsi_mtf=rsi_mtf,
            slopes=slopes, leg_slopes=leg_slopes, cand_leg_slopes=cand_leg_slopes,
            leg_slopes_raw=leg_slopes_raw,
            entry_allowed=entry_allowed, exit_allowed=exit_allowed,
        )
        ad["rsi_graphs"] = rsi_graphs
        ad["macd_graphs"] = macd_graphs
        ad["ce_slopes"] = ce_slopes
        ad["pe_slopes"] = pe_slopes
        ad["leg_rsi_graphs"] = leg_rsi_graphs
        ad["leg_macd_graphs"] = leg_macd_graphs
        ad["close_threshold"] = close_threshold
        ad["exit_allowed"] = exit_allowed
        ad["min_hold_left"] = min_hold_left
        self._last_exit_reason = ad.get("exit_reason")   # carried into the close log (why we closed)
        self._execute(buy_signal, sell_signal)
        try:
            write_signal_state(self.symbol,
                               self._signal_payload(ad, buy_pos, sell_pos, buy_signal, sell_signal))
        except Exception as e:
            logger.debug("[%s] signal_state write failed: %s", self.symbol, e)
        try:
            self._persist_ohlc(source_df)
        except Exception as e:
            logger.debug("[%s] ohlc_state write failed: %s", self.symbol, e)

    # ── OHLC persistence for the dashboard price chart ──────────────────────
    def _backfill_ohlc(self) -> None:
        """Seed PAST-day OHLC snapshots (last week) for this symbol from the underlying's history, so
        the dashboard date picker shows past-day price charts right after an upgrade — not only for
        days the engine has since run. One API call; best-effort; never overwrites a live-written day."""
        try:
            self._resolve_underlying()
            df = self.ctx.api.fetch_historical_data(
                self._active_underlying, "5", 1500,   # M5 index candles, ~enough for the retention week
                market_type=self.asset_type, holidays=self.ctx.holidays,
                special_sessions=self.ctx.special_sessions)
            if df is None or len(df) == 0:
                return
            ts = df["Timestamp"]
            try:                           # Fyers candles are naive-UTC; align to IST (same as _persist_ohlc)
                ts = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts
                ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
            except Exception:
                pass
            tstr = [str(x)[:19] for x in ts.tolist()]
            o = [round(float(v), 2) for v in df["Open"].tolist()]
            h = [round(float(v), 2) for v in df["High"].tolist()]
            l = [round(float(v), 2) for v in df["Low"].tolist()]
            c = [round(float(v), 2) for v in df["Close"].tolist()]
            today = now_ist().strftime("%Y-%m-%d")
            rows_by_date: dict = {}
            for i, s in enumerate(tstr):
                d = s[:10]
                if d >= today:             # today is written live by _persist_ohlc
                    continue
                b = rows_by_date.setdefault(d, {"t": [], "o": [], "h": [], "l": [], "c": []})
                b["t"].append(tstr[i]); b["o"].append(o[i]); b["h"].append(h[i])
                b["l"].append(l[i]); b["c"].append(c[i])
            if rows_by_date:
                backfill_ohlc_state(self.symbol, self._active_underlying, "M5", rows_by_date)
                logger.info("[%s] backfilled OHLC history for %d past day(s)", self.symbol, len(rows_by_date))
        except Exception as e:
            logger.debug("[%s] ohlc backfill failed: %s", self.symbol, e)

    def _persist_ohlc(self, index_df) -> None:
        """Persist the UNDERLYING INDEX's today M5 candles (IST-aligned) for the dashboard. The chart
        overlays ALL of today's trades for this symbol (every leg) on it — so the live position AND
        every closed trade are visible on one common time axis."""
        df = index_df
        if df is None or len(df) == 0:
            return
        tail = df.tail(220)
        ts = tail["Timestamp"]
        try:                               # Fyers candles are naive-UTC (epoch); fills are IST — align them
            ts = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts
            ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        except Exception:
            pass
        tstr = [str(x)[:19] for x in ts.tolist()]
        o = [round(float(v), 2) for v in tail["Open"].tolist()]
        h = [round(float(v), 2) for v in tail["High"].tolist()]
        l = [round(float(v), 2) for v in tail["Low"].tolist()]
        c = [round(float(v), 2) for v in tail["Close"].tolist()]
        today = now_ist().strftime("%Y-%m-%d")               # current day only
        idx = [i for i, s in enumerate(tstr) if s[:10] == today]
        if idx:
            tstr = [tstr[i] for i in idx]; o = [o[i] for i in idx]
            h = [h[i] for i in idx]; l = [l[i] for i in idx]; c = [c[i] for i in idx]
        write_ohlc_state(self.symbol, self._active_underlying, "", "M5", tstr, o, h, l, c, bool(self.active))

    def _persist_ohlc_offhours(self) -> None:
        """Market closed for this symbol: persist today's index candles ONCE (they don't change after
        the close) so its dashboard chart stays visible while other symbols still trade."""
        if getattr(self, "_offh_day", None) == now_ist().date():
            return
        try:
            df = self._frame(self._active_underlying, "TIMEFRAME_M5")
            if df is not None and len(df):
                self._persist_ohlc(df)
                self._offh_day = now_ist().date()
        except Exception as e:
            logger.debug("[%s] off-hours ohlc persist failed: %s", self.symbol, e)

    # ── leg state → strategy inputs (cost-basis P&L) ────────────────────────
    def _maybe_auto_tune(self):
        """Once per trading day, ~5 min before THIS asset's session close, hill-climb the ₹ profit
        target from the day's win-ratio + net ₹ and auto-apply it (auto_tune.maybe_tune). Idempotent
        per day via ``_tuned_date``; only fires inside the pre-close window while the market is live."""
        today = today_ist().isoformat()
        if self._tuned_date == today:
            return
        close_t = MARKET_HOURS.get(self.asset_type, MARKET_HOURS["INDEX"])["close"]
        now = now_ist()
        close_dt = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        if now < close_dt - timedelta(minutes=5):
            return
        self.hedge_per_unit = auto_tune.maybe_tune(self.symbol, self._base_hedge_per_unit, today)
        self._tuned_date = today
        log_strategy_event(self.symbol, (self.active or {}).get("side", ""), "AUTO_TUNE",
                           details=f"target->Rs{self.hedge_per_unit:.0f} (base Rs{self._base_hedge_per_unit:.0f})")

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
        charges = estimate_trade_charges(net or 1, ltp or 1, num_orders=2 + mg,
                                         is_commodity=(self.asset_type == "COMMODITY"))
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
        self._entry_mono = monotonic()                # start the post-entry min-hold
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
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_SENT", qty=net,
                           details=f"{leg_sym} reason={self._last_exit_reason or '?'}")

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
        charges = estimate_trade_charges(qty or 1, exit_px or 1, num_orders=2 + mg,
                                         is_commodity=(self.asset_type == "COMMODITY"))
        net_pl = self.ctx.tracker.record_close(leg_sym, exit_px, charges)
        log_strategy_event(self.symbol, self.active["side"], "CLOSE_BOOKED",
                           pl=round(net_pl, 2),
                           details=f"{leg_sym} reason={self._last_exit_reason or '?'} "
                                   f"net=Rs{round(net_pl, 2)} tax=Rs{round(charges, 2)}")
        self.active = None
        self.pending_close = False
        self._last_pair = None
        self._entry_mono = None               # clear the basket age
        self._last_close_time = monotonic()   # start the post-close re-entry cooldown

    def _fill_price(self, leg_sym: str, fallback_ltp: float) -> float:
        """Actual fill price for cost basis: tradebook last trade, else ltp fallback."""
        px = self.ctx.api.last_fill_price(leg_sym)
        if px:
            return px
        return float(fallback_ltp or 0.0)

    # ── data helpers ────────────────────────────────────────────────────────
    def _frame(self, symbol: str, tf: str):
        """Cached per-bar OHLC frame for (symbol, timeframe). Used for the INDEX slopes and —
        once a basket is open — the active CE/PE LEG's own slopes. Refreshes at most once per
        bar (TTL = bar duration) so the DATA budget is respected. H6 (360) isn't a native Fyers
        resolution → resample from 60-min; D1 maps to Fyers 'D'."""
        now = monotonic()
        key = (symbol, tf)
        cached = self._frame_cache.get(key)
        ttl = _RES_SECONDS.get(tf, 300)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        res = TIMEFRAME_TO_FYERS_RES.get(tf, "5")
        resample = None
        if res == "360":                             # H6 → resample from 60-min
            res, resample = "60", "6h"
        try:
            df = self.ctx.api.fetch_historical_data(
                symbol, res, RSI_CANDLE_COUNT, market_type=self.asset_type,
                holidays=self.ctx.holidays, special_sessions=self.ctx.special_sessions)
            if resample is not None:
                df = self._resample(df, resample)
        except Exception as e:
            logger.warning("[%s] %s %s frame fetch failed: %s", self.symbol, symbol, tf, e)
            df = cached[1] if cached else None
        self._frame_cache[key] = (now, df)
        return df

    def _slope_and_graph(self, df):
        """RSI + MACD-histogram latest-pivot slopes + the dashboard mini-chart payloads for one
        OHLC frame (index or leg). The MACD pivot side is chosen by the signal line (signal>0→
        'high', <0→'low'). Returns (rsi_slope, macd_slope, rsi_graph, macd_graph)."""
        rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
        macd_df = self.indicator.calculate_macd(df["Close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        rsi_slope = self.indicator.latest_pivot_slope(
            rsi_s, lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, ob_level=RSI_PEAK_OB, os_level=RSI_PEAK_OS)
        sig = float(macd_df["signal"].iloc[-1]) if len(macd_df) else 0.0
        macd_side = "high" if sig > 0 else ("low" if sig < 0 else None)
        macd_slope = self.indicator.latest_pivot_slope(
            macd_df["hist"], lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, side=macd_side)
        hist = macd_df["hist"]
        rsi_graph = {
            "series": [round(float(v), 2) for v in rsi_s.tail(RSI_GRAPH_BARS).tolist()],
            "current": round(float(rsi_s.iloc[-1]), 2),
            "pivot_bars_ago": rsi_slope.get("bars_ago"),
            "pivot_value": (round(rsi_slope["value"], 2) if rsi_slope.get("value") is not None else None),
            "sign": rsi_slope.get("sign", 0),
            "bands": [RSI_PEAK_OS, RSI_PEAK_OB], "clamp": [0, 100], "name": "RSI",
        }
        macd_graph = {
            "series": [round(float(v), 5) for v in hist.tail(RSI_GRAPH_BARS).tolist()],
            "current": round(float(hist.iloc[-1]), 5),
            "pivot_bars_ago": macd_slope.get("bars_ago"),
            "pivot_value": (round(macd_slope["value"], 5) if macd_slope.get("value") is not None else None),
            "sign": macd_slope.get("sign", 0),
            "bands": [0], "clamp": None, "name": "MACD",
        }
        return rsi_slope, macd_slope, rsi_graph, macd_graph

    def _slope_signs(self, df):
        """RSI + MACD-histogram latest-pivot slope dicts ONLY (no dashboard graph payload) — for
        timeframes used purely as decision backups (index M1/M15/H1, leg M1, candidate H1/M1)."""
        rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
        macd_df = self.indicator.calculate_macd(df["Close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        rsi_slope = self.indicator.latest_pivot_slope(
            rsi_s, lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, ob_level=RSI_PEAK_OB, os_level=RSI_PEAK_OS)
        sig = float(macd_df["signal"].iloc[-1]) if len(macd_df) else 0.0
        macd_side = "high" if sig > 0 else ("low" if sig < 0 else None)
        macd_slope = self.indicator.latest_pivot_slope(
            macd_df["hist"], lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, side=macd_side)
        return rsi_slope, macd_slope

    def _slopes_and_rsi(self):
        """INDEX per-TF RSI (ladder tiers) + RSI/MACD latest-pivot slopes + dashboard series,
        computed on the MAIN INDEX (drives OPEN entry + the ladder-add gate + the entry veto).
        One fetch per timeframe, cached per bar. Returns (rsi_mtf, slopes, rsi_graphs, macd_graphs)."""
        needed = []
        for tf in (["TIMEFRAME_M1"] + list(RSI_DCA_LADDER_TIMEFRAMES) + list(SLOPE_TIMEFRAMES)):
            if tf not in needed:
                needed.append(tf)
        rsi_mtf, slopes, rsi_graphs, macd_graphs = {}, {}, {}, {}
        for tf in needed:
            df = self._frame(self._active_underlying, tf)
            if df is None or len(df) == 0:
                rsi_mtf[tf] = 50.0
                continue
            rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
            rsi_mtf[tf] = float(rsi_s.iloc[-1]) if len(rsi_s) else 50.0
            key = tf.replace("TIMEFRAME_", "")
            if tf in SLOPE_TIMEFRAMES:
                rsl, msl, rg, mg = self._slope_and_graph(df)
                slopes[key] = {"rsi": rsl, "macd": msl}
                rsi_graphs[key] = rg
                macd_graphs[key] = mg
            else:                                    # M1/M15/H1 — signs only, for the grey backups
                rsl, msl = self._slope_signs(df)
                slopes[key] = {"rsi": rsl, "macd": msl}
        return rsi_mtf, slopes, rsi_graphs, macd_graphs

    def _leg_slopes(self):
        """LEG data on the ACTIVE CE/PE option's OWN candles: per-TF RSI for the DCA ladder tiers
        (M1-H4) + RSI/MACD latest-pivot slopes for the force-close (M15·H1·H4) and book (M5), plus the
        leg mini-chart series. Empty when flat. Each (leg, tf) frame is cached per bar. Returns
        (leg_slopes, leg_rsi_mtf, leg_rsi_graphs, leg_macd_graphs, leg_slopes_raw)."""
        if not self.active:
            return {}, {}, {}, {}, {}
        leg_sym = self.active["symbol"]
        slopes, rsi_mtf, rsi_graphs, macd_graphs, raw = {}, {}, {}, {}, {}
        needed = []
        for tf in (list(RSI_DCA_LADDER_TIMEFRAMES) + list(LEG_SLOPE_TIMEFRAMES)):
            if tf not in needed:
                needed.append(tf)
        for tf in needed:
            df = self._frame(leg_sym, tf)
            if df is None or len(df) == 0:
                continue
            key = tf.replace("TIMEFRAME_", "")
            rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
            rsi_mtf[tf] = float(rsi_s.iloc[-1]) if len(rsi_s) else 50.0
            if tf in LEG_SLOPE_TIMEFRAMES:
                rsl, msl, rg, mg = self._slope_and_graph(df)
                slopes[key] = {"rsi": rsl, "macd": msl}
                rsi_graphs[key] = rg
                macd_graphs[key] = mg
                raw[key] = self._raw_slope(df)   # OB/OS-unbounded variant → force-close grey fallback
            else:                                # M1 — signs only, for the booking M5→M1 grey backup
                rsl, msl = self._slope_signs(df)
                slopes[key] = {"rsi": rsl, "macd": msl}
        return slopes, rsi_mtf, rsi_graphs, macd_graphs, raw

    def _candidate_leg_slopes(self, cand_sym):
        """Slope signs on a CANDIDATE option leg, computed while FLAT to gate a fresh entry — the
        OPEN_LEG_TIMEFRAMES set (H4·H1·M5·M1): H4+M5 are the gates, H1+M1 the grey backups."""
        out = {}
        for tf in OPEN_LEG_TIMEFRAMES:
            df = self._frame(cand_sym, tf)
            if df is None or len(df) == 0:
                continue
            rsl, msl = self._slope_signs(df)
            out[tf.replace("TIMEFRAME_", "")] = {"rsi": rsl, "macd": msl}
        return out

    def _display_leg_slopes(self, sym):
        """Slope signs (RSI + MACD) on a leg option over the leg timeframes (M5·M15·H1·H4), for the
        dashboard's index / CE / PE readout. Cached per bar. {} if no symbol / no candles."""
        out = {}
        if not sym:
            return out
        for tf in LEG_SLOPE_TIMEFRAMES:
            df = self._frame(sym, tf)
            if df is None or len(df) == 0:
                continue
            rsl, msl, _, _ = self._slope_and_graph(df)
            out[tf.replace("TIMEFRAME_", "")] = {"rsi": rsl, "macd": msl}
        return out

    def _raw_slope(self, df):
        """RSI + MACD-histogram latest-pivot slopes with the OB/OS regime boundary REMOVED
        (ob/os=None) — the force-close fallback when the bounded leg slopes are grey (no pivot)."""
        rsi_s = self.indicator.calculate_rsi(df["Close"], RSI_LENGTH, RSI_MA_TYPE)
        macd_df = self.indicator.calculate_macd(df["Close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        r = self.indicator.latest_pivot_slope(rsi_s, lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH)
        sig = float(macd_df["signal"].iloc[-1]) if len(macd_df) else 0.0
        side = "high" if sig > 0 else ("low" if sig < 0 else None)
        m = self.indicator.latest_pivot_slope(
            macd_df["hist"], lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH, side=side)
        return {"rsi": r, "macd": m}

    def _get_display_pair(self):
        """CE/PE pair used for entries + the dashboard. In a trade -> the open basket's pair.
        Otherwise the pair is LOCKED for the trading day and only re-scanned once PAIR_RELOCK_SECONDS
        has elapsed or the day rolls over, so the bot STICKS to one pair instead of drifting every few
        minutes (two bots on the same clock pick the same one). A scan that returns nothing valid keeps
        the current lock (retry next tick), so a transient empty scan never drops the day's pair."""
        if self.active and self._last_pair:
            return self._last_pair
        now = monotonic()
        today = now_ist().date().isoformat()
        if (self._locked_pair and self._locked_day == today
                and (PAIR_RELOCK_SECONDS <= 0 or (now - self._locked_at) < PAIR_RELOCK_SECONDS)):
            return self._locked_pair
        lot_df = self.ctx.commodity_df if self.asset_type == "COMMODITY" else self.ctx.option_df
        pair = scanner.scan_best_pair(self.cfg, self.ctx.api, self.ctx.meter, lot_df=lot_df)
        if pair and pair.get("CE_Symbol") and pair.get("PE_Symbol"):
            self._locked_pair, self._locked_at, self._locked_day = pair, now, today
            return pair
        return self._locked_pair          # scan empty/failed -> keep the day's pair, retry next tick

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
                   "target": ad.get("close_threshold"),
                   "mg": legdict.get("count", 1) - 1}
        def _signs(raw):
            return {tf: {"rsi": (v.get("rsi") or {}).get("sign", 0),
                         "macd": (v.get("macd") or {}).get("sign", 0)}
                    for tf, v in (raw or {}).items()}
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
            "slopes": _signs(ad.get("slopes")),               # index slopes (open)
            "leg_slopes": _signs(ad.get("leg_slopes")),       # leg slopes (book/fc)
            "ce_slopes": _signs(ad.get("ce_slopes")),         # CE leg slopes (dashboard readout)
            "pe_slopes": _signs(ad.get("pe_slopes")),         # PE leg slopes (dashboard readout)
            "entry_allowed": ad.get("entry_allowed", True),
            "entry_chop_block": ad.get("entry_chop_block", False),
            "exit_allowed": ad.get("exit_allowed", True),
            "min_hold_left": ad.get("min_hold_left", 0),
            "exit_reason": ad.get("exit_reason"),
            "variant": ad.get("variant", "netting"),
            "rsi": ad.get("rsi_mtf", {}),
            "rsi_graphs": ad.get("rsi_graphs", {}),           # index pivot charts (open)
            "macd_graphs": ad.get("macd_graphs", {}),
            "leg_rsi_graphs": ad.get("leg_rsi_graphs", {}),   # leg pivot charts (book/fc)
            "leg_macd_graphs": ad.get("leg_macd_graphs", {}),
            "close_threshold": ad.get("close_threshold"),
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
    def _should_trade_now(self) -> bool:
        """Master trade gate. PRIMARY: the broker's LIVE segment status — trade iff this asset's
        exchange says OPEN right now. That auto-covers Diwali Muhurat, Union Budget and ANY unusual
        session with zero hardcoded dates/times, and also stops us dead on a surprise closure.
        FALLBACK (no fresh live reading): the static schedule, but ONLY on a genuine scheduled day
        (weekday & not an NSE holiday) — so without live confirmation we never trade a would-be
        special day, and never trade a holiday."""
        live = self.ctx.is_segment_open(self.asset_type)   # True / False / None(unknown)
        if live is not None:
            return live
        today = now_ist().date()
        if today.weekday() >= 5 or today.isoformat() in self.ctx.holidays:
            return False
        return self._in_window()

    def _in_window(self) -> bool:
        """Static schedule check (fallback only — the live broker status is the primary gate)."""
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
                    self._entry_mono = monotonic()   # min-hold from adoption (avoid an instant exit on a stale leg pivot after restart)
                    avg = float(row.get("netAvg", row.get("buyAvg", 0.0)) or 0.0)
                    self.ctx.tracker.seed_from_broker(row.get("symbol"), abs(net), avg,
                                                      1 if side == "CE" else -1)
                    logger.info("[%s] adopted carried %s %s (avg~%s)",
                                self.symbol, side, row.get("symbol"), avg)
                    return
        except Exception as e:
            logger.warning("[%s] recover_active failed: %s", self.symbol, e)
