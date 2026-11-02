# TRADING_LOGIC — `fyers_live`

> **Status:** 🟡 Spec (implementation pending confirmation)
> **Source of truth:** Alcadeias MT5 bot, branch `dev_btcusd1_v2`
> (`C:/Users/sings213/Alcadeias/Alcadeias`) — the **HAM = Heiken-Ashi
> Martingale** strategy, to be reproduced **100% exact**, adapted from MT5
> **hedging** to Fyers **netting** (buy-only, clubbed & averaged).
> **Cross-ref:** existing Fyers port on `dev_trading` + `FYERS_PNL_LOGIC.md`.
> **Last updated:** 2026-11-02

This documents *what* the strategy does. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the engine it plugs into. Every change is logged in [CHANGELOG.md](CHANGELOG.md).

---

## 1. The HAM strategy (exactly as Alcadeias)

A mean-reversion **martingale/DCA ladder** driven by Smoothed-Heiken-Ashi
direction and multi-timeframe RSI extremes.

### 1.1 Indicators (`indicator.py`, reproduced verbatim)
| Indicator | Config (Alcadeias) | Purpose |
|---|---|---|
| **Signal SHA** | SHA-v3, `TEMA`, length **10**, on **M5** candles | direction (bull/bear) |
| **Trend SHA** | SHA-v3, `DEMA`, length **20**, on **M5** candles | trend confirm |
| **RSI** | `RMA`, length **14**, multi-timeframe | entry filter + DCA gating |
| **ADX/ATR** (regime) | length 14, M5 | optional fresh-entry gate (OFF by default) |

SHA-v3 = pre-smooth OHLC → Heiken-Ashi → post-smooth (same length/MA). TV-compatible
MAs (SMA-seeded EMA/RMA; DEMA/TEMA chains). **Port the `Indicator` class 1:1.**

### 1.2 Signals
`DO_NOTHING, BUY, SELL, CLOSE_BUY, CLOSE_SELL, BUY_MORE, SELL_MORE`.

**Entry (only when FLAT — both directions have 0 positions):**
- `BUY`  if signal-SHA[0] bullish **and** trend-SHA[0] bullish, **and** no MTF-RSI block, (and regime allows).
- `SELL` if signal-SHA[0] bearish **and** trend-SHA[0] bearish, **and** no MTF-RSI block.
- **MTF-RSI entry filter:** block BUY if **any** of M1/M5/M15/M30 RSI ≤ 35; block SELL if **any** ≥ 65.

**Ladder / DCA (one direction open) — max 6 positions:**
| Position count | Add trigger (BUY side) | Action |
|---|---|---|
| close check (any) | `basket_profit > close_threshold` | `CLOSE_BUY` |
| 1 | RSI **M1** ≤ 35 | `BUY_MORE` |
| 2 | RSI **M5** ≤ 35 | `BUY_MORE` |
| 3 | RSI **M15** ≤ 35 | `BUY_MORE` |
| 4 | RSI **H1** ≤ 35 | `BUY_MORE` |
| 5 | RSI **H4** ≤ 35 | `BUY_MORE` |
| 6 | RSI **H6** ≤ 35 | `CLOSE_BUY` (final forced close) |

SELL side is the mirror (RSI ≥ 65). Adds are **RSI-gated, not loss-gated**
(matches Alcadeias; the `fibo²×hedge` loss threshold is unused dead code in both).

### 1.3 Fibonacci volume ladder
Positions are opened as the fib sequence `[1,2,3,5,8,13,…]` × base:
- **Entry** = `1 × base`; **adds** = `2, 3, 5, 8, 13 × base` (fib[1..5]).
- `base` scales with the running **unit** (§3). Cumulative after 6 = `32 × base`.

### 1.4 Profit booking (close target)
Close the whole basket when `basket_profit > close_threshold`.
- **Alcadeias:** `close_threshold = units` → **$units** (i.e. +$1 per unit, "+1 USD per 0.01 lot").
- **Fyers (tax-aware):** `close_threshold = ₹(HEDGE_PER_UNIT × units) + charges`, with
  **`HEDGE_PER_UNIT = 100`** → +₹100 per unit, **plus** estimated taxes/charges (§4),
  because Fyers deducts STT/GST/etc. that MT5 forex does not.

