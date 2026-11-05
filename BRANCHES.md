# BRANCHES — Ballom_Fyers system & branch map

> **Read this first.** It is the self-contained map of the whole bot: every branch,
> every module, how to run it, and how the strategies differ — written so the system
> can be understood and maintained **without any chat/AI history**. This same file is
> committed on all six branches. Per-branch strategy detail is in
> [TRADING_LOGIC.md](TRADING_LOGIC.md); engine internals in
> [ARCHITECTURE.md](ARCHITECTURE.md); change history in [CHANGELOG.md](CHANGELOG.md);
> the live go-live checklist in [LIVE_DRYRUN.md](LIVE_DRYRUN.md).
> **Last updated:** 2026-11-04

---

## 1. What this project is

`Ballom_Fyers` is an automated options-trading bot for the Indian market via the
**Fyers** broker (API v3). It runs one strategy across NIFTY index options and
(optionally) MCX commodity-future options, sizing positions by account balance,
averaging into a basket on a Fibonacci DCA ladder, and booking profit tax-aware.
A separate Dash web dashboard shows live state.

Two things are **broker facts** you cannot change in code:
- **Fyers is a netting, buy-only** options account — you cannot short an option and
  you cannot hold hedged long+short of the same contract. Every "sell" signal is
  therefore expressed as **buying a PE** (put) leg, every "buy" as **buying a CE**
  (call) leg. The two legs are independent, each clubbed & averaged.
- **Fyers deducts real taxes/charges** (STT, exchange, SEBI, stamp, GST, brokerage)
  that a forex/MT5 account does not, so every profit target is **tax-aware**.

---

## 2. The six branches

| Branch | Mode | Strategy | Profit exit | Symbols (enabled) |
|---|---|---|---|---|
| **fyers_live** | 🔴 LIVE (real money) | HAM (SHA + MTF-RSI) | ₹300×units NET + charges target | NIFTY |
| **fyers_demo** | 🟢 PAPER (simulated) | HAM (SHA + MTF-RSI) | ₹300×units NET + charges target | NIFTY, GOLDM, SILVERM |
| **fyers_netting_live** | 🔴 LIVE | RSI+MACD slope (variant A) | leg **M5 both-reverse** above the ₹300 floor | NIFTY |
| **fyers_netting_demo** | 🟢 PAPER | RSI+MACD slope (variant A) | leg M5 both-reverse above the ₹300 floor | NIFTY, GOLDM, SILVERM |
| **fyers_hedging_live** | 🔴 LIVE | RSI+MACD slope (variant B) | **₹300×units NET + charges target** | NIFTY |
| **fyers_hedging_demo** | 🟢 PAPER | RSI+MACD slope (variant B) | ₹300×units NET + charges target | NIFTY, GOLDM, SILVERM |

**Lineage:** `fyers_live` and `fyers_demo` are the two base branches. The four
`*_live` / `*_demo` RSI+MACD branches were cut from those bases (live-from-live,
demo-from-demo) so each keeps its base's mode plumbing and symbol set.

**LIVE vs PAPER** is the *only* thing that differs between a `*_live` and its
`*_demo` sibling:
- **LIVE** places **real orders** through `fyers_api.py`.
- **PAPER** swaps in `demo_broker.py` (`DemoFyersAPI`): auth, quotes, option-chain and
  historical data are **real API calls** (so real API throttling + real signals are
  exercised), but **open/close orders are simulated on local disk** against a
  simulated balance (`DEMO_START_BALANCE = ₹90,000`). No real money moves.

**HAM vs RSI+MACD** is the strategy difference (see §5).

### Which branch should I run?
- **Testing / tuning:** a `*_demo` branch (no money at risk, real data & throttling).
- **Real trading:** the matching `*_live` branch, only after a dry run
  (see LIVE_DRYRUN.md).
- **Prove a strategy in paper first, then run the identical `*_live` sibling** — the
  trading code is identical; only the broker + symbol set change.

---

## 3. Module map (`*.py`)

