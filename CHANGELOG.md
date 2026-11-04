# CHANGELOG — `fyers_live` / `fyers_demo`

## 2026-11-04 — Fix: H4 / H1 history fetch failed (Fyers 100-day intraday cap)

`fetch_historical_data` walked back 500 bars to build `range_from`; on **H4** (240-min) that spans
~200 calendar days for MCX gold/silver and ~440 for the index (H1 ~110 for the index) — beyond
Fyers' **per-request limit of 100 days** for intraday resolutions (1m–240m; 366 for daily, 30 for
seconds). Fyers rejected the request, so the **H4 fetch failed for GOLDM/SILVERM** (and silently on
NIFTY), which zeroed the H4 slope the OPEN score depends on. Now `start_dt` is clamped to just under
the cap (95d intraday / 360d daily / 28d seconds); M5/M15/M1 are unaffected (a few-day span), and
H4/H1 still return 100+ bars — ample for RSI(14) + the latest-pivot slope. Shared fix in
`fyers_api.py` (demo inherits it).

## 2026-11-04 — Prime renewal date set (annual)

`symbols.json "prime_renewal"` = **2027-11-04** (annual Fyers Prime, bought 2026-11-04). The
dashboard API panel now shows the days-remaining countdown, turning red in the final 7 days.

## 2026-11-04 — Fyers Prime enabled: plan-aware limits / brokerage + dashboard fix

- `symbols.json "api_plan"` → **prime**. Fyers does not expose Prime via API, so it is
  config-driven; this switches the whole chain to Prime: `api_meter` uses the higher limits
  (DATA 10/500/500000, ORDER 10/500/500000), `brokerage_per_order` → **₹15** (was ₹20), and the
  profit target's tax estimate follows (STT / exchange / SEBI / stamp / GST rates unchanged).
- **Dashboard fix:** `api_panel` hardcoded the Standard `API_LIMITS` and — being a separate
  process — never ran `set_api_plan`, so it showed Standard thresholds even on Prime. It now
  reads the plan from `account_state` and shows `API_LIMITS_PRIME` vs `API_LIMITS` day/sec/min
  thresholds; the PRIME badge + renewal countdown already read `account_state`.
- To show the renewal countdown, set `prime_renewal: "YYYY-MM-DD"` in `symbols.json`.
- Restart the engine + dashboard to apply.

## 2026-11-03 — Dashboard: RSI DCA-ladder bars + next-position gauge + ₹ profit meter

More detailed per-symbol panel (symbols stay dynamic — rendered from whatever the workers write
to signal_state):
- **RSI DCA LADDER:** every ladder timeframe (M1/M5/M15/H1/H4) shown as a 0–100 bar with faint
  OS/OB zones and the add-side threshold line; the next add tier is ring-highlighted.
- **NEXT-ADD / FINAL-CLOSE gauge:** while a basket is open, a prominent meter for the next tier's
  RSI vs its OB/OS trigger, with the distance (RSI points) to the next position (or the H6 final
  forced close).
- **₹ PROFIT→BOOK meter:** live cycle P&L vs the ₹100×units + tax target, with a % / BOOKABLE chip.
- Position block adds level, positions used (n/6) and basket age. SHA signal/trend kept (the HAM
  entry indicator).
- **Worker:** `_rsi_mtf` now fetches the full multi-timeframe RSI set (entry filter + all ladder
  tiers + H6), cached per bar so the DATA budget stays bounded; the signal payload adds `ladder`
  (next tier / side / is_final), `close_threshold`, and position count/max/age.
- Verified: compiles on Python 3.11; panels render for flat / mid-ladder / final-close states.

## 2026-11-03 — Docs: BRANCHES.md system & branch map

