# DEV_UPDATER_NIFTY — Branch Documentation

> **Branch:** `dev_updater_nifty`
> **Purpose:** SHA signal analysis + multi-timeframe RSI for NIFTY index
> **Last updated:** 2026-03-XX

---

## 1. Overview

This branch runs a **24×7 near-real-time service** (every 3 seconds) that:

1. **Reads** the Fyers auth token from `C:/Ballom_FYR/fyers_token.json` (written by `dev_scanner`). **Never** performs TOTP login — if the token is invalid, flags it as `expired` so the scanner re-authenticates.
2. **Reads** the best CE/PE pair from `C:/Ballom_FYR/option_pairs.json` (written by `dev_scanner`), filters to NIFTY only.
3. **Fetches** Fyers historical candles for CE, PE, and NIFTY index across **5 timeframes** (1min, 5min, 15min, 30min, 1hr).
4. **Computes** 6 SHAs (Signal + Trend SHA for CE, PE, IDX) on 1min data.
5. **Computes** RSI across all 5 timeframes for CE, PE, and IDX (15 RSI values total).
6. **Computes** GAP% and SHA Relationship (DIVERGING / CONVERGING / PARALLEL / CLOSE).
7. **Dumps** everything to `signal_state.json` for the dashboard and trading branch.

### What this branch does NOT do

- No trading / order placement (that belongs to `dev_trading`)
- No authentication / TOTP login (that belongs to `dev_scanner`)
- No option pair scanning (that belongs to `dev_scanner`)
- No dashboard serving (that belongs to `dev_front_end`)
- No commodity processing (NIFTY index only)

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      start_app.bat                      │
│            (forever-restart loop, demo/live)             │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                     application.py                      │
│              (main while-loop, every 3s)                │
│                                                         │
│  Step 1: Day-change → reload token + holidays           │
│  Step 2: Load token from scanner's file (read-only)     │
│  Step 3: Holiday / market-hours check                   │
│  Step 4: Read option_pairs.json → filter to NIFTY       │
│  Step 5: Fetch 15 data series in parallel               │
│  Step 6: Compute 6 SHAs (Signal + Trend × CE,PE,IDX)   │
│  Step 7: Compute 15 RSIs (5 TFs × CE,PE,IDX)           │
│  Step 8: Write signal_state.json                        │
└───┬──────────┬───────────┬──────────────────────────────┘
    │          │           │
    ▼          ▼           ▼