| File | Role |
|---|---|
| `application.py` | **Loop-1 supervisor.** Boots auth, loads config, detects API plan, spawns one `SymbolWorker` thread per enabled symbol, runs daily maintenance, writes account state. Main entry point. |
| `symbol_worker.py` | **Loop-2 worker (one thread per symbol).** Each tick: resolve underlying, fetch indicators, call the strategy, execute CE/PE leg orders, write the dashboard payload. |
| `strategy.py` | **The signal brain.** `calculate_signal(...)` returns BUY / SELL / BUY_MORE / SELL_MORE / CLOSE_BUY / CLOSE_SELL / DO_NOTHING. This file is what differs between HAM and the two RSI+MACD variants. |
| `indicator.py` | Pure indicator math (TradingView-compatible): RSI (RMA), MACD (`ta.macd`), SHA-v3 (HAM only), ADX, and `latest_pivot_slope` (RSI+MACD only). |
| `fyers_api.py` | Fyers REST wrapper: history, quotes, option-chain, funds, positions, buy/sell, lot-size + commodity-future resolution from the MCX CSV. |
| `fyers_auth.py` | TOTP login (vagator endpoints) → cached access token reused across calls. **Credentials are hardcoded here** (see §9). |
| `api_meter.py` | Thread-safe local counter + **throttle** against the plan's per-sec/min/day limits, split by category DATA / ORDER / USERINFO. Prevents Fyers rate-limit bans. |
| `scanner.py` | Scans the option chain for the best CE/PE pair within the premium cap (`SCAN_MAX_PREMIUM_PER_LOT = ₹30,000`). |
| `position_tracker.py` | **Cost-basis P&L** (carry-safe): `cycle_pl = ltp×net_qty − cost_basis`; records entry/add/close with gross/charges/net into `profit_history.json`. |
| `constants.py` | **All config in one place** — paths, market hours (IST), API limits, tax rates, strategy params, position sizing, demo settings. Edit here to tune. |
| `dashboard.py` | Separate-process Dash web app (port 8050) reading the JSON state files. |
| `state_writer.py` | Writes the per-symbol signal state + strategy event log the dashboard reads. |
| `demo_broker.py` | **(demo branches only)** `DemoBroker` disk store + `DemoFyersAPI` that simulates orders while keeping real read APIs. |
| `demo_cycle.py` | **(demo branches only)** helper to drive/inspect a simulated trade cycle. |
| `dry_run.py` | One-shot **shadow** run: real auth + data + signals, prints what it *would* do, **places no orders**. Run before going live. |

State files (JSON, written at runtime, not in git): account state, API usage,
per-symbol signal state, `profit_history.json`, strategy logs — under the state dir
configured in `constants.py`.

---

## 4. Engine architecture (two loops)

```
application.py  (Loop-1 supervisor, main thread)
   ├─ auth (fyers_auth)  → cached token
   ├─ load symbols.json + detect API plan (standard/prime)
   ├─ spawn SymbolWorker thread per enabled symbol  ───┐
   ├─ daily maintenance (token refresh, roll-over)     │
   └─ write account_state.json for the dashboard       │
                                                        ▼
symbol_worker.py  (Loop-2, one thread per symbol, every WORKER_IDLE_SLEEP)
   1. resolve underlying (commodity future from MCX CSV; index as-is) + window guard
   2. if a close is pending → confirm it first (close-first priority)
   3. read leg state + compute the tax-aware close_threshold (cost basis)
   4. fetch indicators (per-bar cached to respect the API budget)
   5. strategy.calculate_signal(...) → signal + analysis
   6. execute: BUY/BUY_MORE/CLOSE_BUY → CE leg; SELL/SELL_MORE/CLOSE_SELL → PE leg
   7. write the per-symbol dashboard payload

dashboard.py  (separate process) — reads the JSON state files, renders live.
```

Every history/quote call goes through `api_meter` first, which blocks/paces calls so
the Fyers per-minute limit is never breached (a repeated breach = a full-day ban).

---

## 5. The strategies

### 5.1 Shared mechanics (all branches)
- **Netting legs:** BUY→CE, SELL→PE; each leg clubbed & averaged.
- **Balance-tiered units:** `units = min(floor(balance / ₹100,000) + 1, 10)`. Units are
  fixed at entry and scale both the lot ladder and the profit target.
