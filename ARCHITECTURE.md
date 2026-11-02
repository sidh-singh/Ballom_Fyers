# ARCHITECTURE — `fyers_live` branch

> **Status:** 🟡 Skeleton in progress
> **Branch:** `fyers_live` (branched from `dev_scanner`)
> **Scope:** Single consolidated branch for the Ballom_Fyers bot — **LIVE only**
> **Last updated:** 2026-11-02

This is the single source of truth for the consolidated bot. Every change is
also recorded in [CHANGELOG.md](CHANGELOG.md). Trading (open/close) logic is
documented separately in `TRADING_LOGIC.md` (added later).

---

## 1. Purpose & scope

`fyers_live` collapses the old multi-branch design (`dev_scanner`,
`dev_updater_*`, `dev_trading`, `front_end`) into **one branch** that does
everything. Guiding rules from the owner:

- **Live only.** Demo/paper mode is removed (Fyers has no paper trading; demo
  was only a local state-dir switch and is dead weight).
- **Lighter & faster.** No speculative/unused code. Features are added when
  they are actually needed.
- **API-budget aware.** Fyers enforces hard API limits (see §6). The engine
  must stay under them, so a local **API meter** tracks and throttles every
  call.
- **Index + Commodity only.** NIFTY-style index options and MCX commodity
  (GOLD/SILVER). No cash-equity/stock options.

**In scope now (this skeleton):** process/threading architecture, the API
meter, per-symbol workers, auth reuse, config, and a trimmed dashboard.
**Deferred (later prompts):** improved option-pair scan, and opening/closing
positions. See §11.

---

## 2. Decision log

| # | Decision | Choice |
|---|----------|--------|
| D1 | Dashboard scope | **Trimmed** — only panels `fyers_live` feeds now (status, API-usage, positions, option-pairs, strategy log); grow on demand |
| D2 | Dashboard process model | **Separate process, shared state files** — engine writes JSON, dashboard reads it. Isolates the fast engine from the heavy Dash UI |
| D3 | API meter behavior | **Track + throttle** — count every call and proactively pace/pause to stay under limits; honor `-429 Retry-After` |
| D4 | Branch base | Branched from `dev_scanner` (its auth + scan were verified working live on 2026-11-02) |
| D5 | Mode | **Live only** — `demo`/`live` split removed; single state dir |
| D6 | Index entry window | Market **09:15–15:30**; **entry cutoff 15:15** (owner said "3:15"; confirm) |
| D7 | Commodity window | MCX non-agri **09:00–23:30** (US-DST Mar–Nov) / **23:55** (Nov–Mar) |

---

## 3. Process model (D2)

Two independent OS processes in the **same branch/repo**, communicating only
through atomic JSON files under `C:/Ballom_FYR/state/`:

```
┌────────────────────────────┐        writes         ┌─────────────────────────┐
│   application.py (ENGINE)  │ ─────────────────────▶ │  C:/Ballom_FYR/state/   │
│   • Loop-1 supervisor      │   app_status.json      │  (atomic JSON files)    │
│   • Loop-2 symbol workers  │   api_usage.json       │                         │
│   • API meter (throttle)   │   option_pairs.json    │                         │
│   • Fyers auth             │   position_state.json  │                         │
└────────────────────────────┘   strategy_log/…       └───────────┬─────────────┘
                                                                   │ reads (read-only)
                                                       ┌───────────▼─────────────┐
                                                       │  dashboard.py (Dash)    │
                                                       │  http://127.0.0.1:8050  │
                                                       └─────────────────────────┘
```

- The **engine** never imports Dash; the **dashboard** never calls Fyers. This
  guarantees the UI can never slow down or crash the trading loop.
- Launched by two `.bat` files: `start_app.bat` (engine) and
  `start_dashboard.bat` (UI).

---

## 4. Repo layout

