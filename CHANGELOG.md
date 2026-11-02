# CHANGELOG — `fyers_live`

Running log of every change on this branch (per owner's standing request —
record activity even when not explicitly asked). Newest first.

Format: `YYYY-MM-DD` — change — _why_.

---

## 2026-11-02 — Trading Increment 4: live hardening (done & verified)

- **Cost-basis P&L model** (`position_tracker.py`): replaced intraday
  `pl − booked_profit` with self-tracked `cycle_pl = ltp×net_qty − cost_basis`
  — carry/overnight-safe and immune to same-day re-entry blending. Cost basis is
  built from fills, preserved across day-change for open legs, and seeded from the
  broker avg when adopting a carried position.
- **Accurate fills**: added `fyers_api.tradebook()` + `last_fill_price()`; the worker
  records cost basis from the actual traded price (fallback ltp).
- **H6 RSI resample**: `symbol_worker._resample` builds 6-hour bars from 60-min
  (360 isn't a native Fyers resolution).
- **Per-bar RSI cache**: RSI per timeframe cached with TTL = bar duration (H1 hourly,
  H4 4-hourly, H6 6-hourly) → large cut to the DATA budget.
- **Configurable balance basis**: `BALANCE_SOURCE_TITLES` (Total → Available);
  `get_balance` tries them in priority order.
- **Verified** (mock API, Py3.11): cost-basis ENTRY/ADD/CLOSE (realized ₹1560), RSI
  cache (1 fetch then cached), H6 resample, get_balance priority; full engine imports.

### Remaining
- Improved option-pair scan (pt 7–8); user-gated live dry-run (confirm tradebook fill
  price); commodities (O4).

## 2026-11-02 — Committed + live dry-run checklist

- **Committed the `fyers_live` branch** (`3b8478d`) — full engine + trading + dashboard.
  Not pushed; no live orders placed.
- **Added `LIVE_DRYRUN.md`** — safety-first checklist for the first real-money
  validation (minimum size via `unit_max_limit:1`, kill switches, what to watch,
  known rough edges, stop criteria). Live run stays gated on the user.

## 2026-11-02 — Trading Increment 3: dashboard (done & verified)

- **Added `dashboard.py`** — trimmed read-only Dash app (separate process, D1/D2):
  engine-status strip, **API-usage meter** (DATA/ORDER/USERINFO vs Standard limits,
  colour-coded), **per-asset P/L booking chart** (bar-per-close green/red +
  cumulative/drawdown spline, replicating the Alcadeias daily chart) from
  `profit_history.json`, and the strategy log. CE+PE legs group under one asset.
- **Added `start_app.bat` (live-only, Py3.11) + `start_dashboard.bat`.**
- **Verified** (`_fyers_auth_test/verify_dashboard.py`): all panels render; the
  booking figure serializes; asset grouping maps NIFTY CE/PE → "NIFTY".
- Removed the stale `DEV_SCANNER.md` (described the replaced hourly design);
  `ARCHITECTURE.md` + `TRADING_LOGIC.md` are the current docs.

## 2026-11-02 — Trading Increment 2: execution layer (done & verified)

- **Added `position_tracker.py`** — netting booked-profit tracker (`effective_pl =
  pl − booked_profit`), **live-only + carry fix** (day-change preserves booked_profit
  for legs still open overnight; only closed legs reset). Thread-safe.
- **Extended `fyers_api.py`** — routed `safe_api_call` through `api_meter` (every call
  now counted + throttled), and added `buy/sell/positions/funds/get_position/get_balance`
  (orders metered as ORDER, account as USERINFO).
- **Rewrote `symbol_worker.py`** — full HAM execution on netting: index SHA+RSI →
  `calculate_signal` → BUY=CE / SELL=PE / adds / close; balance-tiered units;
  `fib×units×lot` adds; close at `100×units + charges`; carried-position recovery on
  startup; budget-aware selective RSI fetching.
- **Wired `scanner.py`** to the existing `fetch_option_pair`; added `PositionTracker`
  to the engine context in `application.py`.
- **Verified** (`_fyers_auth_test/verify_worker.py`, mock API): ENTRY→ADD→CLOSE produces
  correct qty ladder (130→+260), `martingale_count`, tax-aware threshold (₹291.10),
  booked profit (₹350) and `profit_history` rows. Full engine imports on 3.11.
- _No live orders placed — mock API only._

### Next (Increment 3 — dashboard)
- Trimmed Dash app: API-usage meter panel + **per-symbol P/L booking bar chart**
  (green/red per close) + cumulative/drawdown spline, from `profit_history.json`.
- Then: `.bat` launchers (live-only), and a careful live dry-run on a funded account.
- Open flags: H6 RSI resample (360-min not native), overnight `pl` reconciliation,
  per-bar RSI caching to shrink the DATA budget.

## 2026-11-02 — Trading Increment 1: indicator + strategy ported & verified

- **Resolved O1/O2:** units = `min(floor(balance/50000)+1, UNIT_MAX_LIMIT)` (Alcadeias
  exact); signals computed on the **index**. Updated `TRADING_LOGIC.md`.
- **Added `indicator.py`** — copied 1:1 from Alcadeias (SHA-v3, RSI, ADX). **Verified
  byte-identical output** vs the original on synthetic data (SHA TEMA10 / DEMA20 / RSI
  RMA14 all `identical: True`).
- **Added `strategy.py`** — HAM signal brain copied 1:1 (only tweak: dropped the
  vestigial `STRATEGY_HEDGE` import + header note). Fib ladder verified
  `[0.01,0.02,0.03,0.05,0.08,0.13]`; flat signal runs.
- **Extended `constants.py`** — SHA/RSI/MTF params, balance-tier constants
  (`STEP_UP_BALANCE`, `HEDGE_PER_UNIT=100`, `MAX_MARTINGALE_LEVEL=5`), and
  `estimate_trade_charges()` — **verified ₹69.15** for 65×₹185 (matches FYERS_PNL_LOGIC).
- **Updated `symbols.json`** — per-symbol `step_up_balance` + `unit_max_limit`.
- _Verified under Python 3.11 (`_fyers_auth_test/verify_strategy.py`)._

### Next (Increment 2 — execution layer)
- `position_tracker.py` (port + carry fix: don't wipe booked_profit for open legs).
- `fyers_api.py` order wrappers (buy/sell/positions/funds) **through `api_meter`**.
- `symbol_worker.py`: units-from-balance, build CE/PE leg dicts, index SHA+RSI,
  `calculate_signal` → CE/PE orders, `close_threshold = 100*units + charges`.
- `scanner.py`: pick CE/PE strikes for a fresh basket.
- Then Increment 3: dashboard (booking bar chart + API panel).

## 2026-11-02 — Trading logic spec (Alcadeias HAM port)

- **Studied Alcadeias `dev_btcusd1_v2`** (MT5 HAM = Heiken-Ashi Martingale): read
  `strategy.py`, `indicator.py`, `constants.py`, `symbols.json`, and the `app.py`
  trade loop via a throwaway git worktree at `C:/Users/sings213/Ballom/_alc_ref`.
- **Mapped the existing Fyers `dev_trading` netting port** (via subagent): confirmed
  it already does RSI-gated fib DCA + `effective_pl = pl − booked_profit` + close on
  `hedge + charges`; gaps vs Alcadeias = ladder depth (3 vs 5+forced), extra
  GAP%/relationship gates, no balance step-up.
- **Added `TRADING_LOGIC.md`** — exact spec: HAM strategy, hedging→netting mapping
  (BUY→CE, SELL→PE), balance-tiered units (₹50K→65qty/₹100, ₹1L→130qty/₹200),
  tax-aware close target (₹100×units + charges), carry/overnight handling, and the
  per-symbol P/L-booking dashboard chart (MT5-style per-close bars + cumulative/drawdown).
- _Why:_ owner wants the strategy reproduced 100% exact from Alcadeias before coding.

## 2026-11-02 — Branch created & skeleton started

- **Created branch `fyers_live`** from `dev_scanner`. _Base chosen because its
  auth + scan were verified working live today._
- **Verified Fyers auth works** end-to-end (TOTP → token → live quotes +
  optionchain) on Python 3.11. _Confirmed the broker hasn't broken the flow._
- **Researched current Fyers API limits** (category-wise since 16 Oct 2026):
  Standard plan DATA = 5/s · 50/min · **5,000/day** (binding), ORDER/USERINFO =
  10/s · 200/min · 100,000/day. _Drives the API-meter design._
- **Added `ARCHITECTURE.md`** — design + decision log (live-only, trimmed
  dashboard as separate process, track+throttle meter, two-loop + per-symbol
  threads).
- **Added `CHANGELOG.md`** — this file.
- **Added `constants.py`** (rewritten: live-only paths, market-hours table,
  rate-limit table, endpoint→category map).
- **Added `symbols.json`** (NIFTY index + GOLD/SILVER commodity, disabled by
  default, with `asset_type` + per-symbol window).
- **Added `api_meter.py`** — thread-safe API counter + throttle; writes
  `api_usage.json`.
- **Added `application.py`** (skeleton) — Loop-1 supervisor + per-symbol worker
  spawning.
- **Added `symbol_worker.py`** (skeleton) — per-symbol Loop-2 state machine
  (manage-open-position-first, else scan-once-a-day).
- **Trimmed `state_writer.py`** to live-only (dropped demo/live split + unused
  signal/account writers); added `scanner.py` stub, `.gitignore`, and a leaner
  `requirements_fyers.txt` (dropped dacite/multiprocess/bs4/scipy; added tzdata,
  dash, plotly).
- **Verified:** all modules `py_compile` clean + the full engine import graph
  wires on Python 3.11; `api_meter` throttle tested (5/s pacing, -429 cooldown,
  snapshot persistence to `api_usage.json`). _No live API calls happen in the
  skeleton — the scan is a stub._

### Decisions captured
- Dashboard: **trimmed**, **separate process** reading shared state files.
- API meter: **track + throttle** (never allow a limit breach).
- **Demo mode removed** — live only.

### Deferred (next prompts)
- Wire `fyers_api` calls through `api_meter` (route `safe_api_call` → `meter.guard`).
- `scanner.py` improved option-pair logic (pts 7–8).
- `dashboard.py` trimmed UI + API-usage panel.
- Open/close position logic + `TRADING_LOGIC.md`.
- `.bat` launchers (live-only) + `requirements` (+dash/plotly).

### Open questions
- Index entry cutoff: 15:15 vs trade-to-close 15:30?
- Exact Fyers `MCX:` symbols for GOLD/SILVER.
- Commodity entry cutoff before 23:30/23:55.
