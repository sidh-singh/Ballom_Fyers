# TRADING_LOGIC — `fyers_hedging_live` / `fyers_hedging_demo`  (RSI+MACD, variant B)

> **Status:** 🟢 Implemented
> **Source of truth:** Alcadeias MT5 bot, branch `rsimacd_h6_btcusd1_wd_exp_h`
> (`C:/Users/sings213/Alcadeias/Alcadeias`) — the **RSI+MACD slope** strategy
> (SHA removed), reproduced exactly, adapted from MT5 **hedging** to Fyers
> **netting** (buy-only, clubbed & averaged).
> **Variant B (HEDGING):** profit booked at a **fixed tax-aware ₹ target**
> (`₹100×units + charges`, the current-Fyers rule). The sibling `fyers_netting_*`
> branches book on a BOOK-score reversal instead (variant A) — everything else is
> identical.
> **Last updated:** 2026-11-03

This documents *what* the strategy does. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the engine it plugs into. Every change is logged in [CHANGELOG.md](CHANGELOG.md).

---

## 1. The RSI+MACD slope strategy (exactly as Alcadeias)

No SHA. Direction is read from **latest-pivot slopes** of RSI and the MACD
histogram across several timeframes, combined into **per-section weighted scores**
that drive entry and force-close. A mean-reversion **Fibonacci DCA ladder** (RSI
extremes) averages into an open basket, which is booked at a fixed ₹ target.

### 1.1 Indicators (`indicator.py`, reproduced verbatim)
| Indicator | Config | Purpose |
|---|---|---|
| **RSI** | `RMA`, length **14** | DCA ladder tiers + RSI pivot slopes |
| **MACD** | TV `ta.macd` **12 / 26 / 9** (SMA-seeded EMA) → `macd, signal, hist` | MACD-histogram pivot slopes |
| **latest_pivot_slope** | lookback **20**, pivot width **2** | sign (+/−/0) of the newest confirmed pivot → now |

**latest_pivot_slope** scans back from the newest *confirmable* bar and returns the
**first** turning point (the latest pivot, not the largest): a bar strictly above
(high) / below (low) the `width=2` bars on both sides. Slope =
`(current − pivot_value) / bars_since_pivot`; `sign` = +1 / −1 / 0.
- **RSI** pivots only count inside the OB/OS bands: a HIGH needs value ≥ **`RSI_PEAK_OB=60`**,
  a LOW needs value ≤ **`RSI_PEAK_OS=40`** (neutral 41–59 wiggles ignored).
- **MACD** histogram is unbounded; the **signal line's sign picks the side** —
  `signal > 0` → read the HIGH side, `signal < 0` → the LOW side.

Slopes are computed on the **index** for `SLOPE_TIMEFRAMES` = **M5, M15, H4**
(the de-duplicated union of the sections' timeframes — all already pulled for the RSI
ladder, so slopes add no extra history calls; D1/H1 are no longer used for slopes).

### 1.2 Per-section weighted scores (`strategy._weighted_score`)
Each decision **section** has its own `{timeframe: weight}` map; its score is the RAW
weighted sum of the latest-pivot signs, range **±(2·Σ|weight|)**:

```
score = Σ_tf  weight_tf · (rsi_sign_tf + macd_sign_tf)      each sign ∈ {−1, 0, +1}
```

| Section | Timeframes · weights (`constants.py`) | Range | Threshold | Used for |
|---|---|---|---|---|
| **OPEN** | `{H4:1, M5:1}` | ±4 | `ENTRY_S=2`, `LADDER_S=2` | fresh entry + ladder-add gate |
| **FORCE_CLOSE** | `{M15:1}` | ±2 | `FORCE_CLOSE_S=1` | force-close + entry agreement gate — **either** M15 slope reverses |

(There is **no BOOK section** in variant B — the profit exit is the fixed ₹ target,
not a score. `BOOK_SCORE_WEIGHTS` is still defined in `constants.py` for parity but
unused here.)

**grey_fill** (OPEN only): if exactly one of a timeframe's two signs is 0 (its RSI or
MACD is at a peak/plateau — pivot undefined), the grey sign borrows the other
indicator's direction, so a TF clear on one indicator counts as a **full** aligned
vote (±2·w) instead of half. Both grey → the TF stays 0. FORCE_CLOSE uses **raw**
signs (no grey_fill).

> **FORCE_CLOSE = either.** With `FORCE_CLOSE={M15:1}` and `FORCE_CLOSE_S=1`, a force-close
> fires at M15 score ±1 — i.e. **either** the M15 RSI or MACD slope turning the net M15 slope
> adverse to the basket. The same M15 score is the entry agreement-gate veto.

### 1.3 Signals
`DO_NOTHING, BUY, SELL, CLOSE_BUY, CLOSE_SELL, BUY_MORE, SELL_MORE`.

**Entry (only when FLAT — both legs 0) — the agreement gate:**
- `BUY`  if `open_score ≥ +ENTRY_S` **and** `fc_score ≥ +ENTRY_AGREE_S` (`=0`).
- `SELL` if `open_score ≤ −ENTRY_S` **and** `fc_score ≤ −ENTRY_AGREE_S`.
- else **WAIT**. When OPEN wants in but FORCE-CLOSE opposes (chop), the bot WAITS
  rather than open-then-instantly-force-close (`entry_chop_block` telemetry).