| File | Status | Purpose |
|------|--------|---------|
| `ARCHITECTURE.md` | ✅ | This document |
| `CHANGELOG.md` | ✅ | Running log of every change |
| `constants.py` | ✅ | Paths, market-hours table, rate-limit table, categories |
| `symbols.json` | ✅ | Per-symbol config (NIFTY / GOLD / SILVER) with `asset_type` + window |
| `TRADING_LOGIC.md` | ✅ | Strategy spec (Alcadeias HAM → netting) |
| `api_meter.py` | ✅ | API counter + throttle → `api_usage.json` |
| `application.py` | ✅ | Engine: Loop-1 supervisor + spawns per-symbol workers |
| `symbol_worker.py` | ✅ | Per-symbol trading thread (HAM execution on netting) |
| `indicator.py` | ✅ | SHA-v3 / RSI / ADX — 1:1 from Alcadeias (verified identical) |
| `strategy.py` | ✅ | HAM signal brain — 1:1 from Alcadeias |
| `position_tracker.py` | ✅ | Netting booked-profit (`effective_pl`) + carry fix |
| `fyers_auth.py` | ✅ reused | TOTP auth (verified 2026-11-02); unchanged |
| `fyers_api.py` | ✅ | Data + order/account calls, all through `api_meter` |
| `scanner.py` | ✅ | CE/PE strike pick (reuses `fetch_option_pair`; pt7 improvement later) |
| `state_writer.py` | ✅ | Atomic state writes (live-only) |
| `dashboard.py` | ✅ | Trimmed Dash UI: API meter + per-asset booking chart |
| `start_app.bat` / `start_dashboard.bat` | 🟡 | Live-only launchers |

Legend: ✅ done · 🟡 skeleton/partial · ⬜ not started

---

## 5. Runtime architecture — two loops (pts 10–12)

### Loop 1 — Supervisor (single thread, low frequency)

Owns everything **not** symbol-specific. Runs slowly (e.g. tick every 30–60 s)
and performs these in order:

1. **Daily maintenance @ ~00:10** — force re-auth + re-download the NSE symbol
   CSV once per day (pt 1).
2. **Holidays** — fetched **yearly on Jan 1**, *or* immediately if the list for
   the current year is missing (pt 1). Used to gate workers (pt 3).
3. **Auth health** — if auth ever fails anywhere, re-authenticate (pt 2).
4. **Brake kill-switch** — if `symbols.json "brake": 1`, pause all workers (pt 4).
5. **Worker supervision** — spawn one worker thread per configured symbol;
   restart a worker if it dies.

It does **not** poll market data on a timer (no hourly firing — pt 5). Market
work happens in the workers.

### Loop 2 — Per-symbol workers (one thread each, pt 12)

Each symbol (NIFTY, GOLD, SILVER) runs its **own** `SymbolWorker` thread so one
symbol can never block another (pt 12) and heavy work stays off Loop 1 (pt 10).
Each worker only acts inside **its** market window (pt 9) and runs this state
machine (pt 6):

```
          ┌──────────────┐
          │   IDLE       │  outside market window / holiday → sleep
          └──────┬───────┘
                 │ in-window & trading day
                 ▼
          ┌──────────────┐   position already open for this symbol?
          │  CHECK_POS   │────────────────────────┐
          └──────┬───────┘                        │ YES
             NO  │ (flat)                          ▼
                 ▼                          ┌──────────────┐
          ┌──────────────┐  scanned today?  │  MANAGE_POS  │  priority: close the
          │  SCAN        │  NO → scan once  │  (existing)  │  open position first (pt 6)
          │ (once/day)   │                  └──────┬───────┘
          └──────┬───────┘                         │ closed OK → back to SCAN
                 │ best pair chosen                 │
                 ▼                                  │
          ┌──────────────┐                          │
          │  (OPEN POS)  │  ← DEFERRED (later)      │
          └──────────────┘                          │
```

- **Flat** → run the once-a-day scan to pick the best CE/PE pair (pt 6, 7),
  then (later) open the position.
- **Position open** → do **not** scan; manage it, and the first priority is to
  close it successfully; only then is the symbol eligible to scan again (pt 6).
- API calls inside a worker go through the **API meter** (§6).

---

## 6. API meter — the budget guard (pt 5, D3)

Fyers enforces **category-wise** limits (changed **16 Oct 2026**). The owner is
on the **Standard** plan:

| Category | Endpoints | /sec | /min | /day |
|----------|-----------|-----:|-----:|-----:|
| **DATA** | `quotes`, `optionchain`, `history`, `depth` | 5 | 50 | **5,000** |
| **ORDER** | `place/modify/cancel/exit_order` | 10 | 200 | 100,000 |
| **USERINFO** | `get_profile`, `funds`, `holdings`, `positions`, `orderbook`, `tradebook` | 10 | 200 | 100,000 |

Rules enforced:
- **DATA 5,000/day is the binding constraint.** The old hourly scanner could
  burn it quickly — this is the main reason for the redesign.
- Exceeding any window → Fyers returns **`-429`**. Breaching the **per-minute**
  limit **>3×/day → account blocked for the rest of the day.** The meter
  **must never allow a breach.**