---

## 2. Hedging → Netting adaptation (buy-only, clubbed & averaged)

MT5 holds many discrete hedged tickets; Fyers **nets** everything into one
averaged position per instrument. We only ever **BUY options**.

| Alcadeias (hedging) | Fyers (netting) |
|---|---|
| `BUY` basket (bullish) | **BUY CE** leg |
| `SELL` basket (bearish) | **BUY PE** leg |
| `BUY_MORE` / `SELL_MORE` (new ticket) | **add qty to the same leg** → Fyers averages `buyAvg` |
| `CLOSE_BUY` / `CLOSE_SELL` (close all tickets) | **SELL to close** the CE / PE leg |
| per-ticket `profit`, `count` | reconstructed from `martingale_count` + `effective_pl` (netting hides tickets) |

- **Direction source:** index SHA decides the leg — bullish → trade **CE**, bearish → **PE**.
  Entry requires both legs flat, so **only one leg is active at a time** (exactly mirrors
  Alcadeias opening a fresh basket only when flat).
- **Ladder level** can't be counted from Fyers positions (they're merged), so it's
  tracked in `position_tracker.json` as `martingale_count` (as the existing port does).
- **Per-cycle P&L (netting):** Fyers blends averages across re-entries, so use
  **`effective_pl = api_total_pl − booked_profit`** (see `FYERS_PNL_LOGIC.md`). `booked_profit`
  is snapshotted on each **confirmed** close (`netQty == 0`).

### 2.1 Signal → Fyers order mapping
| Signal | Fyers action |
|---|---|
| `BUY` (CE entry) | `fyers.buy(CE_sym, entry_qty)` → `tracker.record_entry` |
| `SELL` (PE entry) | `fyers.buy(PE_sym, entry_qty)` → `record_entry` |
| `BUY_MORE` / `SELL_MORE` | `fyers.buy(leg_sym, fib_add_qty)` → `record_martingale` |
| `CLOSE_BUY` / `CLOSE_SELL` | `fyers.sell(leg_sym, net_qty)` → `mark_pending_close`; on `netQty==0` → `confirm_close` (snapshot `booked_profit`) |

Product type = **MARGIN/NRML (carry)** — positions may be held overnight (§5).

---

## 3. Position sizing — balance-tiered units (NEW vs dev_trading)

`units` scales both lot size **and** the ₹ profit target together, from the live
balance. **Re-derived only when the leg is FLAT** (an open basket keeps its unit).

```
lot_size   = CSV "Minimum lot size" for the leg symbol   (e.g. NIFTY = 65)
units      = min( floor(balance / STEP_UP_BALANCE) + 1, UNIT_MAX_LIMIT )  # Alcadeias (O1 ✓)
entry_qty  = units × lot_size
add_qty(L) = fib[L] × units × lot_size                     # fib = 1,2,3,5,8,13
target     = HEDGE_PER_UNIT × units + charges(qty, ltp)    # HEDGE_PER_UNIT = ₹100
```

Worked example (NIFTY, `lot_size = 65`, `STEP_UP_BALANCE = 50,000`, floor+1):
| Balance | units | entry_qty | profit target |
|---|---|---|---|
| < ₹50,000 | 1 | 65 | **₹100 + taxes** |
| ₹50,000 – <₹1L | 2 | 130 | **₹200 + taxes** |
| ₹1,00,000 – <₹1.5L | 3 | 195 | ₹300 + taxes |

Config (per symbol, in `symbols.json`): `step_up_balance`, `unit_max_limit`,
`lot_size` auto-read from the cached NSE CSV (not hard-coded).

---

## 4. Charges / taxes (exact — from dev_trading `constants.py`)

```python
def estimate_trade_charges(qty, ltp, num_orders=2):
    turnover  = abs(qty) * ltp
    brokerage = 20.0 * num_orders                   # ₹20 flat per order
    stt       = 0.000625 * turnover                 # 0.0625% (sell-side options)
    exchange  = 0.000495 * turnover * 2             # ~0.0495% both sides
    sebi      = (turnover * 2 / 1_00_00_000) * 10.0 # ₹10 per crore, both sides
    gst       = 0.18 * (brokerage + exchange + sebi)# 18%
    stamp     = 0.00003 * turnover                  # 0.003% buy-side
    return round(brokerage + stt + exchange + gst + stamp + sebi, 2)
```
`num_orders = 2 + martingale_count` (entry + exit + each add). This is the "taxes"
added to the ₹100×units target so **net** booked profit ≥ ₹100×units.

