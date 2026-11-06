"""
strategy.py — RSI+MACD slope strategy (NETTING / variant A: book-score profit exit).

PORTED FROM Alcadeias `rsimacd_btcusd1_wd_exp_h` strategy.py (MT5). SHA is removed:
entry, force-close and the DCA ladder-add gate are decided by three per-section
weighted slope scores built from RSI + MACD-histogram latest-pivot slopes (see
indicator.latest_pivot_slope + constants *_SCORE_WEIGHTS). The hedging→netting
adaptation lives in the execution layer (symbol_worker), which maps:
    BUY  / BUY_MORE  / CLOSE_BUY   → buy / add / sell the CE leg
    SELL / SELL_MORE / CLOSE_SELL  → buy / add / sell the PE leg

PROFIT-BOOK (variant A). Alcadeias books when the basket is in profit by at least
`times * PROFIT_PROTECT_USD_PER_UNIT` ($1/unit) AND the BOOK score has reversed. On
Fyers the floor is passed in as `close_threshold` = HEDGE_PER_UNIT*units + charges
(₹, tax-aware), so a book only fires once the basket's gross P&L clears ₹100×units +
charges (i.e. nets ≥ ₹100×units) AND the BOOK score reverses — faithful to Alcadeias
(floor + momentum reversal) while guaranteeing each booked trade is net-positive.
The HEDGING branch drops the book-score reversal and books at that same floor.
"""
from enum import Enum
from constants import (
    FIBO_SEQUENCE_LENGTH,
    RSI_OVERSOLD, RSI_OVERBOUGHT,
    ENTRY_S, ENTRY_AGREE_S, LADDER_S, FORCE_CLOSE_S, BOOK_S,
    OPEN_SCORE_WEIGHTS, BOOK_SCORE_WEIGHTS, FORCE_CLOSE_SCORE_WEIGHTS,
)


class Signal(Enum):
    DO_NOTHING = 0
    BUY = 1
    SELL = 2
    CLOSE_BUY = 3
    CLOSE_SELL = 4
    BUY_MORE = 5
    SELL_MORE = 6


