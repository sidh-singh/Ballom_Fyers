# TRADING_LOGIC — `fyers_hedging_live` / `fyers_hedging_demo`  (RSI+MACD, variant B)

> **Status:** 🟢 Implemented
> **Source of truth:** Alcadeias MT5 bot, branch `rsimacd_h6_btcusd1_wd_exp_h`
> (`C:/Users/sings213/Alcadeias/Alcadeias`) — the **RSI+MACD slope** strategy
> (SHA removed), adapted from MT5 **hedging** to Fyers **netting** (buy-only).
> **Variant B (HEDGING):** profit booked at a **fixed tax-aware ₹ target**; the
> sibling `fyers_netting_*` branches book on an M5 reversal instead (variant A).
> Everything else — entry, force-close, DCA ladder — is identical.
> **Last updated:** 2026-11-10

This documents *what* the strategy does. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the engine it plugs into. Every change is logged in [CHANGELOG.md](CHANGELOG.md).

---

## 0. The unified CE/PE model (read this first)

The account is **buy-only**: a bullish view is a **long CE**, a bearish view a **long PE**.
**Both legs are long options** — each profits when its **own premium rises** — and the
self-tracked P&L is long-only (`cycle_pl = ltp×net_qty − cost_basis`; the stored `side` is
ignored). So **every per-leg rule is identical for CE and PE**: add on the leg's own dips,
book/force-close when the leg's own momentum turns down. The only place direction matters is
*picking* CE vs PE (from the index) and *borrowing* the index as a backup, where a **polarity**
applies: a CE tracks the index (`pol = +1`), a PE is inverse (`pol = −1`, i.e. `−index`).

> A long PE rises when the index **falls** — standard negative-delta. Earlier builds mistakenly
> *mirrored* the BUY↔SELL thresholds on the PE's own chart (an index-era habit), so a PE had no
> real stop, booked on continuation, and averaged into highs. Fixed 2026-11-10 — see CHANGELOG.

### The decision unit — `_decisive(tf)` + fallback chains
A timeframe **counts only when BOTH its RSI and MACD latest-pivot slopes agree** →
`_decisive` returns **+1** (both up) / **−1** (both down) / **0** (grey *or* mismatch). A `0`
is "no read" → the caller **walks a fallback chain**: the **leg's own** finer timeframe, then the
**index** (polarity-adjusted). This replaces the old same-TF `grey_fill` indicator-borrow.

---

## 1. The RSI+MACD slope strategy

### 1.1 Indicators (`indicator.py`)
| Indicator | Config | Purpose |
|---|---|---|
| **RSI** | `RMA`, length **14** | DCA ladder tiers + RSI pivot slopes |
| **MACD** | TV `ta.macd` **12 / 26 / 9** → `macd, signal, hist` | MACD-histogram pivot slopes |
| **latest_pivot_slope** | lookback **20**, pivot width **2** | sign (+/−/0) of the newest confirmed pivot → now |

`latest_pivot_slope` scans back from the newest *confirmable* bar for the **first** turning point
(a bar strictly above/below the `width=2` bars each side); `slope = (current − pivot)/bars`,
`sign = +1/−1/0`. **RSI** pivots count only inside the bands (HIGH ≥ `RSI_PEAK_OB=60`, LOW ≤
`RSI_PEAK_OS=40`); the **MACD** signal-line sign picks the side. A monotonic run with no pivot → `0`.