- **Cooldown:** a fresh entry is blocked for **`REENTRY_COOLDOWN_SECONDS=300`** after
  any close (`entry_allowed=False`).

**Ladder / DCA (one side open) — max 6 positions.** Exits are checked FIRST (close
beats add); the add is gated so it only averages in while OPEN is **not** strongly
adverse (`buy_add_ok = open_score > −LADDER_S`):

| Position count | Add trigger (BUY side) | Action |
|---|---|---|
| profit target (checked first) | `cycle_pl > close_threshold` | `CLOSE_BUY` (reason `target`) |
| force-close (checked next) | `fc_score ≤ −FORCE_CLOSE_S` | `CLOSE_BUY` (reason `force_close`) |
| 1 | RSI **M1** ≤ 35 | `BUY_MORE` |
| 2 | RSI **M5** ≤ 35 | `BUY_MORE` |
| 3 | RSI **M15** ≤ 35 | `BUY_MORE` |
| 4 | RSI **H1** ≤ 35 | `BUY_MORE` |
| 5 | RSI **H4** ≤ 35 | `BUY_MORE` |

SELL side is the mirror (RSI ≥ 65; `fc_score ≥ +FORCE_CLOSE_S`). After 5 adds (6
positions) there are no further ladder tiers — the basket is held until the target or
force-close fires. There is **no H6 final-close** (that was the HAM design);
force-close replaces it.

### 1.4 Fibonacci volume ladder
Unchanged from the Fyers base. Positions are opened as the fib sequence
`[1,2,3,5,8,13,…] × base`: **entry** = `1 × base`, **adds** = `2,3,5,8,13 × base`
(`FIB[mg+1] × units × lot` in `symbol_worker._add_leg`). `base` scales with the
running **unit** (§3).

### 1.5 Profit booking — **variant B (fixed ₹ target)**
Close the whole basket the moment it clears the tax-aware target:

```
buy_profit > close_threshold   → CLOSE_BUY ('target')
```

- `close_threshold = ₹(HEDGE_PER_UNIT × units) + charges` with **`HEDGE_PER_UNIT=100`**
  → **+₹100 per unit plus estimated taxes/charges** (§4), so the booked trade nets
  **≥ ₹100×units**. As the balance tier raises `units`, the target scales up
  (unit 1 → ~₹100, unit 2 → ~₹200, …), exactly the current-Fyers booking rule.
- This is the Alcadeias `buy_profit > close_threshold` (+$unit) target, with the
  `$unit` target replaced by the tax-aware Fyers target. **No BOOK-score reversal.**
- Checked **before** the force-close.

> **Variant A (`fyers_netting_*`)** keeps the same floor but **also** requires a
> `book_score` reversal (`M15`) before booking, so winners run until momentum turns.
> That is the only difference between the two branch pairs.

---

## 2. Hedging → Netting adaptation (buy-only, clubbed & averaged)

Fyers is a **netting**, buy-only options account, so the MT5 hedge is expressed as
two independent buy-only legs on the **index options**:

| Strategy signal | Fyers action |
|---|---|
| `BUY` / `BUY_MORE` / `CLOSE_BUY` | buy / add-average / sell the **CE** leg |
| `SELL` / `SELL_MORE` / `CLOSE_SELL` | buy / add-average / sell the **PE** leg |

Per-cycle P&L is a **self-tracked cost basis** (carry-safe), not the broker's booked
field: `cycle_pl = ltp × net_qty − cost_basis` (`position_tracker.py`). `cycle_pl`
is what the strategy sees as `total_profit`, and the close books
`net = gross − charges` into `profit_history.json`.

---

## 3. Position sizing — balance-tiered units
`units = min(floor(balance / STEP_UP_BALANCE) + 1, UNIT_MAX_LIMIT)` with
`STEP_UP_BALANCE = ₹100,000` and `UNIT_MAX_LIMIT = 10` (per-symbol override in
`symbols.json`). Units are fixed at entry and drive both the Fibo lot ladder and the
`close_threshold` target. The per-contract lot comes from the downloaded NSE/MCX CSV.

---

## 4. Charges / taxes (tax-aware target)
`estimate_trade_charges(qty, price, num_orders)` (`constants.py`, verified against
fyers.in/charges-list) sums brokerage (₹20, ₹15 Prime), STT 0.15% sell, exchange txn
0.0355299% (NSE), SEBI ₹10/cr, stamp 0.003% buy, GST 18%. It feeds both the ₹ target
and the booked `net_pl`.

---

## 5. Data flow per tick (`symbol_worker._tick`)
1. Resolve underlying (commodity future from MCX CSV; index as-is) + trading-window guard.
2. Close-first: if a close is pending, confirm it (and start the cooldown) before anything else.
3. `_leg_state_and_threshold()` → leg `buy_pos`/`sell_pos` from cost basis + `close_threshold`.
4. `_slopes_and_rsi()` → per-TF RSI (ladder tiers) + RSI/MACD latest-pivot slopes
   (one fetch per TF, **cached per bar** so the DATA budget is respected).
5. `calculate_signal(... slopes, entry_allowed ...)` → BUY/SELL/ADD/CLOSE + analysis.
6. `_execute()` maps the signal to the CE/PE leg; the dashboard payload is written
   (scores, per-TF slope arrows, entry direction, RSI, pair, position).
