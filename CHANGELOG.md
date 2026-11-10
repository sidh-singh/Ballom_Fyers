# CHANGELOG — `fyers_live` / `fyers_demo`

## 2026-11-10 — Dashboard: today's market-status badge (open / closed / holiday / unusual open)

The top status strip now shows, right next to **● Engine RUNNING**, a pill telling you what
**today's market** is doing — so a silent bot is no longer ambiguous between *markets are shut*
and *something broke*:

- **● Market open** (green) — a traded segment is in its normal session right now.
- **○ Market closed** (grey) — a normal trading day, but outside hours (tooltip: *Pre-open —
  trading starts 09:15* / *Closed for the day — reopens 09:15*).
- **◆ Holiday** (blue) — a weekend or an NSE holiday.
- **★ Unusual open** (amber) — the exchange is open on a statically-closed day: a Diwali
  **Muhurat**, a weekend **Union-Budget** session, or any special session.

**Engine** (`application.py`): new `_market_summary(ctx, config)` classifies today **only** from
state the supervisor already tracks — the broker's live `market_status` (`ctx.is_segment_open`),
the NSE holiday set and the static schedule — with **zero hardcoded dates**. An open segment on a
statically-closed day ⇒ *unusual* (this is how Muhurat / Budget auto-light-up, reusing the live
special-session auto-detection). A stale/missing live poll falls back to the static schedule and
flags `live=false`. The dict rides on the periodic `write_app_status()` writes (running + brake)
under a new `market` key.

**Dashboard** (`dashboard.py`): new `_market_badge()` renders a themed pill (colour + icon) after
the engine dot, with the detail in the tooltip + sub-text; it renders nothing when the field is
absent, so old `app_status.json` files stay compatible. `DASHBOARD.md` documents the four states.

Applies to **all 12 branches** (`fyers_*` + `linux_fyers_*`, demo/live × plain/netting/hedging) —
the code is identical on each; only this changelog entry is per-branch.

## 2026-11-09 — Dashboard is now mobile-phone friendly (responsive layout)

The dashboard renders properly on a phone, matching the `health_check` dashboard. Added the **viewport meta-tag** (`width=device-width, initial-scale=1`) so the page scales to the screen instead of showing a zoomed-out desktop layout; both Plotly charts are **responsive** (`config` responsive + figure `autosize` + width 100%) so they resize to their container; the per-symbol **info column and price/booking-chart column** — plus the **API-usage cards** — now **wrap and stack** on narrow screens (fluid `flex`-basis + `flexWrap` + `minWidth:0`); the **strategy-log table scrolls horizontally** instead of overflowing the page, and the **API rate-limit rows wrap**. Desktop is unchanged — the fluid bases only take effect once the viewport is too narrow for side-by-side columns. Both families patched (HAM `info_row` + RSI+MACD `top` symbol-panel splits). All 12 branches. _Why: owner wanted every bot's dashboard to be phone-friendly like health_check's._

## 2026-11-09 — Dashboard: automatic light / dark theme by the IST clock

The dashboard now **themes itself to the time of day** — a light palette from **06:00 to 18:00 IST** and the original dark palette overnight — matching the `health_check` dashboard. No toggle, no restart: a single colour dict `C` is re-applied at the top of every 5-second refresh (`_apply_theme()` chooses light/dark from `now_ist().hour` against `DAY_START_HOUR`/`DAY_END_HOUR` = 6/18), and the page background + title are returned as callback outputs, so the whole UI — KPI cards, per-symbol panels, Plotly charts (transparent paper, so they inherit the themed card), the strategy log and the API-limit panel — recolours live the moment the clock crosses 06:00 / 18:00. The force-close / add-balance inputs keep a fixed neutral style so a half-typed password is never wiped by a refresh. Two palettes live side by side as `THEME_DARK` / `THEME_LIGHT`; edit the two hour constants to move the window. All 12 branches (HAM + RSI+MACD dashboards both patched). _Why: owner wanted the same auto light/dark-by-IST behaviour the health_check dashboard already had._

## 2026-11-09 — Force-close is now password-protected (+ how to reset the password)