**Slope sources (2026-11-10).** The **INDEX** computes slope signs for **all fetched TFs**
(`M1·M5·M15·H1·H4` — the frames are already pulled for the ladder RSI, so it's free). The **active
leg** (CE/PE option's own candles) computes `M5·M15·H1·H4` (+ `M1`). While flat, the **candidate
leg** fetches `OPEN_LEG_TIMEFRAMES = H4·H1·M5·M1` (H4+M5 gate, H1+M1 the grey backups). Anti-vibration:
after an entry, book + force-close are suppressed for `MIN_HOLD_SECONDS` (180 s).

### 1.2 The ladder-add trend filter (`open_score`)
The only remaining **weighted** score. `open_score = Σ weight·(rsi+macd)` over `{H4:1, M5:1}` on the
**index** (±4, `grey_fill` kept here as a coarse filter). It gates **averaging only**: an add is
allowed while the index is not strongly adverse (`buy_add_ok = open_score > −LADDER_S`, `LADDER_S=2`).
It does **not** decide entry or exit anymore.

### 1.3 Signals
`DO_NOTHING, BUY, SELL, CLOSE_BUY, CLOSE_SELL, BUY_MORE, SELL_MORE`.

**Fresh entry (only when FLAT).** Direction from the index, then the candidate leg must be
**advancing** on two timeframes (each via its own fallback chain; `+1` = advancing):

| Step | Resolution chain (stop at first decisive) |
|---|---|
| **Direction** (picks CE/PE) | index **H4** → index **H1** |
| **H4 confirm** | leg **H4** → leg **H1** → index **H1**·pol |
| **M5 confirm** | leg **M5** → index **M5**·pol → leg **M1** → index **M1**·pol |

`BUY` (open CE) when direction = bullish **and** both confirms = +1; `SELL` (open PE) when
bearish **and** both confirms = +1; else **WAIT**. Cooldown: a fresh entry is blocked for
`REENTRY_COOLDOWN_SECONDS = 600` after any close.

**Force-close = trend-reversal stop (identical for CE & PE).** A weighted multi-timeframe reversal
of the **active leg's own** chart; where a leg TF is grey, the **index** same-TF is borrowed
(polarity-adjusted):

```
fc_score = Σ_tf  weight_tf · 2 · resolve(leg_decisive(tf)  else  index_decisive(tf)·pol)
weights  = {M15:4, H1:3, H4:2}      range ±18      fire at fc_score ≤ −FORCE_CLOSE_S (=12)
```

Both a long CE and a long PE close when the option turns **down** (`fc ≤ −12`). A single-TF wobble
can't fire it (needs e.g. M15+H1 or M15+H4 reversed); **if every TF is grey on leg *and* index →
`0` → HOLD** (no twitch — this removed the old leg-M5 hair-trigger that churned thin commodity legs).

**DCA ladder (one side open) — max 6 positions.** Exits are checked first (close beats add); the add
is `open_score`-gated (above). Tiers read the **active leg's OWN RSI**, and **both CE and PE average
in on the leg's dips** (oversold):

| Position count | Add trigger (CE **and** PE) | Action |
|---|---|---|
| profit (first) | hedging ₹ target met (§1.5) | `CLOSE_*` (reason `target`) |
| force-close (next) | `fc_score ≤ −FORCE_CLOSE_S` | `CLOSE_*` (reason `force_close`) |
| 1 | leg RSI **M1** ≤ 35 | `*_MORE` |
| 2 | leg RSI **M5** ≤ 35 | `*_MORE` |
| 3 | leg RSI **M15** ≤ 35 | `*_MORE` |
| 4 | leg RSI **H1** ≤ 35 | `*_MORE` |
| 5 | leg RSI **H4** ≤ 35 | `*_MORE` |

After 5 adds (6 positions) the basket is held until the target or force-close. No H6 final-close.

### 1.4 Fibonacci volume ladder
Positions follow the fib sequence `[1,2,3,5,8,13,…] × base`: **entry** = `1 × base`, **adds** =
`2,3,5,8,13 × base` (`FIB[mg+1] × units × lot` in `symbol_worker._add_leg`). `base` scales with
the running **unit** (§3).

### 1.5 Profit booking — **variant B (fixed ₹ target)**
Close the whole basket the moment it clears the tax-aware target:

```
cycle_pl > close_threshold          → CLOSE_* (reason 'target')
close_threshold = HEDGE_PER_UNIT × units + estimated charges   (so it nets ≥ target × units)
```

- The per-unit ₹ target starts at **₹100** (`hedge_per_unit` in `symbols.json`, which overrides
  `constants.HEDGE_PER_UNIT`) and is then **auto-tuned daily** (§6) within **[₹100, ₹1000]**.
- Books before the force-close; same for CE and PE (P&L is long-only).

> **Variant A (`fyers_netting_*`)** books on a **downward M5 reversal** (leg M5 → index M5·pol)
> **above** the same ₹ floor, so winners run until momentum turns. It is **not** auto-tuned
> (its `hedge_per_unit` is a floor, not a target). That is the only difference between the pairs.

---

## 2. Hedging → Netting adaptation (buy-only)

| Strategy signal | Fyers action |
|---|---|
| `BUY` / `BUY_MORE` / `CLOSE_BUY` | buy / add-average / sell the **CE** leg |
| `SELL` / `SELL_MORE` / `CLOSE_SELL` | buy / add-average / sell the **PE** leg |

Per-cycle P&L is a self-tracked cost basis (carry-safe): `cycle_pl = ltp × net_qty − cost_basis`
(`position_tracker.py`) — long-only, so CE and PE are symmetric. A close books `net = gross −
charges` into `profit_history.json`, and the close log records **why**: `CLOSE_SENT` / `CLOSE_BOOKED
… reason=target|book|force_close`.

---

## 3. Position sizing — balance-tiered units
`units = min(floor(balance / STEP_UP_BALANCE) + 1, UNIT_MAX_LIMIT)` with `STEP_UP_BALANCE =
₹100,000`, `UNIT_MAX_LIMIT = 10` (per-symbol override in `symbols.json`). Units are fixed at entry
and drive both the Fibo lot ladder and the `close_threshold`. The per-contract lot comes from the
downloaded NSE/MCX CSV.

**Strike / expiry scan (`scanner.py`).** Best CE + PE within a per-lot **premium band**, a
**round-strike** bonus, and an **expiry window** (`SCAN_*`); commodities set `min_premium_per_lot: 0`
until tuned. Month-end monthlies are preferred so the option legs have enough M15/H1/H4 history.

---

## 4. Charges / taxes (tax-aware, exchange-aware)
`estimate_trade_charges(qty, price, num_orders, is_commodity=)` (`constants.py`, verified against
fyers.in/charges-list): brokerage (₹20, ₹15 Prime), **NSE STT 0.15% sell** *or* **MCX CTT 0.05%**
for commodities, exchange txn (NSE vs MCX rate), SEBI ₹10/cr, stamp 0.003% buy, GST 18%. It feeds
both the ₹ target and the booked `net_pl`.

---

## 5. Data flow per tick (`symbol_worker.tick`)
1. Resolve underlying (commodity future from MCX CSV; index as-is) + trading-window guard.
2. Close-first: if a close is pending, confirm it (start the cooldown) before anything else.
3. **`_maybe_auto_tune()`** — once/day, ~5 min before close, hill-climb the ₹ target (§6).
4. `_leg_state_and_threshold()` → leg `buy_pos`/`sell_pos` from cost basis + `close_threshold`.
5. `_slopes_and_rsi()` (index, all TFs) + `_leg_slopes()` / `_candidate_leg_slopes()` — one fetch
   per (symbol, TF), **cached per bar** so the DATA budget is respected.
6. `calculate_signal(...)` → BUY/SELL/ADD/CLOSE + analysis; `_execute()` maps it to the CE/PE leg;
   the dashboard payload is written (Index/CE/PE slope arrows, entry direction, RSI, pair, position).

---

## 6. Auto-tuner — daily hill-climb of the ₹ target (HEDGING only)
`auto_tune.py` "trains" `hedge_per_unit` per symbol. Once a day, ~5 min before that asset's close
(NIFTY 15:25, commodities 23:25), it reads the day's closed trades from `profit_history.json`,
computes **win-ratio** + **total net ₹**, and compares to the previous recorded day:

- both metrics **non-worse & ≥1 better** → **+₹100**; both **non-better & ≥1 worse** → **−₹100**; else **hold**.
- Guards: needs **≥3 closes** that day to move; clamp to **[₹100 floor, ₹1000 cap]**; once/day.

`≥/≤` (not strict) is used so a saturated win-ratio still lets rising net ₹ push the target up. The
new amount is **auto-applied** (the worker loads it on start and after each tune) and persisted to
`auto_tune.json`; the `symbols.json` value stays the base/floor. The dashboard draws a per-symbol
**bottom chart**: daily target step line, markers coloured by step (green +₹100 / red −₹100 / grey
hold), and a dashed **best-fit average line** (the converged "best amount"). Netting is **not** tuned.