`api_meter.py` provides a thread-safe singleton that:
1. **Classifies** each call by method name → category.
2. **Tracks** rolling windows (per-second & per-minute via timestamp deques;
   per-day via a counter that resets at IST midnight).
3. **Throttles (D3):** before a call, blocks until a slot is free in all three
   windows; if the **daily** cap is hit it refuses and raises/parks with a loud
   warning (can't wait out a day).
4. **Honors `-429`** `Retry-After` / `X-Retry-After-Ms` headers after the fact.
5. **Persists** a snapshot to `api_usage.json` (counts + limits + utilization %
   per category/window) for the dashboard panel.

Integration: `fyers_api` routes every Fyers SDK call through
`meter.guard(method_name)` (wraps `FyersAuth.safe_api_call`).

---

## 7. Market hours & sessions (pt 9)

| Asset type | Open | Close | Entry cutoff | Notes |
|------------|------|-------|--------------|-------|
| `INDEX` (NFO) | 09:15 | 15:30 | 15:15 | NSE F&O; owner's "3:15" treated as cutoff (confirm) |
| `COMMODITY` (MCX non-agri) | 09:00 | 23:30 / 23:55 | TBD | 23:30 US-DST Mar–Nov; 23:55 Nov–Mar. Pre-open 08:45 |

Times are IST. **All datetime logic must be timezone-aware (IST)** — the old
code used naive `datetime.now()`, which silently breaks on non-IST hosts; the
redirect URI (`jenkin.…`) suggests a server, so this is a real risk to fix.

---

## 8. State files & paths

```
C:/Ballom_FYR/
├─ fyers_token.json              ← auth token (fyers_auth.py)
├─ cache/
│  ├─ fyers_options_YYYYMMDD.csv ← NSE F&O symbol master (daily)
│  └─ trading_holidays_YYYY.json ← NSE holidays (yearly)
└─ state/                        ← single live dir (demo removed, D5)
   ├─ app_status.json            ← engine lifecycle/health
   ├─ api_usage.json             ← API meter snapshot (NEW)
   ├─ option_pairs.json          ← scan output (best CE/PE per symbol)
   ├─ position_state.json        ← open positions + P&L (later)
   └─ strategy_log/YYYY-MM-DD.json
```

---

## 9. Symbol config (`symbols.json`)

```json
{
  "symbols": [
    {"symbol": "NIFTY",  "asset_type": "INDEX",     "underlying": "NSE:NIFTY50-INDEX", "qty_times": 1, "hedge": 500, "enabled": true},
    {"symbol": "GOLD",   "asset_type": "COMMODITY", "underlying": "MCX:GOLD...",        "qty_times": 1, "hedge": 500, "enabled": false},
    {"symbol": "SILVER", "asset_type": "COMMODITY", "underlying": "MCX:SILVER...",      "qty_times": 1, "hedge": 500, "enabled": false}
  ],
  "brake": 0
}
```

- `asset_type` drives the market window (§7). `enabled` lets a symbol be turned
  off without deleting config. `brake:1` halts everything (pt 4).

---

## 10. Auth (reused, verified)

`fyers_auth.py` is carried over unchanged from `dev_scanner` and was **verified
working end-to-end on 2026-11-02** (TOTP login → token → live quotes +
optionchain). Re-auth is triggered by Loop 1 daily and on any auth failure
(pt 2). Needs **Python 3.11** (deps don't build on 3.13).

---

## 11. Status / remaining

**Implemented & verified** (see CHANGELOG + TRADING_LOGIC.md): auth, API meter,
two-loop engine, per-symbol workers, HAM strategy + indicators (1:1 Alcadeias,
verified identical), netting execution + booked-profit, trimmed dashboard.

**Remaining:**
- **Improved option-pair scan** (pt 7–8) — `scanner.py` currently reuses `fetch_option_pair`.
- **Live dry-run** on a funded account — user-gated (real orders); confirming the
  tradebook fill price is the key item.
- Commodity symbols / timings (O4).

_Live hardening done 2026-11-02: self-tracked cost-basis carry model, H6 RSI resample,
per-bar RSI caching, configurable balance basis — see CHANGELOG._

---

## 12. Open questions

- O1: Is the index **entry cutoff 15:15** correct, or do you mean trade until
  market close 15:30? (D6)
- O2: Exact MCX symbols/expiries for GOLD & SILVER on Fyers (`MCX:…`)?
- O3: Commodity entry cutoff before 23:30/23:55 close?