Because the dashboard is reachable on the network (see next entry) and **Force-close flattens a REAL position**, the button now requires a password. A password box sits in the controls row; `_on_force_close` hashes the entry (SHA-256) and compares it to `_FC_HASH` — a wrong or empty password returns `⛔ wrong password — force-close for <sym> NOT sent` and queues nothing. **The password is never stored in the repo as plaintext** (this repo is public) — only its SHA-256 hash is compiled in. **To reset it** (if leaked), use any one of these, then restart the dashboard: `FORCE_CLOSE_PASSWORD=<new>` (plaintext env, hashed at startup), `FORCE_CLOSE_PASSWORD_SHA256=<hash>` (pre-computed hash env), or change the built-in default hash in `dashboard.py` (`_FC_HASH`) — compute a new one with `python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "<new>"`. Precedence: env plaintext → env hash → built-in default. See **DASHBOARD.md → Security** for the full procedure (systemd / Windows / shell). All 12 branches. _Why: owner asked to gate force-close with a password, and how to reset it if it is compromised._

## 2026-11-09 — Dashboard reachable from other devices (bind 0.0.0.0) + mobile access

The dashboard bound to `127.0.0.1` (local only). It now binds **`0.0.0.0`**, so it is reachable at **`http://<server-ip>:<port>`** from a phone or another PC on the network — the same as the `health_check` dashboard. The startup line now prints `dashboard → http://0.0.0.0:<port>   (reachable at http://<server-ip>:<port>)`. Open the port in the host firewall (Linux: `ufw allow <port>/tcp`; Windows: an inbound rule for Python). The dashboard is read-only apart from the now-password-gated Force-close, but prefer a **VPN / SSH tunnel / authenticated reverse proxy** when exposing it beyond a trusted LAN. See **DASHBOARD.md → Access**. All 12 branches. _Why: owner wanted to open each bot's dashboard from a phone / another machine by IP:port, like health_check._

## 2026-11-09 — Dashboard controls: per-symbol Force-close button + demo Add-balance

Two dashboard actions that reach the engine through a new command channel — the dashboard (a separate read-only process) writes a JSON file to `state/commands/`, and the supervisor executes it each tick then deletes it (same spirit as the brake flag; `application._process_commands`). `constants.COMMANDS_DIR`.

- **Force close (all 12):** each symbol panel (today view) gets a small "⏹ Force close" button → queues a `force_close` command → flags that symbol's worker (`force_close`) to flatten its open basket on the next tick via the normal `_close_leg` path (a real Fyers sell on **live**, simulated on **demo**; books P&L + updates the dashboard). Dash pattern-matching callback with a value guard so the 5-sec auto-refresh never fires a stray close. (To also *pause* re-entry, use the brake.)
- **Add demo balance (6 demo branches only):** an amount input + "＋ Add demo balance" button → `add_balance` command → `DemoBroker.add_balance` tops up the simulated balance (locked + persisted). Hidden on live, which has no `add_balance` (the command is safely ignored there).

_Why: owner wanted a one-click force-close per symbol, and a way to top up a low demo balance._

## 2026-11-07 — Backfill past-day OHLC candles on startup (date-picker charts work immediately)

The date picker showed past-day trades/P&L but **no price candles** for days before the upgrade, because per-day OHLC snapshots were only written going forward. Each worker now backfills on startup: one `fetch_historical_data` call (~1500 M5 index candles) per symbol, IST-aligned, grouped by date, writing `state/ohlc_hist/<date>.json` for each past day in the retention window (`state_writer.backfill_ohlc_state`; never overwrites a live-written day; best-effort, guarded to run once). So after a restart the last week's price charts appear right away. `symbol_worker._backfill_ohlc` uses the `"5"` (M5) resolution — identical on both HAM and RSI+MACD workers. All 12 branches. _Why: owner saw past-day charts with no candles; snapshots needed seeding from history, not just going forward._

## 2026-11-07 — Dashboard date picker (view the last 7 days) + 1-week data retention