- Added **BRANCHES.md** — a self-contained map of the whole bot: all six branches
  (`fyers_live`/`fyers_demo` + the four RSI+MACD netting/hedging branches), the module
  map, the two-loop engine architecture, both strategy families (HAM vs RSI+MACD),
  position sizing, the tax model, API throttling limits, environment + run steps, and
  the git topology. Written so the system can be maintained from the repo alone,
  without any external/chat history.
- Policy going forward: every change lands with a detailed CHANGELOG entry **and** a
  docs update (`TRADING_LOGIC.md` / `ARCHITECTURE.md` / `BRANCHES.md`).

## 2026-11-03 — Fix: commodity fib-DCA ladder never added (RSI used wrong symbol)

- `symbol_worker._rsi_one` fetched `self.underlying` (the placeholder `MCX:GOLDM-FUT`)
  instead of `self._active_underlying` (the resolved future, e.g. `MCX:GOLDM26DECFUT`).
  The invalid symbol threw → RSI fell back to **50.0** → GOLDM/SILVERM never hit OS/OB →
  the **fibonacci DCA ladder never added** (1→2→3→5→8→13). NIFTY's underlying is valid so
  it was unaffected. Now uses the resolved underlying.
- Verified: commodity RSI resolves to a real value and `BUY_MORE` fires at count 1 (M1) /
  2 (M5) / 3 (M15) on oversold; no add at neutral RSI.

## 2026-11-03 — Plan-aware limits/brokerage (Standard/Prime) + dashboard indicator

- API plan config-driven (`symbols.json "api_plan"`: standard|prime|auto); Prime raises
  API limits + lowers brokerage ₹20→₹15 (taxes unchanged); dashboard shows plan badge +
  renewal countdown (`prime_renewal` date — Fyers has no renewal API).

## 2026-11-03 — Per-trade tax / gross / net + Fyers-verified rates

- **Tax rates corrected to Fyers' published charges** (fyers.in/charges-list, fetched
  2026-11-03): STT 0.0625%→**0.15%** (sell premium), exchange txn 0.0495%→**0.0355299%**
  (NSE, incl. IPFT); brokerage ₹20/order, SEBI ₹10/cr, stamp 0.003% buy, GST 18% unchanged.
  (≈ ₹75.7 round-trip on 65 × ₹185.)
- Each close records **gross, tax, net (actual)** in `profit_history.json`; `realized_total`
  now accumulates **net**.
- Dashboard: booking bars = **net after tax** with Gross/Tax/Net hover; KPI row adds
  **Taxes (today)** + **Realised net (today)**; each symbol shows gross/tax/net booked.
- Verified: charges ₹75.71; gross 325 → tax 75 → net 250 through tracker → history → chart.

## 2026-11-03 — Dashboard fix: single-close cumulative visible

- Booking-chart cumulative trace now `lines+markers`, so a lone close renders as a dot
  (a single point in `lines` mode drew nothing). Applied to fyers_demo + fyers_live.

## 2026-11-03 — Rich dashboard + engine analysis state (demo)

- Demo balance → **₹90,000**; enabled **NIFTY + GOLDM + SILVERM** (mini contracts) for testing.
- **Engine writes analysis state** for the dashboard: `signal_state.json` per symbol
  (signal, best CE/PE pair, SHA signal+trend power/list, multi-timeframe RSI, rsi-block,
  live position) and `account_state.json` (balance/equity/margin/unrealized). Worker caches
  a per-bar display-scan so the best pair always shows without extra API cost.
- **Rebuilt `dashboard.py`** (rich, colourful): account KPI row, API meter, per-symbol
  strategy panels (signal badge, CE/PE pair cards, **SHA candle squares**, **RSI TF cells**,
  live position), per-asset **booking charts** (bar + cumulative/drawdown), strategy log.
  Colour semantics BUY=green / SELL=red / nothing=grey / ADD=amber / CLOSE=purple /
  HOLD=blue — every status colour paired with a text label (dataviz method).