---

## 5. Carry / delivery handling (your point 8) — ✅ handled

Positions are **carried** (held overnight, product = MARGIN/NRML). To stay correct
across the day boundary, `position_tracker.py` uses a **self-tracked cost-basis**
model instead of Fyers' intraday `pl − booked_profit`:

    cycle_pl = ltp × net_qty − cost_basis       # open-basket P&L, day-independent
    realized = exit_price × net_qty − cost_basis

`cost_basis`/`net_qty` are built from our own fills (fill price from the **tradebook**,
fallback ltp) and are **preserved across day-change for any open leg** (only fully-closed
legs reset). This is immune to the midnight reset and to Fyers' blended-average
contamination after same-day re-entry. On restart, a carried position is adopted and its
cost basis seeded from the broker avg.

The worker's "scan once/day if flat; else manage/close first" rule (ARCHITECTURE §5)
means a carried position is **managed to close before any new scan**.

⚠️ Fill-price accuracy (tradebook) is the one item to confirm during the live dry-run.

---

## 6. Dashboard — P/L booking graph (your request)

Reproduce the **Alcadeias MT5 chart** (`_alc_ref/dashboard.py:_build_daily_chart`),
**per symbol/asset**, from `profit_history.json` (filter `action == "CLOSE"`):
- **Row 1 — bar per booking:** one bar per closed basket; **green if ≥0, red if <0**;
  X = close time, Y = `effective_pl` (net booked ₹). *This is the "every profit/loss
  booking" graph you asked for.*
- **Row 2 — cumulative spline + drawdown:** running sum (area fill) + gold **peak** line
  + red **drawdown** fill (peak − cumulative).
- **Per-asset:** one chart per symbol (NIFTY / GOLD / SILVER), selectable.
- Plus the **API-usage meter** panel (ARCHITECTURE §6). All read-only from state files.

---

## 7. Divergences from the existing `dev_trading` port (to reconcile for "100% exact")

| Aspect | dev_trading (now) | Alcadeias (target) | Plan |
|---|---|---|---|
| Ladder depth | 3 adds (M1/M5/M15) | 5 adds (M1/M5/M15/H1/H4) + **H6 forced close** | **adopt Alcadeias (6 levels)** |
| Entry gates | SHA + **GAP% + relationship DIVERGING** + RSI | SHA + MTF-RSI filter (+ regime off) | **drop GAP%/relationship** (not in Alcadeias) |
| Signal source | per-leg CE/PE/idx (`signal_state.json`) | one instrument's SHA+RSI | **compute on the index** (⚠️ confirm §8 O2) |
| Position sizing | `qty` from `option_pairs.json` (no tiers) | `units` from balance | **add balance-tier units** |
| Close target | flat `hedge(500) + charges` | `units × target + charges` | **₹100 × units + charges** |
| Day reset / carry | wipes `booked_profit` | 24/7, carries | **self-tracked cost basis**, preserved across midnight ✓ (§5) |
| H6 RSI timeframe | n/a | 360-min | **resampled from 60-min** (360 not native) ✓ |

---

## 8. Open decisions (confirm before implementation)

- **O1 — units formula.** ✅ RESOLVED (2026-11-02): use Alcadeias
  `units = min(floor(balance/50000)+1, UNIT_MAX_LIMIT)`. Tiers: `<50K→1` (65qty/₹100),
  `50K–1L→2` (130qty/₹200), `1L–1.5L→3` (195qty/₹300), …
- **O2 — signal source.** ✅ RESOLVED (2026-11-02): compute SHA + MTF-RSI on the
  **index** (`NSE:NIFTY50-INDEX`); bullish→CE, bearish→PE. Commodity uses its own index.
- **O3 — ladder depth 6** (5 RSI adds M1/M5/M15/H1/H4 + H6 forced close): taken as
  decided per "100% exact Alcadeias".
- **O4 — commodity timings / symbols** still pending (ARCHITECTURE O1–O3).