- **Fibonacci DCA ladder (max 6 positions):** entry = `1×units×lot`; adds =
  `2,3,5,8,13 × units × lot`, each added when the tier's RSI hits its extreme
  (count 1→M1, 2→M5, 3→M15, 4→H1, 5→H4; BUY at RSI≤35, SELL at RSI≥65).
- **Tax-aware target:** `close_threshold = ₹(300 × units NET) + estimated charges`. The
  **₹300×units is what you keep after taxes** — the estimated charges are stacked *on top*, so
  taxes never eat into your target (booking is on gross `cycle_pl`; net ≥ ₹300×units).

### 5.2 HAM — `fyers_live` / `fyers_demo`
Mean-reversion driven by **Smoothed Heiken-Ashi (SHA-v3)** direction + a
**multi-timeframe RSI** entry filter:
- **Entry (flat):** signal-SHA (TEMA-10) and trend-SHA (DEMA-20) both bullish → BUY
  (both bearish → SELL), unless any of M1/M5/M15/M30 RSI is in the opposite extreme.
- **Ladder:** the shared RSI ladder above; a 6th tier (H6 RSI) force-closes.
- **Exit:** basket `profit > close_threshold` → CLOSE (the ₹300×units NET + charges target).

### 5.3 RSI+MACD slope — the four `*_netting_*` / `*_hedging_*` branches
**SHA is removed.** Direction comes from the **latest-pivot slopes** of RSI and the
MACD histogram, combined into per-section weighted scores:

```
score = Σ_tf  weight_tf · (rsi_sign_tf + macd_sign_tf)      each sign ∈ {−1, 0, +1}
```

**Two data sources (important):** the OPEN score (entry + the ladder-add gate + the entry veto)
reads the **main index** (e.g. NIFTY-INDEX); the **BOOK + FORCE-CLOSE exits read the ACTIVE CE/PE
leg's OWN option candles**, so exits follow the premium you actually hold, not the index.

| Section | Weights | Threshold | Source | Drives |
|---|---|---|---|---|
| OPEN | H4:1, M5:1 (±4) | `ENTRY_S=2`, `LADDER_S=2` | **index** | entry + ladder-add gate |
| BOOK | M5:1 (±2) | `BOOK_S=2` | **leg** | profit book (variant A) — **both** M5 slopes reverse |
| FORCE_CLOSE | M15:1 (±2) | `FORCE_CLOSE_S=1` | **leg** (exit) / **index** (entry gate) | force-close — **either** M15 slope reverses |

- **Entry (flat), agreement gate:** BUY when the index `open_score ≥ +2` **and** the index
  force-close score isn't opposing; SELL mirror; else WAIT. Blocked for **300 s after any close**
  (`REENTRY_COOLDOWN_SECONDS`).
- **Anti-vibration:** after an entry, BOTH book + force-close are suppressed for **180 s**
  (`MIN_HOLD_SECONDS`) so a basket can never open-then-close on a stale leg pivot.
- **Force-close:** the leg's M15 `fc_score ≤ −1` (either the RSI or MACD slope reversing) closes it.
- **Ladder:** the shared RSI ladder, gated on the index OPEN score.
- **Profit book — the ONLY difference between the two variants:**
  - **Variant A = NETTING** (`fyers_netting_*`, from Alcadeias `rsimacd_btcusd1_wd_exp_h`): book
    when `cycle_pl > close_threshold` (net ≥ ₹300×units) **AND both** the leg's M5 slopes reverse.
    Winners run until M5 momentum turns; never books a net loss.
  - **Variant B = HEDGING** (`fyers_hedging_*`, from Alcadeias `rsimacd_h6_btcusd1_wd_exp_h`):
    book the moment `cycle_pl > close_threshold` (the fixed ₹300×units+tax target). Force-close
    still reads the leg.

> **Names:** "netting"/"hedging" label the *profit-exit flavour* from the two Alcadeias source
> branches — **both** run on the same Fyers netting account (buy-only CE/PE). Full detail per
> branch is in its `TRADING_LOGIC.md`.

---

## 6. Position sizing & taxes
- **Units:** `min(floor(balance / STEP_UP_BALANCE) + 1, UNIT_MAX_LIMIT)` with
  `STEP_UP_BALANCE = ₹100,000`, `UNIT_MAX_LIMIT = 10`. <1L→1 lot, 1L–2L→2, …
