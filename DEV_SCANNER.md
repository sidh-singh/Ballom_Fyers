# DEV_SCANNER — Branch Documentation

> **Branch:** `dev_scanner`
> **Purpose:** Fyers authentication + hourly CE/PE option-pair scanning for NIFTY index
> **Last updated:** 2025-01-XX

---

## 1. Overview

This branch runs a **24×7 infinite-loop service** that:

1. **Authenticates** with the Fyers API using TOTP-based login (token persisted on `C:` drive so other branches can share it).
2. **Scans** for the optimal CE (Call) and PE (Put) option pair for the NIFTY index **every hour** during market hours (09:15 – 15:30 IST).
3. **Writes** the result to `C:/Ballom_FYR/option_pairs.json` for the `dev_trading` branch to consume.
4. **Skips** scanning for any symbol that already has an open trading position (read from `dev_trading`'s `position_state.json`).

### What this branch does NOT do

- No trading / order placement
- No SHA / RSI / GAP calculations (those belong to `dev_updater_nifty`)
- No dashboard serving (that belongs to `dev_front_end`)
- No commodity scanning (index only)

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
│                  (main while-loop)                       │
│                                                         │
│  Step 1: Day-change → re-auth + re-download CSV         │
│  Step 2: Auth health check / retry                      │
│  Step 3: Holiday check                                  │
│  Step 4: Hourly scan trigger                            │
│  Step 5: Write option_pairs.json                        │
│  Step 6: Sleep 30 s & repeat                            │
└───┬───────────┬────────────┬────────────────────────────┘
    │           │            │
    ▼           ▼            ▼
fyers_auth  fyers_api    state_writer
   .py        .py           .py
    │           │            │
    │           │            ▼
    │           │     C:/Ballom_FYR/state/<mode>/
    │           │       ├── app_status.json
    │           │       ├── position_state.json  (read-only, written by dev_trading)
    │           │       └── strategy_log/YYYY-MM-DD/
    │           │
    │           ▼
    │     C:/Ballom_FYR/cache/
    │       ├── nse_fo_symbols_YYYY-MM-DD.csv
    │       └── fyers_holidays_YYYY.json
    │
    ▼
C:/Ballom_FYR/fyers_token.json
```

### Multi-branch architecture

| Branch            | Responsibility                               | Reads from scanner          |
|-------------------|----------------------------------------------|-----------------------------|
| `dev_scanner`     | Auth + hourly CE/PE pair scanning            | —                           |
| `dev_updater_nifty` | SHA, RSI, GAP calculations                | `option_pairs.json`         |
| `dev_trading`     | Order execution, position management          | `option_pairs.json`         |
| `dev_front_end`   | Dashboard (reads state files)                 | `app_status.json`, logs     |

All branches share the **same token file** at `C:/Ballom_FYR/fyers_token.json`. Only `dev_scanner` writes the token; other branches use `read_only=True`.

---

## 3. File descriptions

### `application.py` — Main entry point

- Parses `demo` / `live` mode from CLI argument
- Configures state writer for the chosen mode
- Instantiates `FyersAuth` (primary auth writer) and `FyersAPI`
- Runs the infinite while-loop with all scanning logic
- Key functions:
  - `scan_index_pairs()` — iterates NIFTY symbols, calls `FyersAPI.fetch_option_pair()`, writes results
  - `is_trading_day()` — skips weekends and NSE holidays (respects special Saturday sessions)
  - `get_symbols_with_open_positions()` — reads `position_state.json` from dev_trading
  - `daily_setup()` — auth + CSV download + holiday fetch (once per day)

### `fyers_auth.py` — Authentication manager

- TOTP-based login flow: `send_login_otp → verify_otp → verify_pin → auth_code → generate_token`
- Token persistence: `C:/Ballom_FYR/fyers_token.json`
  ```json
  {
    "access_token": "...",
    "date": "2025-01-15",
    "created_at": "2025-01-15T09:12:34.567890",
    "expired": false
  }
  ```
- Automatic re-auth on `expired: true` or new trading day
- `safe_api_call()` — wraps any Fyers SDK method with auth-error detection + re-auth retry
- Background heartbeat thread (5-minute interval) to detect server-side invalidation
- Exponential backoff + jitter on retries (5 attempts, 10 s base, 120 s cap)
- TOTP window-edge avoidance (waits if < 5 s remaining in current TOTP window)
- Thread-safe via `threading.Lock`

**Credentials** (hardcoded in class):
| Key | Value |
|-----|-------|
| FY_ID | YS07018 |
| APP_ID | OUDS3XQTRU |
| REDIRECT_URI | https://trade.fyers.in/api-login/redirect-uri/index.html |
| PIN | 0000 |
| TOTP_KEY | (base32 secret) |

### `fyers_api.py` — Market data & option-pair scanning

- `FyersAPI(auth)` — takes a `FyersAuth` instance, delegates API calls through `safe_api_call`
- Key methods:

| Method | Type | Purpose |
|--------|------|---------|
| `download_option_data()` | static | Download + cache NSE F&O symbol CSV (daily) |
| `get_lot_size(symbol, df)` | static | Look up lot size from cached CSV |
| `fetch_option_pair(underlying, asset_type)` | instance | **Core scanner** — scores CE/PE options using 7 weighted criteria |
| `fetch_historical_data(symbol, tf, candles)` | instance | Fetch OHLCV candles (market-hours aware) |
| `fetch_trading_holidays(year)` | static | Download + cache NSE holiday list |
| `load_holiday_set(year)` | static | Parse holidays into `(set[str], list[dict])` |
| `_count_available_candles(symbol, tf, candles)` | instance | Test if enough history exists for a symbol |

**Option pair scoring weights:**
| Criterion | Weight |
|-----------|--------|
| Moneyness (ATM proximity) | 25% |
| Bid-ask efficiency | 15% |
| Affordability (premium) | 15% |
| Theta (time value) | 10% |
| OI buildup | 15% |
| Price momentum | 10% |
| Volume | 10% |

### `constants.py` — Shared constants

Defines file paths, trading windows, and column definitions used across all modules:

| Constant | Value | Used by |
|----------|-------|---------|
| `SYMBOLS_JSON` | `./symbols.json` | application.py |
| `OPTION_PAIRS_JSON` | `C:/Ballom_FYR/option_pairs.json` | application.py |
| `TOKEN_DIR` / `TOKEN_FILE` | `C:/Ballom_FYR/` / `fyers_token.json` | fyers_auth.py |
| `CACHE_DIR` | `C:/Ballom_FYR/cache/` | fyers_api.py |
| `STATE_DIR_BASE` | `C:/Ballom_FYR/state/` | state_writer.py |
| `INDICES_START` | `09:15` | application.py |
| `INDICES_END` | `15:30` | application.py |
| `MIN_CANDLES_FOR_ANALYSIS` | `200` | fyers_api.py |
| `STRATEGY_HEDGE_INDEX` | `500` (₹) | application.py |
| `SYMBOLS_COLS` | 21-column list | fyers_api.py |

### `state_writer.py` — Atomic JSON state-file manager

- `configure(mode)` — set state directory to `demo/` or `live/`
- `write_app_status()` — scanner health / progress
- `write_position_state()` — written by dev_trading (scanner only reads)
- `log_strategy_event()` — date-partitioned log files under `strategy_log/`
- All writes are atomic (temp file + `shutil.move`)

### `symbols.json` — Symbol configuration

```json
{
  "indices": [
    {
      "symbol": "NIFTY",
      "indices": "NSE:NIFTY50-INDEX",
      "qty_times": 1,
      "hedge": 500
    }
  ],
  "max_active_commodities": 1,
  "max_balance_usage": 100000,
  "brake": 0
}
```

| Field | Purpose |
|-------|---------|
| `symbol` | Short key used in logs & output JSON |
| `indices` | Fyers symbol identifier for the underlying index |
| `qty_times` | Multiplier applied to lot size |
| `hedge` | Hedge amount in ₹ (default fallback: `STRATEGY_HEDGE_INDEX = 500`) |
| `brake` | If `1`, scanning is halted (safety kill-switch) |

### `start_app.bat` — Launcher

- Usage: `start_app.bat [demo|live]` (defaults to `demo`)
- Auto-installs dependencies on first run (creates `deps_installed.flag`)
- Forever-restart loop with 10 s cooldown on crash
- Live mode shows safety warning
- Python path configurable via `PYTHON_ROOT` variable (falls back to system `python` if not found)

---

## 4. Auth flow (detailed)

```
 ┌──────────────┐
 │ Start        │
 └──────┬───────┘
        │
        ▼
 ┌──────────────────────────────────────────┐
 │ Read C:/Ballom_FYR/fyers_token.json      │
 │ Token exists & date == today & !expired? │
 └──────┬──────────────────────┬────────────┘
     YES│                      │NO
        ▼                      ▼
 ┌──────────────┐     ┌───────────────────────────────┐
 │ Verify token │     │ TOTP Login Flow:              │
 │ (quotes API) │     │  1. send_login_otp(FY_ID)     │
 └──────┬───────┘     │  2. verify_otp(TOTP code)     │
        │             │  3. verify_pin("0000")         │
   PASS │ FAIL        │  4. auth_code → auth URL       │
        │   └─────┐   │  5. SessionModel.generate()    │
        │         ▼   └──────────────┬────────────────┘
        │     Re-auth                │
        │     (same flow) ──────────▶│
        │                            ▼
        │                   ┌──────────────────────────┐
        │                   │ Token verification       │
        │                   │ (3 retries, 5 s apart)   │
        │                   └──────────┬───────────────┘
        │                              │
        ▼                              ▼
 ┌──────────────────────────────────────────┐
 │ Token valid — write to fyers_token.json  │
 │ Start heartbeat thread (5 min interval)  │
 └──────────────────────────────────────────┘
```

API base URLs:
- Login: `https://api-t2.fyers.in/vagator/v2`
- Token: `https://api-t1.fyers.in/api/v3`

---

## 5. Scanning flow (detailed)

```
 ┌────────────────────────────────────────────────┐
 │  Main loop iteration (every 30 s)              │
 └───────────────────┬────────────────────────────┘
                     │
                     ▼
         ┌───────────────────────┐
         │ Is current_hour       │
         │ different from        │  NO → sleep & wait
         │ last_scan_hour?       │──────────────────►
         └───────────┬───────────┘
                     │ YES
                     ▼
         ┌───────────────────────┐
         │ 09:15 ≤ now ≤ 15:30? │  NO → skip (outside market hours)
         └───────────┬───────────┘
                     │ YES
                     ▼
         ┌───────────────────────┐
         │ Read position_state   │
         │ .json (dev_trading)   │
         └───────────┬───────────┘
                     │
                     ▼
    ┌────────────────────────────────────┐
    │ For each index in symbols.json:   │
    │                                    │
    │  1. Skip if open position exists   │
    │  2. fetch_option_pair(underlying)  │
    │     → option chain from Fyers API  │
    │     → score CE/PE using 7 criteria │
    │     → return best pair             │
    │  3. get_lot_size from cached CSV   │
    │  4. qty = lot × qty_times          │
    │  5. Store result                   │
    └────────────────┬───────────────────┘
                     │
                     ▼
    ┌────────────────────────────────────┐
    │ Write option_pairs.json atomically │
    │ Log strategy event                 │
    └────────────────────────────────────┘
```

### Output: `C:/Ballom_FYR/option_pairs.json`

```json
{
    "NIFTY": {
        "CE": "NSE:NIFTY25JAN23500CE",
        "PE": "NSE:NIFTY25JAN23000PE",
        "CE_Strike": 23500,
        "PE_Strike": 23000,
        "Expiry": "2025-01-30",
        "Trend_Score": 0.65,
        "VIX": 14.2,
        "indices": "NSE:NIFTY50-INDEX",
        "qty": 75,
        "hedge": 500
    }
}
```

---

## 6. File paths on `C:` drive

All persistent data lives under `C:/Ballom_FYR/`:

```
C:/Ballom_FYR/
  ├── fyers_token.json          ← auth token (shared across branches)
  ├── option_pairs.json         ← scanner output (consumed by dev_trading)
  ├── cache/
  │   ├── nse_fo_symbols_2025-01-15.csv   ← NSE F&O symbol master (daily)
  │   └── fyers_holidays_2025.json        ← NSE holiday calendar (yearly)
  └── state/
      ├── demo/
      │   ├── app_status.json
      │   ├── position_state.json   ← written by dev_trading
      │   └── strategy_log/
      │       └── 2025-01-15/
      │           └── events.jsonl
      └── live/
          └── (same structure)
```

---

## 7. Dependencies on other branches

### This branch writes (other branches read):

| File | Consumer |
|------|----------|
| `fyers_token.json` | All branches (auth token) |
| `option_pairs.json` | `dev_trading`, `dev_updater_nifty` |
| `app_status.json` | `dev_front_end` (dashboard) |
| `strategy_log/` | `dev_front_end` (dashboard) |

### This branch reads (other branches write):

| File | Producer |
|------|----------|
| `position_state.json` | `dev_trading` — if file missing, scanner assumes no open positions and scans normally |

---

## 8. How to run

### Quick start (demo mode)

```batch
cd C:\Users\sings213\fyers\Ballom_Fyers
start_app.bat demo
```

### Live mode

```batch
start_app.bat live
```

### Direct Python execution

```bash
python application.py demo
python application.py live
```

### First-time setup

1. Ensure Python 3.11+ is installed
2. Install dependencies: `pip install -r requirements_fyers.txt`
3. Verify `symbols.json` has correct index configuration
4. Run `start_app.bat demo` to test

---

## 9. Configuration

### Adding a new index to scan

Edit `symbols.json`:
```json
{
  "indices": [
    {
      "symbol": "NIFTY",
      "indices": "NSE:NIFTY50-INDEX",
      "qty_times": 1,
      "hedge": 500
    },
    {
      "symbol": "BANKNIFTY",
      "indices": "NSE:NIFTYBANK-INDEX",
      "qty_times": 1,
      "hedge": 500
    }
  ]
}
```

### Emergency stop

Set `"brake": 1` in `symbols.json` — the scanner will skip all scanning until it's set back to `0`.

### Scan timing

- Scans trigger **once per hour** when the hour changes
- Window: 09:15 – 15:30 IST (configurable in `constants.py` as `INDICES_START` / `INDICES_END`)
- Poll interval: 30 seconds (`POLL_INTERVAL` in `application.py`)

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Auth fails repeatedly | TOTP key expired or Fyers password changed | Update credentials in `fyers_auth.py` |
| Token file shows `"expired": true` | Server-side invalidation (manual login elsewhere) | Scanner auto-detects and re-auths |
| No option_pairs.json written | Scan returned 0 pairs (illiquid options) | Check `strategy_log/` for details |
| "Outside market hours" all day | System clock wrong or timezone mismatch | Ensure system time is IST |
| position_state.json missing | `dev_trading` branch not running | Scanner scans normally (defaults to no open positions) |
| CSV download fails | NSE website down or URL changed | Check `NSE_FO_URL` in `fyers_api.py` |
| Holiday misdetection | Holiday list not cached for current year | Delete `cache/fyers_holidays_YYYY.json` to force re-fetch |

---

## 11. Python dependencies

From `requirements_fyers.txt`:

| Package | Version | Purpose |
|---------|---------|---------|
| `fyers-apiv3` | 3.1.7 | Fyers broker SDK |
| `pyotp` | 2.9.0 | TOTP code generation |
| `pandas` | 2.2.0 | CSV parsing, option chain analysis |
| `numpy` | latest | Numerical scoring |
| `requests` | (transitive) | HTTP calls for login flow |
| `beautifulsoup4` | 4.14.2 | (available, not currently used by scanner) |
| `scipy` | 1.16.3 | (available, not currently used by scanner) |

---

## 12. Key design decisions

1. **Atomic file writes** — All JSON writes use `tempfile + shutil.move` to prevent partial-read by other branches.
2. **Token sharing** — Single token file on `C:` drive, scanner is the auth writer (`read_only=False`), all other branches read (`read_only=True`).
3. **Hourly scanning** — Scans once per hour (not continuously) to avoid API rate limits and because option prices don't change drastically intra-hour.
4. **Position-aware skipping** — Reads `position_state.json` from dev_trading to avoid rescanning symbols where trades are already open.
5. **Holiday awareness** — Fetches NSE holiday calendar, skips scanning on holidays, respects special Saturday sessions (Muhurat trading etc.).
6. **Forever-restart loop** — Batch file wraps Python process in a loop so crashes auto-recover after 10 s cooldown.
7. **No demo trading** — Fyers does not support paper trading. Demo mode only changes the state directory path (`/state/demo/` vs `/state/live/`).