Added a **"View date"** dropdown (top-right): Today + the previous 6 days. Pick a past date and the whole dashboard switches to THAT day — the per-symbol OHLC price chart with that day's entry/exit markers, the day's closes / booked P&L, per-symbol P&L chart, and that day's strategy log. A banner marks the historical view; live-only panels (current RSI / SHA / slope signals, balance / equity, API usage) are shown for **Today only**, and the KPI row switches to the day's realised / gross / taxes / closes. Today stays fully live. The picker auto-rolls each day without losing the current selection (`_sync_viewdate`).

**Storage + retention (1 week):** the engine now writes a per-day OHLC snapshot `state/ohlc_hist/<date>.json` (the dashboard loads the selected day's); `profit_history.json` is pruned to the last 7 days on write; strategy-log retention drops 14 → 7 (`HISTORY_RETENTION_DAYS`); a shared `_prune_dir` keeps each history folder to the window. `profit_history` already stamped every fill with a `date`, so past-day filtering was already supported. Dashboards are strategy-specific (HAM vs RSI+MACD) — both patched. All 12 branches. _Why: owner wanted to review what/how it traded on past days and keep only ~1 week of data._

## 2026-11-07 — Faster dashboard refresh: account/P&L cadence 30s → 10s

The dashboard already polls every 5 s and reads JSON state, and per-symbol signals already refresh every `WORKER_IDLE_SLEEP`=8 s — but the **account block** (balance / equity / unrealized P&L / margin / open-position count / app-status) is written once per Loop-1 supervisor tick, which slept `SUPERVISOR_TICK`=30 s. So those values felt ~30 s stale. Dropped `SUPERVISOR_TICK` 30 → **10 s**, so account/P&L now refreshes ~10 s (dashboard shows it within 5–10 s). Cost: `_write_account` (funds+positions = 2 USERINFO) + `market_status` (1) per tick → ~18 USERINFO/min during market hours, far under the 200/min standard-plan cap (and off-hours `_write_account` still skips the API, reusing the last snapshot). Raise `SUPERVISOR_TICK` again if several bots share one Fyers account. All 12 branches. _Why: owner saw the dashboard values refreshing only ~every 30 s and asked to bring it to ~10 s._

## 2026-11-07 — NIFTY round-strike grid = 500s (famous strikes), per owner

The synced scanner defaulted every underlying to a 1000 round-step; NIFTY should prefer the **500-grid** (owner's "famous strikes": 24000/24500/25000/25500/26000, not just 24000/25000/26000). Added `_default_round_step(underlying)`: when symbols.json gives no `round_strike_step`, NIFTY/BANKNIFTY default to **500** and GOLD/SILVER to **1000**; an explicit `round_strike_step` in symbols.json still overrides (incl. 0 = off). Verified: 24500/25500 now score the full round bonus (1.0); granular 25050/25100/25250 are deprioritised (0.80/0.60/0.00). All 12 branches. _Why: owner confirmed the 500-grid for NIFTY._

## 2026-11-07 — Round-strike + min-premium scanner ported to HAM: all 12 scanners in sync

The round-strike preference and per-lot premium BAND only ever reached the 8 RSI+MACD branches' scanner; the 4 HAM branches (fyers_live/demo + their linux) still ran the plain moneyness/liquidity scorer (same dead-code gap as the month-end fix). Ported the full `scan_best_pair` + `_score` from the RSI+MACD scanner to HAM so strike selection is identical on all 12: `_roundness(strike, step)` bonus (prefer round, liquid, long-history strikes), per-lot premium **BAND** (skip premium×lot > max **or** < min), rebalanced weights (moneyness .30 / roundness .20 / spread .20 / OI .20 / volume .10). Added `SCAN_MIN_PREMIUM_PER_LOT=10000` + `SCAN_ROUND_STRIKE_STEP=1000` to HAM `constants.py`. `scanner.py` is now **byte-identical across all 12 branches**. Round step is per-symbol (`round_strike_step` in symbols.json) with a 1000 default. _Why: round strikes were a prior "all 12" requirement but were only half-wired; owner asked to sync all 12._

## 2026-11-07 — Fix: month-end expiry now actually applied in the LIVE scanner

The earlier "month-end expiry (roll if <2wk)" change was written into `fyers_api.fetch_option_pair`, which is **dead code** — the live engine selects expiry in `scanner.scan_best_pair`, which was still picking the nearest expiry in a DTE window. Result: NIFTY on 2026-11-07 chose the 2026-11-17 weekly (10 days out) instead of the 2026-11-23 month-end. Ported the rule into `scan_best_pair`: group expiries by calendar month (last per month = the monthly/month-end), pick the closest month-end `≥ SCAN_ROLL_DTE` (14 days = 2 weeks); if the current month-end is inside 2 weeks, roll to next month's. Added `SCAN_ROLL_DTE=14` (legacy `SCAN_MIN_DTE`/`SCAN_MAX_DTE` now unused). Applies to index **and** commodity. Verified on the real NIFTY chain: 2026-11-07 → 11-23 (16 d, was 11-17 / 10 d); 2026-11-15 → rolls to 12-29 (Nov month-end then 8 d < 2 wk). All 12 branches (HAM + RSI+MACD scanners both patched). _Why: owner saw NIFTY on a ~10-day weekly expiry; the month-end rule existed only in an unused function._

## 2026-11-07 — Auto-detect trading days + unusual sessions LIVE (no hardcoded dates)

Superseded the hand-maintained Muhurat/Budget table (added earlier today) with fully automatic, broker-driven detection — the engine now trades Diwali Muhurat, the Union Budget session and ANY day NSE/MCX keeps open, **every year, with zero hardcoded dates or times**. `fyers_api.get_market_status()` polls Fyers' own `market_status` (per-segment OPEN/CLOSE: NSE-NFO for NIFTY, MCX for GOLD/SILVER). The supervisor caches it on the context each tick (`EngineContext.is_segment_open`, trusted only while `< MARKET_STATUS_STALE_S` = 180 s fresh) via `_refresh_market_status`. `symbol_worker._should_trade_now()` is the new master gate: **PRIMARY** = the broker says this asset's segment is OPEN right now (so muhurat/budget/any special session just works, and a surprise halt stops us); **FALLBACK** (no fresh live reading) = the static schedule, but only on a genuine scheduled weekday — never trade a holiday/weekend without live confirmation. When the broker reports open on a statically-closed day, `application._refresh_special_sessions()` injects TODAY as a special session (wide 00:00–23:59 window) so the historical fetch + `is_trading_day` include today's forming candles; the actual trade gate still uses the live status directly. `_known_special_sessions()` now returns `[]` (the `_MUHURAT_SESSIONS` table is removed); `market_status` is metered USERINFO. Holidays still auto-refresh yearly from the NSE holiday API. Propagated to all 12 branches by cherry-pick (clean on every branch). _Why: owner will not hardcode any dates — the system must find open days + their hours itself and trade accordingly._

## 2026-11-07 — Trade Diwali Muhurat + weekend Union-Budget special sessions

The engine now TRADES the NSE sessions that fall on otherwise-closed days. `fyers_api._known_special_sessions` replaces the old (wrong) hardcoded "Oct 21 evening" Muhurat with a per-year table `_MUHURAT_SESSIONS` holding the real date **and** time from each year's NSE circular (2025-10-21 13:45–14:45; 2026-11-08 14:20–15:30 — Muhurat moved from an evening to an afternoon slot in 2025, and both date and time shift yearly, so **the table MUST be updated every year**; an un-listed year yields no Muhurat session, so the bot never trades a guessed time). Union Budget now fires when Feb 1 lands on **Saturday OR Sunday** (was Saturday-only), running the normal 09:15–15:30 session (2015 / 2020 / 2025 precedents). Each session is tagged `asset_type=INDEX`. `fetch_trading_holidays` now **re-derives special_sessions on every cache read** (they are code-defined, not from the NSE API), so a stale `trading_holidays_YYYY.json` can never serve an outdated Muhurat date/time. Crucially `symbol_worker._in_window` now honours the session: on a special-session day it trades **only inside that session's own window** and **only for the matching asset** (MCX legs stay flat during an NSE-only Muhurat); normal days are unchanged. `is_trading_day` already treats a special-session date as open, so the gates now line up. All 12 branches. _Why: owner wanted the bot to detect and trade Diwali Muhurat, the Union Budget session, and any day NSE keeps trading open — not sit idle through them._

## 2026-11-06 — OHLC price chart: index + all today's trades, IST-aligned, current-day

Final per-symbol price chart. Plots the UNDERLYING INDEX's today 5-min candles (IST-aligned — Fyers epochs are UTC, fills are IST) and overlays EVERY trade for the symbol (live position + all closed trades, across every option leg) at its exact time: ENTRY/ADD hollow circle (CE blue / PE orange), CLOSE filled circle (green profit / red loss), dotted entry->exit line, green/red Rs P/L label; the netting averaged position (yellow dotted) shows only for a multi-entry position. Premium + net Rs in the hover. symbol_worker persists the index (not the active leg) and, after a symbol's market closes, persists today's index candles once so its chart stays visible (fixes NIFTY blank in the evening while MCX still trades). Dashboard groups fills by asset.

## 2026-11-06 — Per-symbol OHLC price chart with your open/close fills marked

New full-width candlestick chart per symbol, plotting the ACTIVE option-leg premium (the instrument you trade) so fills line up on the price axis. From profit_history.json: OPEN (green up-triangle), each OB/OS ADD (green dot), CLOSE (red down-triangle); a dotted connector per position to the close/now coloured by P/L (green profit / red loss); a recessive dashed line at the booked average price while open. **Netting branches** also draw the averaged position — blue diamond open, orange diamond close, yellow dotted line between (gated by SHOW_AVG_POSITION). Recessive teal/slate candles keep the bright fills the focus; hover shows each fill's time/price/net.

Engine: new OHLC_STATE_FILE + state_writer.write_ohlc_state persist the active leg's last ~96 M5 candles; symbol_worker._persist_ohlc uses the active leg while open, the last-traded leg through the session, else the index. RSI+MACD reuses the per-bar _frame cache (no extra API); HAM adds a small per-bar leg-candle cache. All 12 branches. _Why: owner asked to see the price action with their entries / exits / averages marked, full-width, per symbol._

## 2026-11-06 — Worker loop idle 15s -> 8s (faster LTP-based profit-booking)

`WORKER_IDLE_SLEEP` reduced 15 -> 8 in `constants.py`. Entries are unaffected (RSI/MACD/SHA signals are computed on 5m/15m/H4 candles and only change on a new bar), so this only speeds up the **LTP-based profit-booking / force-close reaction** from ~15-25s to ~8-10s. Safe for a single bot on Prime (well under 500 DATA/min with per-bar caching); keep >=15 if several bots share one Fyers account (shared per-account rate limit). Not set to 0 — the `api_meter` would then throttle the loop anyway while spinning CPU on cached recompute, with no entry benefit and a -429 / daily-block risk. `SUPERVISOR_TICK` (30s account/maintenance poll) unchanged. All 12 branches. _Why: owner wanted less delay; this trims the only latency that matters (profit exits) without hammering the API._

## 2026-11-05 — API rate-limit detection + bottom-of-dashboard “API RATE LIMITS” panel

`api_meter` now captures every Fyers **-429** — per category it exposes `rate_limited`, the live
`cooldown_s` left on the back-off, and a `limited_today` count, plus the day's `minute_breaches` —
all published in `api_usage.json`. The dashboard gains a **bottom panel** listing every API category
(DATA / ORDER / USERINFO) with sec / min / day used-vs-limit (amber ≥60 %, red ≥85 %), a day bar, and
a live status chip: **⛔ RATE-LIMITED · Ns** during a cooldown, “N× limited today” after a hit, else
OK; a ⚠ MIN-BREACHES badge appears if >3. _Why: the hedging branch surfaced “You've reached API
request limit for timeframe M15 for GoldM” while netting didn't — with one engine on Prime
(500 DATA/min) and per-bar caching that shouldn't happen, so the likely cause is a second process on
the same Fyers account (the per-account limit is global; each process's meter counts only its own
calls) or hedging's higher book/re-enter frequency (more scanner + option-leg fetches). The panel
makes any -429 visible instead of a silent log line._ All 6 branches (api_meter on all; dashboard
HAM + slope).

## 2026-11-05 — Fix: "insufficient candles" — IST-aware history fetch + holiday-skip

Root cause: `fetch_historical_data` used naive `datetime.now()` + naive `.timestamp()` for the
request window while market hours are IST. On a non-IST host (demo/cloud boxes are usually UTC) the
`range_from`/`range_to` epochs shifted by the 5h30m IST offset, so the window fell largely outside
NSE/MCX trading hours and Fyers returned far fewer candles than the range implied — tripping
"insufficient candles" (HAM needs ≥60 for SHA; the slope branches warn). It was worst in the early
morning (the end_dt branch also flips to the previous close). Now `now = now_ist()` and every
`datetime.combine(...)` is `tzinfo=IST` → correct UTC epochs on any server timezone (falls back to
naive if tzdata is missing; unchanged on an IST host). Secondary: the worker now passes
`ctx.holidays` + `ctx.special_sessions` to the fetch so the day-walk skips holidays (it was treating
them as trading days and under-delivering after a holiday). All 6 branches.

## 2026-11-04 — Off-hours: no balance/positions API polling when markets are closed

The supervisor polled `get_balance` + `positions` every 30 s all night (~2 read-API calls / 30 s for
nothing — balance/positions don't move while the market is shut). Now it polls only while any enabled
symbol's market window is open (index to 15:30, commodities to 23:30); off-hours it reuses the last
snapshot and writes only the plan/renewal + a `market_open` flag (no API). The per-symbol worker was
already clean out-of-window (it returns before any API past its trading-window guard). The only API
calls off-hours remain the **auth keep-alive** (token verify / 5 min) + the **~00:10 daily reauth**.

## 2026-11-04 — Docs: BRANCHES.md refreshed to the current logic

Brought the system map up to date with every recent change: the RSI+MACD **index/leg split**
(open on the index, book + force-close on the CE/PE leg's own chart), the retuned sections (BOOK
= M5 **both-reverse** ±2, FORCE_CLOSE = M15 **either-reverse** ±1), the **180 s min-hold**
anti-vibration, the **₹300-net** profit floor (taxes stacked on top), and a new **§8 Operational
& tuning notes** — 120-candle fetch + convergence reasoning, the Fyers **100-day intraday cap** +
clamp, index/commodity market-close times (15:30 / 23:30), the **one-loop worker (15 s idle)** vs
the separate supervisor thread, and **Prime being config-driven** (no API field exposes it).

## 2026-11-04 — Lighter history fetch (120 candles) + faster, clearer worker loop

- **Candle counts cut:** `RSI_CANDLE_COUNT` 500→**120**, `SHA_CANDLE_COUNT` 300→**120**. RMA-14 RSI,
  MACD's EMA-26, and SHA's DEMA-20/TEMA-10 all converge well within 120 bars, and the latest-pivot
  slope only reads the last ~20 bars — so 120 is ample while sending ~4× less data per request.
  (Not 50-60: the slow smoothers — MACD EMA-26 and SHA DEMA-20 — aren't converged by then, which
  would skew the slope that the OPEN score depends on.) Bonus: keeps H4 under the Fyers 100-day cap.
- **Worker loop faster + clearer:** `WORKER_IDLE_SLEEP` 60→**15s**. The worker is a SINGLE loop
  (fetch+trade, then idle) — not two dependent loops; the supervisor (account snapshot/maintenance)
  is already a separate independent thread. Shorter idle → the LTP-based profit-book reacts in
  ~15-25s instead of ~70s; per-bar caching keeps the slope fetches bounded. Timing print relabelled
  `fetch+trade=.. / cycle=..` so it reads as one loop (RSI+MACD branches).
- Confirmed: the history walk-back already uses the correct closes (index 15:30, commodity 23:30).

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