fyers_api   indicator   state_writer
   .py        .py          .py
    │          │           │
    │          │           ▼
    │          │   C:/Ballom_FYR/state/<mode>/
    │          │     ├── signal_state.json   ← THIS BRANCH WRITES
    │          │     ├── app_status.json     ← THIS BRANCH WRITES
    │          │     └── strategy_log/
    │          │
    │          └── SmoothedHeikenAshi (SHA v3)
    │              RSI (Wilder's smoothing)
    │
    ▼
C:/Ballom_FYR/fyers_token.json  ← READ ONLY (written by dev_scanner)
C:/Ballom_FYR/option_pairs.json ← READ ONLY (written by dev_scanner)
```

### Multi-branch architecture

| Branch               | Responsibility                            | Token              |
|----------------------|-------------------------------------------|--------------------|
| `dev_scanner`        | Auth + hourly CE/PE pair scanning         | WRITES token       |
| **`dev_updater_nifty`** | **SHA + RSI computation (this branch)** | **READS token**    |
| `dev_trading`        | Order execution, position management      | READS token        |
| `dev_front_end`      | Dashboard (reads state files)             | READS token        |

---

## 3. Data Flow — What Gets Fetched & Computed

### Historical Candle Fetches (15 parallel API calls per cycle)

| # | Symbol | Timeframe | Candles | Purpose |
|---|--------|-----------|---------|---------|
| 1 | CE     | 1min      | 500     | SHA Signal + Trend + 1min RSI |
| 2 | PE     | 1min      | 500     | SHA Signal + Trend + 1min RSI |
| 3 | IDX    | 1min      | 500     | SHA Signal + Trend + 1min RSI |
| 4 | CE     | 5min      | 200     | 5min RSI |
| 5 | PE     | 5min      | 200     | 5min RSI |
| 6 | IDX    | 5min      | 200     | 5min RSI |
| 7 | CE     | 15min     | 100     | 15min RSI |
| 8 | PE     | 15min     | 100     | 15min RSI |
| 9 | IDX    | 15min     | 100     | 15min RSI |
| 10 | CE    | 30min     | 100     | 30min RSI |
| 11 | PE    | 30min     | 100     | 30min RSI |
| 12 | IDX   | 30min     | 100     | 30min RSI |
| 13 | CE    | 60min     | 100     | 1hr RSI |
| 14 | PE    | 60min     | 100     | 1hr RSI |
| 15 | IDX   | 60min     | 100     | 1hr RSI |

### Fyers History API — Market Hours Constraint

NSE Index market hours: **9:15 AM – 3:30 PM IST** (375 minutes/day).

The Fyers API only returns candles during actual trading sessions. So to get N candles:
- **1min × 500 candles**: spans ~1.3 trading days (375 candles/day)
- **5min × 200 candles**: spans ~2.7 trading days (75 candles/day)
- **15min × 100 candles**: spans ~4 trading days (25 candles/day)
- **30min × 100 candles**: spans ~8 trading days (12-13 candles/day)
- **60min × 100 candles**: spans ~16 trading days (6-7 candles/day)

The `fetch_historical_data()` in `fyers_api.py` handles this by:
1. Starting from current time (or last market close if outside hours)
2. Walking backwards, skipping weekends and holidays
3. Computing the correct `range_from` timestamp

**Options (CE/PE)** use `cont_flag=0` (specific contract data).
**Index** uses `cont_flag=1` (continuous data).

### Fyers Supported Timeframes (resolution values)

| Resolution | Period  | Category  |
|------------|---------|-----------|
| `5S`       | 5 sec   | intraday  |
| `10S`      | 10 sec  | intraday  |
| `15S`      | 15 sec  | intraday  |
| `30S`      | 30 sec  | intraday  |
| `45S`      | 45 sec  | intraday  |
| `1`        | 1 min   | intraday  |
| `2`        | 2 min   | intraday  |
| `3`        | 3 min   | intraday  |
| `5`        | 5 min   | intraday  |
| `10`       | 10 min  | intraday  |
| `15`       | 15 min  | intraday  |
| `20`       | 20 min  | intraday  |
| `30`       | 30 min  | intraday  |
| `60`       | 1 hr    | intraday  |
| `120`      | 2 hr    | intraday  |
| `240`      | 4 hr    | intraday  |
| `D`        | 1 day   | daily     |

### Computations

**6 SHAs (1min only):**
| # | Component | SHA Type | Length | MA Type |
|---|-----------|----------|--------|---------|
| 1 | CE Signal | Signal   | 3      | RMA     |
| 2 | PE Signal | Signal   | 3      | RMA     |
| 3 | IDX Signal| Signal   | 3      | RMA     |
| 4 | CE Trend  | Trend    | 6      | RMA     |
| 5 | PE Trend  | Trend    | 6      | RMA     |
| 6 | IDX Trend | Trend    | 6      | RMA     |

**15 RSIs (all timeframes × CE, PE, IDX):**
| Timeframe | CE RSI | PE RSI | IDX RSI |
|-----------|--------|--------|---------|
| 1min      | ✓      | ✓      | ✓       |
| 5min      | ✓      | ✓      | ✓       |
| 15min     | ✓      | ✓      | ✓       |
| 30min     | ✓      | ✓      | ✓       |
| 1hr       | ✓      | ✓      | ✓       |

---

## 4. File Descriptions

### `application.py` — Main entry point

- Parses `demo` / `live` mode from CLI
- Loads token (read-only from scanner's file)
- Infinite loop: fetch data → compute indicators → write JSON
- Key functions:
  - `process_symbol()` — orchestrates 15 parallel fetches, 6 SHAs, 15 RSIs
  - `get_symbol_details()` — Signal SHA (length=3) → power, list, debug
  - `get_trend_details()` — Trend SHA (length=6) → same structure
  - `compute_sha_gap()` — GAP% between Signal and Trend SHA
  - `compute_sha_relationship()` — DIVERGING / CONVERGING / PARALLEL / CLOSE

### `fyers_api.py` — Read-only Fyers API wrapper

- `FyersAPI()` — client that reads scanner's token from `C:/Ballom_FYR/fyers_token.json`
- `load_token()` — load and verify token (3 retries for network flakes)
- `invalidate()` — flags token as `expired` in JSON so scanner re-auths
- `verify_session()` — profile API check + auto-reload from file
- `safe_api_call()` — wraps API calls with auth-error retry
- `fetch_historical_data()` — market-hours aware OHLCV fetch (all Fyers timeframes)
- `fetch_trading_holidays()` / `load_holiday_set()` — NSE holiday calendar

### `indicator.py` — Technical indicators

**SmoothedHeikenAshi (SHA v3):**
- `ma()` — 14 moving average types (SMA, EMA, WMA, RMA, VWMA, DEMA, TEMA, ZLEMA, HMA, ALMA, SMMA, SWMA, LSMA, DONCHIAN)
- `calculate()` — Full SHA pipeline: pre-smooth → Heiken Ashi → post-smooth
- Matches TradingView Pine Script behavior exactly

**RSI:**
- `calculate()` — Wilder's smoothing (ta.rma)
- Matches TradingView's `ta.rsi()` exactly

### `constants.py` — Shared constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `OPTION_PAIRS_JSON` | `C:/Ballom_FYR/option_pairs.json` | Scanner's output |
| `TOKEN_FILE` | `C:/Ballom_FYR/fyers_token.json` | Shared auth token |
| `CACHE_DIR` | `C:/Ballom_FYR/cache/` | Holiday calendar cache |
| `INDICES_START/END` | 09:15 / 15:30 | Market hours |
| `SHA_LENGTH` / `SHA_TREND_LENGTH` | 3 / 6 | Signal vs Trend SHA |
| `SHA_MA_TYPE` / `SHA_TREND_MA_TYPE` | RMA / RMA | MA type for SHA |
| `RSI_PERIOD` | 14 | Wilder's look-back |
| `RSI_TIMEFRAMES` | 1m, 5m, 15m, 30m, 1h | Multi-TF RSI config |
| `UPDATE_INTERVAL` | 3 seconds | Loop frequency |
| `ACTIVE_SYMBOLS` | {"NIFTY"} | Only process NIFTY |

### `state_writer.py` — Atomic JSON state writer

- `write_signal_state()` — upserts per-symbol SHA + RSI data
- `write_app_status()` — updater lifecycle status
- `log_strategy_event()` — date-partitioned event log
- All writes atomic (temp + shutil.move)

### `start_app.bat` — Launcher

- `start_app.bat [demo|live]` (defaults to `demo`)
- Forever-restart loop with 10s cooldown on crash
- Auto-installs dependencies on first run

---

## 5. Output: `signal_state.json`

Written to `C:/Ballom_FYR/state/<mode>/signal_state.json`:

```json
{
  "NIFTY": {
    "timestamp": "2026-03-17 10:15:30",
    "ce_symbol": "NSE:NIFTY26MAR23500CE",
    "pe_symbol": "NSE:NIFTY26MAR23000PE",
    "underlying": "NSE:NIFTY50-INDEX",
    "market_type": "INDEX",

    "ce":  {"power": 5, "list": [1,1,0,1,1,0,1], "sha": [...]},
    "pe":  {"power": 3, "list": [0,0,1,1,0,1,0], "sha": [...]},
    "idx": {"power": 6, "list": [1,1,1,0,1,1,1], "sha": [...]},
    "idx_trend": "BULLISH",

    "ce_trend":     {"power": 4, "list": [...], "sha": [...]},
    "pe_trend":     {"power": 2, "list": [...], "sha": [...]},
    "idx_trend_sha": {"power": 5, "list": [...], "sha": [...]},

    "ce_gap":  {"gap_pct": 1.2, "signal_mean": 250.5, "trend_mean": 247.5, "per_candle": [...]},
    "pe_gap":  {"gap_pct": -0.8, ...},
    "idx_gap": {"gap_pct": 0.3, ...},

    "ce_relationship":  {"status": "DIVERGING", "strength": 0.42, "avg_gap": 1.2, "delta": 2.1},
    "pe_relationship":  {"status": "CONVERGING", ...},
    "idx_relationship": {"status": "CLOSE", ...},

    "rsi": {
      "1m":  {"ce": 45.2, "pe": 55.3, "idx": 50.1},
      "5m":  {"ce": 42.0, "pe": 58.0, "idx": 48.5},
      "15m": {"ce": 40.0, "pe": 60.2, "idx": 46.3},
      "30m": {"ce": 38.5, "pe": 62.1, "idx": 44.8},
      "1h":  {"ce": 35.0, "pe": 65.0, "idx": 42.0}
    },

    "ce_rsi": 45.2,
    "pe_rsi": 55.3,
    "idx_rsi": 50.1,
    "ce_rsi_5m": 42.0,
    "pe_rsi_5m": 58.0,
    "ce_rsi_15m": 40.0,
    "pe_rsi_15m": 60.2
  }
}
```

### SHA Debug Format (per candle, last 7)

```json
{
  "ts": "2026-03-17 10:14:00",
  "O": 250.35,
  "H": 251.80,
  "L": 249.90,
  "C": 251.45,
  "dir": "BULL"
}
```

- `dir` = `"BULL"` if `(C - O) / |H - L| >= 0`, else `"BEAR"`
- `power` = count of BULL candles in last 7
- `list` = `[1|0, ...]` most-recent-first

---

## 6. Token Management

```
  Updater loads token from C:/Ballom_FYR/fyers_token.json
         │
         ├─ Token valid? ─── YES ──→ Build FyersModel, proceed
         │
         └─ Token invalid? ─── flag "expired": true in JSON
                                     │
                                     ▼
                              dev_scanner detects flag
                              on its next heartbeat →
                              re-authenticates → writes
                              fresh token → updater picks
                              it up on next cycle
```

**Key rules:**
- Updater NEVER does TOTP login (prevents token invalidation cascade)
- Anti-collision jitter: 2–10s random wait before retry (staggers multiple updaters)
- Day-change grace: accepts yesterday's token if it still verifies against server
- 3-retry verification with 2s escalating backoff

---

## 7. Dependencies on Other Branches

### This branch reads:

| File | Producer | Purpose |
|------|----------|---------|
| `fyers_token.json` | `dev_scanner` | Auth token (read-only) |
| `option_pairs.json` | `dev_scanner` | CE/PE pair selection |

### This branch writes:

| File | Consumer | Purpose |
|------|----------|---------|
| `signal_state.json` | `dev_trading`, `dev_front_end` | SHA analysis + RSI data |
| `app_status.json` | `dev_front_end` | Updater lifecycle |
| `strategy_log/` | `dev_front_end` | Event logs |

---

## 8. How to Run

### Prerequisites

1. **dev_scanner** must be running (writes token + option_pairs.json)
2. Python 3.11+ installed
3. Dependencies: `pip install -r requirements_fyers.txt`

### Quick start

```batch
cd C:\Users\sings213\fyers\Ballom_Fyers
start_app.bat demo
```

### Live mode

```batch
start_app.bat live
```

### Direct Python

```bash
python application.py demo
python application.py live
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Waiting for dev_scanner to write token" | Scanner not running or auth failed | Start dev_scanner branch first |
| "Token expired mid-session — flagged" | Fyers invalidated token (single-session policy) | Scanner will re-auth automatically |
| "option_pairs.json not found" | Scanner hasn't scanned yet | Wait for scanner's hourly scan |
| "PROCESS_ERROR" in logs | API returned bad data or insufficient candles | Check candle counts in strategy_log |
| All RSIs show `null` | Insufficient candles for RSI period (14) | Verify market is open and options are liquid |
| SHA values all NaN | Insufficient candles for SHA warmup | 500 candles need ~1.3 trading days of data |
| Data quality warnings | Fyers API returned unsorted/duplicate candles | Auto-fixed by defensive sort+dedup |

---

## 10. Python Dependencies

From `requirements_fyers.txt`:

| Package | Version | Purpose |
|---------|---------|---------|
| `fyers-apiv3` | 3.1.7 | Fyers broker SDK (FyersModel, history API) |
| `pandas` | 2.2.0 | DataFrame for OHLCV candle data |
| `numpy` | latest | Numerical computation (SHA, RSI) |
| `requests` | (transitive) | HTTP calls for holiday API |

---

## 11. Key Design Decisions

1. **Read-only auth** — Updater never does TOTP login. Flags `expired: true` in token file for scanner to re-auth. Prevents token collision across branches.
2. **15 parallel fetches** — Uses `ThreadPoolExecutor(max_workers=15)` to fetch all 5 timeframes × 3 symbols simultaneously. Reduces cycle time from ~45s sequential to ~3-5s parallel.
3. **3-second update cycle** — Near real-time updates during market hours. Configurable via `UPDATE_INTERVAL` in constants.py.
4. **SHA on 1min only** — Signal SHA (length=3) and Trend SHA (length=6) computed on 1-minute candles only, as these are the most responsive to price action.
5. **RSI on all 5 timeframes** — Gives multi-timeframe confluence: 1min for scalping, 5min for intraday, 15min/30min for swing, 1hr for position.
6. **Backward-compatible RSI fields** — `signal_state.json` includes both the new structured `rsi` dict and legacy flat fields (`ce_rsi`, `pe_rsi_5m`, etc.) for dashboard compatibility.
7. **Atomic writes** — All JSON writes use temp+rename to prevent partial reads by dashboard/trading branches.
8. **Market-hours aware history** — Walking backwards through trading days only (skipping weekends, holidays, respecting special sessions like Muhurat trading).