- **Charges** (`estimate_trade_charges`, verified vs fyers.in/charges-list 2026-11-03):
  brokerage ₹20/order (₹15 with Prime), STT 0.15% on sell premium, exchange txn
  0.0355299% (NSE), SEBI ₹10/cr, stamp 0.003% buy, GST 18%. (~₹75.7 round-trip on
  65 × ₹185.)

---

## 7. API throttling (never breach)
Plan is **not** exposed by the Fyers API — it is set from config
(`symbols.json "api_plan"`: `standard` | `prime` | `auto`).

| Category | Standard (sec / min / day) | Prime (sec / min / day) |
|---|---|---|
| DATA | 5 / 50 / 5,000 | 10 / 500 / 500,000 |
| ORDER | 10 / 200 / 100,000 | 10 / 500 / 500,000 |
| USERINFO | 10 / 200 / 100,000 | 10 / 500 / 500,000 |

`api_meter` paces calls to stay under these. Indicator fetches are **cached per bar**
(e.g. a 5-min timeframe is re-fetched at most once / 5 min) so multi-timeframe slopes
stay affordable. Prime also lowers brokerage ₹20→₹15 (taxes unchanged) and the
dashboard shows a plan badge + renewal countdown (renewal date is config, not API).

**Current:** `api_plan: "prime"` on all branches (Fyers Prime bought 2026-11-04, annual →
`prime_renewal: "2027-11-04"`). Fyers exposes **no** account-type / subscription field in any API
(`get_profile` returns identity + DDPI/MTF flags only) — so Prime is, and must be, config-driven.

**Rate-limit detection + dashboard.** `api_meter` records every Fyers **-429** (per category: `rate_limited` + live `cooldown_s`, a `limited_today` count) and the running `minute_breaches`, and publishes them in `api_usage.json`. The dashboard's bottom **“API RATE LIMITS” panel** shows each category's sec / min / day usage vs the active plan's limit (amber ≥60 %, red ≥85 %) with a day bar, and flags **⛔ RATE-LIMITED · Ns** during a back-off or “N× limited today” after one — so a limit is visible, not silent. A single engine on **Prime** (500 DATA/min) won't hit this with per-bar caching; a -429 here means **another process is sharing the same Fyers account** (the per-account limit is global, but each process's `api_meter` only counts its own calls) **or** a strategy that books / re-enters very often (more scanner + option-leg fetches). Hedging books on a ₹ target so it cycles more than netting (which also needs both slopes to reverse) — hence it can surface the warning first.

---

## 8. Operational & tuning notes (data fetch, loop cadence)

**Candle counts (`constants.py`).** `RSI_CANDLE_COUNT = 120`, `SHA_CANDLE_COUNT = 120`. Short on
purpose: RMA-14 RSI, MACD's EMA-26 and SHA's DEMA-20/TEMA-10 all converge well within 120 bars, and
the latest-pivot slope only reads the last ~20 bars — so 120 is ample at ~4× less payload than the
old 500/300. **Don't drop to ~50:** the slow smoothers (MACD EMA-26, SHA DEMA-20) aren't converged
by then, which skews the slope the OPEN score depends on.

**Fyers history is capped at 100 days per request** for intraday resolutions (1m–240m; 366 for
daily, 30 trading-days for seconds). Walking back 500 H4 bars used to span ~200 days (MCX) / ~440
(index) and the API rejected it ("H4 failing"). `fetch_historical_data` now **clamps the window**
to just under the cap (95d intraday / 360d daily / 28d seconds); the 120-candle count also keeps H4
naturally under it. For spans longer than 100 days Fyers' own answer is paginated 100-day windows —
not needed here.

**History walk-back uses the right market close per asset:** **index 15:30** (3:30 PM NSE),
**commodity 23:30** (11:30 PM MCX; 23:55 in summer DST — minor). Picked by `market_type`.

**History fetch is IST-aware** — `fetch_historical_data` uses `now_ist()` and tags every datetime
`tzinfo=IST`, so `range_from`/`range_to` are correct UTC epochs on **any** server timezone. (Before,
naive `datetime.now()` + naive `.timestamp()` shifted the window by the 5h30m IST offset on a UTC
host, dropping candles → "insufficient candles", worst in the morning.) The worker also passes
**NSE holidays + special sessions** to the fetch so the day-walk skips them (no under-delivery after
a holiday). If a fetch still comes back short, the strategy just degrades that timeframe's slope to
neutral rather than trading on bad data.

**Worker loop = ONE loop per symbol, not two.** Each worker thread does *fetch + trade* (~10 s of
work on a bar boundary, ~1-2 s when cached) then idles `WORKER_IDLE_SLEEP = 15 s` → a ~15-25 s
cycle. The timing print reads `[NIFTY] fetch+trade=.. / cycle=..` (one loop; cycle = work + idle).
The **second, genuinely independent loop is the supervisor thread** (`application.py`: account
snapshot, maintenance, plan). A short idle makes the **LTP-based profit-book react in ~15-25 s**
(vs ~70 s before); entry/slope signals are bar-based so they're unaffected. Tunable via
`WORKER_IDLE_SLEEP`.

**Off-hours = no wasted API.** When every enabled market is closed, the per-symbol worker returns
before any API call (its trading-window guard), and the supervisor **skips the balance/positions
read poll** (nothing moves while shut) — reusing the last snapshot and refreshing only the plan
badge / renewal countdown (no API) + a `market_open` flag. So outside market hours the *only* API
calls are the **auth keep-alive** (token verify / 5 min) and the **~00:10 daily reauth** — the
important ones. Markets are "open" here if any enabled symbol's window is live (index to 15:30,
commodities to 23:30).