- **Verified:** unit render (3 symbols) + a **live demo engine run (2026-11-03)** wrote real
  state — NIFTY held (M30 RSI 79 → RSI-block), GOLDM/SILVERM opened dummy CE; dashboard
  rendered from the real files. (Same dashboard + state code then ported to `fyers_live`.)

## 2026-11-03 — `fyers_demo` branch: local paper-trading (demo account)

Branch `fyers_demo` (from `fyers_live`). Fyers has no demo account, so OPEN/CLOSE are
**simulated on local disk** while **all read APIs stay real** — so API throttling and
the full scan/signal/trade logic are genuinely exercised with no real orders or money.

- Added `demo_broker.py`: `DemoBroker` (disk store at `C:/Ballom_FYR/demo/demo_broker.json`
  — positions/realized/balance/trades) + `DemoFyersAPI(FyersAPI)` overriding
  buy/sell/positions/get_position/funds/get_balance/last_fill_price/tradebook (simulated,
  **real LTP for P&L**); inherits real auth/quotes/optionchain/history (all metered).
- `application.py` uses `DemoFyersAPI`; dashboard header shows **DEMO**; demo config in
  `constants.py` (`DEMO_DIR`, `DEMO_START_BALANCE = ₹2,00,000`).
- Added `demo_cycle.py`: forced one-shot entry → fib-add → close (real data + simulated
  orders) to validate logic + throttling without waiting for a live signal.
- **Verified** — unit: DemoBroker math (avg 106, realized 2730), real-LTP P&L (₹1300 on
  +20). **Live cycle 2026-11-03:** balance ₹2L → units 3, scan CE 24000, entry 195, fib
  add +390, close; **DATA 9 calls (throttled), ORDER 0 (simulated).**

> To run the demo: `start_app.bat` on this branch runs the full engine with simulated
> orders; or `python demo_cycle.py` for a one-shot check. `dashboard.py` shows the
> simulated bookings + the real API-usage meter.

---

# CHANGELOG — `fyers_live`

Running log of every change on this branch (per owner's standing request —
record activity even when not explicitly asked). Newest first.

Format: `YYYY-MM-DD` — change — _why_.

---

## 2026-11-02 — Loop optimization (lighter, no over-processing)

- **SHA cached per bar** (`symbol_worker._get_sha`): the 300-candle fetch + SHA/trend
  compute now refresh at most once per 5-min bar instead of every 60s tick (SHA is a
  5-min indicator — sub-bar re-evaluation was wasted work). ~5× fewer DATA calls/symbol.
- **Commodity resolution cached daily**: the 16k-row MCX filter + session parse runs
  once per IST day (the future changes monthly), not every tick.
- **Loop-1 config reload on mtime**: `symbols.json` is re-read only when it changes on
  disk, not every 30s.
- (RSI was already cached per bar.) Net: both loops do mostly cached reads + cheap compute
  per tick; live API work is bounded by bar cadence, not tick cadence.
- Verified: SHA + RSI fetch once then serve from cache; netting flow / cap / commodity /
  get_balance tests still pass; full engine imports.

## 2026-11-02 — Per-lot premium cap (₹30K) on pair selection

- Scanner skips any strike whose **premium × lot_size > `SCAN_MAX_PREMIUM_PER_LOT`**
  (₹30,000 for a conservative start; `0` = off; per-symbol override `max_premium_per_lot`
  in symbols.json). Worker + `dry_run.py` pass the lot CSV so the cap applies; the best
  pair is chosen from strikes under the cap.
- Verified: end-to-end pair stays ≤ ₹30K/lot; scoring excludes ₹32,500/lot, keeps ₹26,000.

## 2026-11-02 — Auth day-boundary switched to IST

- `fyers_auth.py` now uses `now_ist()` / `today_ist()` for the token day-boundary,
  `created_at`, and the TOTP window check (was naive local time). The token's "new day"
  now matches the **IST market day regardless of server timezone**, so a non-IST server
  won't mistime re-logins or briefly treat a valid token as stale. Token caching/reuse
  behaviour is unchanged (1 login/day, reused for all calls). Verified: no naive
  `date.today()`/`datetime.now()` left; imports + `is_authenticated` exercise the IST path.