class Strategy:
    """RSI+MACD slope strategy — slope entry, RSI Fibo DCA ladder, slope exits (variant A book)."""

    def __init__(self):
        self.hedge = 1  # vestigial: calculate_signal uses times/close_threshold instead

    def _recur_fibo(self, n):
        if n <= 1:
            return n
        return self._recur_fibo(n - 1) + self._recur_fibo(n - 2)

    def _get_fibo_qty(self, qty_count, times):
        fib = [self._recur_fibo(i) for i in range(FIBO_SEQUENCE_LENGTH)][2:]
        try:
            return fib[qty_count] * times
        except (IndexError, ValueError):
            return times

    def _get_next_fibo_volume(self, total_volume, times):
        """
        Next fibonacci volume so each placed position is a fib value (kept for parity
        with the Alcadeias port; the Fyers worker sizes adds via its own FIB ladder).
        """
        fib = [self._recur_fibo(i) for i in range(FIBO_SEQUENCE_LENGTH)][2:]
        total_units = round(total_volume * 100 / times) if times else 0
        cumulative = 0
        for i, f in enumerate(fib):
            cumulative += f
            if cumulative >= total_units:
                try:
                    return round(fib[i + 1] * times / 100, 2)
                except IndexError:
                    return round(0.01 * times, 2)
        return round(0.01 * times, 2)

    @staticmethod
    def _slope_sign(slopes, tf, ind):
        """Slope sign (+1/-1/0) for (timeframe, indicator); 0 if missing/undefined."""
        try:
            s = slopes[tf][ind]['sign']
        except (KeyError, TypeError, IndexError):
            return 0
        return s if s in (-1, 0, 1) else 0

    @staticmethod
    def _weighted_score(slopes, weights, grey_fill=False):
        """RAW weighted slope score for one section's {TF: weight} map.
            score = Σ weight * (rsi_sign + macd_sign)   over the map  (each sign ∈ {-1,0,+1})
        Range ±(2 * Σ|weight|): e.g. {'H4':1,'M5':1} -> -4..+4 ; {'M15':1} -> -2..+2 ; the
        4/3/2/1 force-close set -> -20..+20. A missing/zero slope contributes 0; empty map -> 0.

        grey_fill (OPEN + BOOK): when EXACTLY ONE of a timeframe's two signs is 0 (grey — its
        RSI or MACD is at a peak/plateau so the pivot slope is undefined), substitute the OTHER
        sign for the grey one, so a timeframe clear on one indicator and grey on the other counts
        as a FULL aligned vote (±2·w) instead of half. Both grey -> the timeframe stays 0. The
        FORCE-CLOSE score keeps RAW signs (grey_fill off)."""
        total = 0
        for tf, w in weights.items():
            r = Strategy._slope_sign(slopes, tf, 'rsi')
            m = Strategy._slope_sign(slopes, tf, 'macd')
            if grey_fill:
                if r == 0 and m != 0:      # RSI grey -> borrow MACD's direction
                    r = m
                elif m == 0 and r != 0:    # MACD grey -> borrow RSI's direction
                    m = r
            total += w * (r + m)
        return total

    @staticmethod
    def _entry_direction(open_score, fc_score):
        """Fresh-entry direction. Opens only when the OPEN score is clearly directional AND the
        FORCE-CLOSE score is NOT opposing it (agreement gate):
          open_score >= ENTRY_S  and fc_score >= ENTRY_AGREE_S   -> BUY
          open_score <= -ENTRY_S and fc_score <= -ENTRY_AGREE_S  -> SELL
          else                                                   -> WAIT
        Stops the open-then-instant-force-close "vibration": when OPEN and FORCE-CLOSE disagree
        (chop) the bot WAITS. Returns 'BUY' / 'SELL' / None."""
        if open_score >= ENTRY_S and fc_score >= ENTRY_AGREE_S:
            return 'BUY'
        if open_score <= -ENTRY_S and fc_score <= -ENTRY_AGREE_S:
            return 'SELL'
        return None

    def _slope_direction(self, slopes):
        """
        Structural 4H / 1H / 15M latest-pivot read (dashboard / compare only; entry uses the
        OPEN score, not this). 4H primary, 1H confirm, 15M trigger (D1 removed on this "_wd" base).
          H4 both +ve & (>=1 H1 +ve) & (>=1 M15 +ve)  -> BUY
          H4 both -ve & (>=1 H1 -ve) & (>=1 M15 -ve)  -> SELL
          H4 split    & H1 both +ve  & (>=1 M15 +ve)  -> BUY
          H4 split    & H1 both -ve  & (>=1 M15 -ve)  -> SELL
          else                                        -> WAIT
        Returns 'BUY', 'SELL', or None (WAIT).
        """
        slopes = slopes or {}
        h4r = self._slope_sign(slopes, 'H4', 'rsi')
        h4m = self._slope_sign(slopes, 'H4', 'macd')
        h1r = self._slope_sign(slopes, 'H1', 'rsi')
        h1m = self._slope_sign(slopes, 'H1', 'macd')
        m15r = self._slope_sign(slopes, 'M15', 'rsi')
        m15m = self._slope_sign(slopes, 'M15', 'macd')

        m15_up = (m15r > 0 or m15m > 0)
        m15_dn = (m15r < 0 or m15m < 0)

        if h4r > 0 and h4m > 0:
            return 'BUY' if ((h1r > 0 or h1m > 0) and m15_up) else None
        if h4r < 0 and h4m < 0:
            return 'SELL' if ((h1r < 0 or h1m < 0) and m15_dn) else None
        if (h4r > 0 and h4m < 0) or (h4r < 0 and h4m > 0):
            if h1r > 0 and h1m > 0 and m15_up:
                return 'BUY'
            if h1r < 0 and h1m < 0 and m15_dn:
                return 'SELL'
            return None
        return None

    def calculate_signal(self, source_df,
                         buy_positions, sell_positions, times,
                         close_threshold=2,
                         rsi_value=None, rsi_mtf=None, slopes=None, leg_slopes=None,
                         cand_leg_slopes=None, leg_slopes_raw=None,
                         entry_allowed=True, exit_allowed=True):
        """
        RSI+MACD slope strategy (NETTING / book-score profit exit). Three per-section weighted
        scores, each from its OWN configurable timeframe set (constants OPEN/BOOK/FORCE_CLOSE
        _SCORE_WEIGHTS):
          - open_score  -> fresh entry + the DCA ladder-add gate   (default TFs: H4, M5)
          - book_score  -> profit-book (this variant)              (default TF: M15)
          - fc_score    -> force-close                             (default TFs: M15,H1,H4,D1 w4/3/2/1)

          Flat: open when the OPEN score is directional AND the FORCE-CLOSE score agrees
                (agreement gate); else WAIT. Single position, cooldown-gated.
          Open BUY: exits checked FIRST, then the RSI DCA ladder —
            - profit-book: once buy_profit > close_threshold (= HEDGE_PER_UNIT*units + charges,
              so net ≥ ₹100×units) AND book_score <= -BOOK_S -> CLOSE_BUY (reason 'book').
            - force-close: fc_score <= -FORCE_CLOSE_S -> CLOSE_BUY (reason 'force_close').
            - DCA add (gated on open_score): BUY_MORE at RSI 35, one tier per count
              1->M1 2->M5 3->M15 4->H1 5->H4 (max 6 positions).
          Open SELL mirrors.

        Args:
            source_df: price feed (metadata / liveness only; not used by the score logic).
            buy_positions / sell_positions: leg dicts from the worker or None.
            times: effective unit; scales the Fibo lot ladder.
            close_threshold: tax-aware ₹ profit floor (HEDGE_PER_UNIT*units + charges) — the
                   profit-book floor for this variant.
            rsi_value: current RSI (dashboard/logs).
            rsi_mtf: {timeframe_name: rsi_value} — drives the DCA ladder tiers.
            slopes: {tf: {'rsi': <pivot-slope>, 'macd': <pivot-slope>}} per fetched timeframe.
            entry_allowed: False during the post-close re-entry cooldown.

        Returns:
            tuple: (buy_signal, sell_signal, analysis_data)
        """
        # Extract position data
        buy_count = buy_positions['count'] if buy_positions else 0
        buy_profit = buy_positions['total_profit'] if buy_positions else 0
        sell_count = sell_positions['count'] if sell_positions else 0
        sell_profit = sell_positions['total_profit'] if sell_positions else 0

        current_rsi = rsi_value if rsi_value is not None else 50.0

        buy_status = Signal.DO_NOTHING
        sell_status = Signal.DO_NOTHING
        exit_reason = None

        # Per-timeframe RSI for the DCA ladder tiers (count 1->M1 ... 5->H4)
        rsi_1m = rsi_mtf.get('TIMEFRAME_M1', 50.0) if rsi_mtf else 50.0
        rsi_5m = rsi_mtf.get('TIMEFRAME_M5', 50.0) if rsi_mtf else 50.0
        rsi_15m = rsi_mtf.get('TIMEFRAME_M15', 50.0) if rsi_mtf else 50.0
        rsi_1h = rsi_mtf.get('TIMEFRAME_H1', 50.0) if rsi_mtf else 50.0
        rsi_4h = rsi_mtf.get('TIMEFRAME_H4', 50.0) if rsi_mtf else 50.0

        slope_direction = self._slope_direction(slopes)   # structural read (dashboard/compare only)

        in_position = (buy_count + sell_count) > 0

        # ── Per-section RAW weighted slope scores ──
        # OPEN (entry + ladder-add gate) + the entry agreement-gate veto are read on the INDEX.
        open_score = self._weighted_score(slopes, OPEN_SCORE_WEIGHTS, grey_fill=True)
        fc_gate = self._weighted_score(slopes, FORCE_CLOSE_SCORE_WEIGHTS)   # index FC → entry veto
        # BOOK + FORCE-CLOSE exits are read on the ACTIVE LEG's own option chart (leg_slopes).
        book_score = self._weighted_score(leg_slopes, BOOK_SCORE_WEIGHTS, grey_fill=True)
        fc_leg = self._weighted_score(leg_slopes, FORCE_CLOSE_SCORE_WEIGHTS)
        # Grey-slope fallback (owner fix): the force-close M15 slope can sit grey (no pivot) too long
        # so the basket never closes. If M15 is grey, fall back to the leg M5; if M5 is also grey,
        # take out the RSI OB/OS regime boundary (unbounded raw slopes) and read M15 then M5.
        if fc_leg == 0:
            fc_leg = self._weighted_score(leg_slopes, {'M5': 1})
        if fc_leg == 0 and leg_slopes_raw:
            fc_leg = (self._weighted_score(leg_slopes_raw, FORCE_CLOSE_SCORE_WEIGHTS)
                      or self._weighted_score(leg_slopes_raw, {'M5': 1}))
        # Dashboard FC meter shows the leg FC when in a basket, else the index entry-gate FC.
        fc_score = fc_leg if in_position else fc_gate

        # Fresh-entry direction (agreement gate, on the index) — kept for the dashboard/compare.
        entry_direction = self._entry_direction(open_score, fc_gate)
        open_wants = 'BUY' if open_score >= ENTRY_S else ('SELL' if open_score <= -ENTRY_S else None)
        entry_chop_block = bool(open_wants and entry_direction is None)

        # NEW fresh-entry gate (owner): INDEX H4 both-directional + the CANDIDATE option leg's OWN
        # H4 and M5 both +ve, and the leg's M15 (force-close TF) not already reversing — never open
        # into an immediate force-close (anti-vibration). This replaces the index-only fresh entry.
        def _both_pos(sl, tf):
            v = (sl or {}).get(tf, {})
            return ((v.get('rsi') or {}).get('sign', 0) > 0) and ((v.get('macd') or {}).get('sign', 0) > 0)

        def _both_neg(sl, tf):
            v = (sl or {}).get(tf, {})
            return ((v.get('rsi') or {}).get('sign', 0) < 0) and ((v.get('macd') or {}).get('sign', 0) < 0)

        def _tf_against(sl, tf):
            v = (sl or {}).get(tf, {})
            return ((v.get('rsi') or {}).get('sign', 0) < 0) or ((v.get('macd') or {}).get('sign', 0) < 0)

        _leg_entry_ok = (_both_pos(cand_leg_slopes, 'H4') and _both_pos(cand_leg_slopes, 'M5')
                         and not _tf_against(cand_leg_slopes, 'M15'))
        new_entry = ('BUY' if (_both_pos(slopes, 'H4') and _leg_entry_ok)
                     else ('SELL' if (_both_neg(slopes, 'H4') and _leg_entry_ok) else None))

        # DCA ladder-add gate: average in only while the OPEN (index) score is not strongly adverse.
        buy_add_ok = open_score > -LADDER_S
        sell_add_ok = open_score < LADDER_S

        # Force-close (EXIT, on the LEG): close when the leg FORCE-CLOSE score is far enough AGAINST.
        # Suppressed during the post-entry min-hold (exit_allowed) so we never open-then-close.
        buy_fc = exit_allowed and (fc_leg <= -FORCE_CLOSE_S)
        sell_fc = exit_allowed and (fc_leg >= FORCE_CLOSE_S)

        # Profit-book (variant A, on the LEG): once profit clears the tax-aware floor AND the leg
        # BOOK score reverses. Checked BEFORE the force-close. Also min-hold gated.
        profit_protect_min = close_threshold
        buy_pp = exit_allowed and (buy_profit > profit_protect_min) and (book_score <= -BOOK_S)
        sell_pp = exit_allowed and (sell_profit > profit_protect_min) and (book_score >= BOOK_S)

        if buy_count == 0 and sell_count == 0:
            # Fresh entry: index-H4 + candidate-leg-H4 + leg-M5 gate (anti-vibration), cooldown-gated.
            if entry_allowed and new_entry == 'BUY':
                buy_status = Signal.BUY
            elif entry_allowed and new_entry == 'SELL':
                sell_status = Signal.SELL

        elif buy_count > 0 and sell_count == 0:
            # Open BUY: slope exits FIRST, then the RSI DCA ladder (threat = DOWN move).
            if buy_pp:
                buy_status = Signal.CLOSE_BUY
                exit_reason = 'book'
            elif buy_fc:
                buy_status = Signal.CLOSE_BUY
                exit_reason = 'force_close'
            elif buy_add_ok:
                if rsi_1m <= RSI_OVERSOLD and buy_count == 1:
                    buy_status = Signal.BUY_MORE
                elif rsi_5m <= RSI_OVERSOLD and buy_count == 2:
                    buy_status = Signal.BUY_MORE
                elif rsi_15m <= RSI_OVERSOLD and buy_count == 3:
                    buy_status = Signal.BUY_MORE
                elif rsi_1h <= RSI_OVERSOLD and buy_count == 4:
                    buy_status = Signal.BUY_MORE
                elif rsi_4h <= RSI_OVERSOLD and buy_count == 5:
                    buy_status = Signal.BUY_MORE

        elif buy_count == 0 and sell_count > 0:
            # Open SELL (mirror): slope exits FIRST, then the RSI DCA ladder.
            if sell_pp:
                sell_status = Signal.CLOSE_SELL
                exit_reason = 'book'
            elif sell_fc:
                sell_status = Signal.CLOSE_SELL
                exit_reason = 'force_close'
            elif sell_add_ok:
                if rsi_1m >= RSI_OVERBOUGHT and sell_count == 1:
                    sell_status = Signal.SELL_MORE
                elif rsi_5m >= RSI_OVERBOUGHT and sell_count == 2:
                    sell_status = Signal.SELL_MORE
                elif rsi_15m >= RSI_OVERBOUGHT and sell_count == 3:
                    sell_status = Signal.SELL_MORE
                elif rsi_1h >= RSI_OVERBOUGHT and sell_count == 4:
                    sell_status = Signal.SELL_MORE
                elif rsi_4h >= RSI_OVERBOUGHT and sell_count == 5:
                    sell_status = Signal.SELL_MORE

        analysis_data = {
            'rsi_value': round(current_rsi, 2),
            'rsi_mtf': rsi_mtf or {},
            'slopes': slopes or {},
            'leg_slopes': leg_slopes or {},
            'slope_direction': slope_direction or 'WAIT',
            'entry_direction': entry_direction or 'WAIT',
            'new_entry': new_entry or 'WAIT',
            'cand_leg_slopes': cand_leg_slopes or {},
            'open_score': round(open_score, 1),
            'book_score': round(book_score, 1),
            'fc_score': round(fc_score, 1),
            'buy_add_ok': bool(buy_add_ok),
            'sell_add_ok': bool(sell_add_ok),
            'entry_allowed': bool(entry_allowed),
            'exit_allowed': bool(exit_allowed),
            'entry_chop_block': bool(entry_chop_block),
            'exit_reason': exit_reason,
            'variant': 'netting',
        }

        return buy_status, sell_status, analysis_data