---

## 9. Environment & how to run
- **Python 3.11 only** (hardcoded server path `C:\Users\Administrator\AppData\Local\
  Programs\Python\Python311`). 3.13 fails to build a dependency (`aiohttp`, no MSVC).
- **Timezone:** all market logic is **IST** (`now_ist` / `today_ist` in constants).
- **Auth** is cached: the TOTP login runs once and the access token is reused across
  API calls (no re-login per request).

Run (from the repo root, on the desired branch):
```bash
# 1. (first time on live) shadow dry-run — places NO orders:
python dry_run.py

# 2. start the engine (live places real orders; demo simulates):
python application.py

# 3. in a second terminal, start the dashboard:
python dashboard.py            # → http://127.0.0.1:8050  (default port 8050)
```
Switch branches with `git checkout <branch>` before running — the branch decides
LIVE vs PAPER and HAM vs RSI+MACD.

---

## 10. Safety notes
- **`*_live` branches place real orders.** Always paper-test on the `*_demo` sibling
  and run `dry_run.py` before a live session.
- **Credentials (TOTP secret, PIN, API key/secret) are hardcoded in `fyers_auth.py`**
  by the owner's explicit choice. Keep the repo private. Rotate them if it ever
  becomes public.
- **Never breach the per-minute API limit** — repeated breaches get the account
  rate-limit-banned for the day. The meter guards this; don't bypass it.
- The engine **adopts a carried position on startup** (seeds cost basis from the
  broker average), so a restart mid-trade continues the cycle rather than double-opening.

---

## 11. Git topology & keeping docs current
```
fyers_live ──┬─ fyers_netting_live ── (core → strategy A → doc)
             └─ fyers_hedging_live ── (core → strategy B → doc)
fyers_demo ──┬─ fyers_netting_demo ── (core → strategy A → doc)
             └─ fyers_hedging_demo ── (core → strategy B → doc)
```
The RSI+MACD "core" (indicator + constants + worker + dashboard) is **identical**
across the four RSI+MACD branches; **netting vs hedging differ only in
`strategy.py`**; **live vs demo differ only in the demo plumbing**
(`demo_broker.py`, `demo_cycle.py`, the `DEMO_*` constants, the dashboard `IS_DEMO`
flag, `symbols.json`).

**Maintenance rule:** every code change must land with (a) a detailed dated entry in
`CHANGELOG.md` and (b) an update to the relevant doc (`TRADING_LOGIC.md` for strategy,
`ARCHITECTURE.md` for the engine, this file for the branch map) — so the system stays
understandable from the repo alone.