## 2026-11-02 — Server launchers modelled on Alcadeias (.bat)

- Rewrote `start_app.bat` + `start_dashboard.bat` to match the Alcadeias
  `start_btcusd.bat` / `start_dashboard.bat` pattern for server/Jenkins runs:
  hardcoded Python 3.11 root (`C:\Users\Administrator\...\Python311`), UTF-8 (Jenkins),
  deps-install-once via flag files, and run the app **directly** (its own loop runs
  forever; the server/Jenkins manages restart — no bat-level restart loop).
- **Demo mode removed** from launchers (one engine process runs all symbols as threads;
  no `%MODE%` / demo-vs-live branching).
- `.gitignore`: added `dash_installed.flag`.

## 2026-11-02 — Unit tier step ₹50K → ₹1L + live shadow dry run

- **Tier step 50,000 → 100,000**: `units = floor(bal/1L)+1` → `<1L`=1 lot, `1L–2L`=2, `2L–3L`=3.
  Lot size still from the CSV (NIFTY = 65). Updated `constants.py`, `symbols.json`, `TRADING_LOGIC.md`.
- **Added `dry_run.py`** — one-shot SHADOW validation (real auth + live data + real scan/signal,
  **NO orders**; safe any time).
- **Ran it live:** auth OK; balance **₹0 → units 1**; NIFTY SHA bullish but **MTF RSI overbought
  (96.6/82.6/75.4/73.8) → entry correctly BLOCKED** (DO_NOTHING); live scan returned a real carry
  pair CE 23800 / PE 23000, expiry 2026-11-17 (DTE 14), lot 65 → entry_qty 65; 8 DATA + 1 USERINFO
  calls, 0 orders. **Pipeline validated end-to-end against live Fyers.**
- ⚠️ Account is currently **unfunded (₹0)** — must be funded before a real-money run.

## 2026-11-02 — Commodities wired (GOLD/SILVER, verified vs MCX master)

- **`fyers_api.py`**: `download_commodity_data()` (MCX_COM.csv cache), `resolve_commodity(root)`
  → nearest non-expired FUTURE symbol + **session parsed from the CSV** (DST-correct),
  `_parse_mcx_session()`.
- **`symbol_worker.py`**: COMMODITY resolves the active future each tick (signal source +
  optionchain underlying), uses the MCX CSV for lot size, its CSV session for the window,
  and a per-symbol `hedge_per_unit`.
- **`application.py`**: downloads the MCX master in daily maintenance when a commodity is enabled.
- **`symbols.json`**: GOLD/SILVER by `root` + `hedge_per_unit`; **kept `enabled:false`** pending
  (1) a live Fyers optionchain confirm for MCX futures and (2) `hedge_per_unit` tuning
  (GOLD lot_size=1, so ₹100 target is likely too small).
- **Verified** vs the real MCX master: GOLD→`MCX:GOLD26DECFUT`, SILVER→`MCX:SILVER26DECFUT`,
  session 09:00–23:55; worker resolve/window correct; full engine imports.

## 2026-11-02 — Improved option-pair scan (pts 7–8, done & verified)

- **Rewrote `scanner.py`** — carry-oriented, self-contained CE/PE selection via
  `optionchain`: scores on moneyness (slight-OTM) + bid/ask spread + OI + volume +
  affordability; expiry preference `DTE ∈ [10,45]`. **Dropped the old per-candidate
  candle-history check** (unneeded now signals are index-driven) → far fewer DATA calls.
- Added scan tuning constants (`SCAN_*`) + `MCX_COM_URL`.
- **Verified** (mock chain): picks liquid slightly-OTM CE above spot / PE below spot,
  expiry inside the carry band. No per-option history calls.

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
