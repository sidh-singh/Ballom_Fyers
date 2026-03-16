# DEV_TRADING — Trading Engine Branch

## Overview

The `dev_trading` branch is the **execution layer** of the Ballom FYR system.
It reads pre-computed signals from `dev_updater_nifty` and executes trades
based on the Heiken-Ashi Martingale strategy.

**No SHA or RSI computation happens here.** All signal data is consumed via
`signal_state.json` written by the updater branch.

## Branch Dependency Chain

```
dev_scanner → dev_updater_nifty → DEV_TRADING (this) → dev_front_end
   (auth)       (SHA + RSI)         (trading)           (dashboard)
```

## Architecture

### Single Infinite Loop (`application.py`)

```
┌─────────────────────────────────────────────────┐
│  OUTER LOOP (forever)                           │
│                                                  │
│  1. Day-change → reload token + holidays         │
│  2. Holiday/weekend → sleep 5min                 │
│  3. Trading window (9:15-15:30) → inner loop     │
│  4. Outside window → idle + sleep 1s             │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │  INNER LOOP (per symbol pair)               │ │
│  │                                              │ │
│  │  A. Read signal_state.json (from updater)   │ │
│  │  B. strategy.evaluate(signal_data)          │ │
│  │  C. strategy.execute_orders()               │ │
│  │  D. Monitor positions                       │ │
│  │  E. Day-change → reload token               │ │
│  │  F. All closed → break for fresh pair       │ │
│  └─────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────┘
```

### Token Management

- **Source:** `C:/Ballom_FYR/fyers_token.json` (written by `dev_scanner`)
- **Read-only:** This branch never performs TOTP login
- **On auth failure:** Flags `expired: true` in the token file so `dev_scanner` re-authenticates
- **Daily reload:** Token is reloaded on day-change detection

### Signal Data Flow

- **Source:** `C:/Ballom_FYR/state/<mode>/signal_state.json` (written by `dev_updater_nifty`)
- **Contains:** SHA power/list, trend SHA, GAP%, relationship status, RSI (1m, 5m, 15m)
- **Read every cycle** in the inner loop — always uses latest data

## Files

| File | Purpose |
|------|---------|
| `application.py` | Main entry point — single infinite loop |
| `strategy.py` | Entry/exit/martingale logic (FIXED) |
| `fyers.py` | Fyers API wrapper (token, orders, positions) |
| `demo_fyers.py` | Paper trading simulator (extends Fyers) |
| `constants.py` | Shared enums, paths, tuning params |
| `state_writer.py` | Atomic JSON writes for dashboard |
| `position_tracker.py` | Per-symbol booked profit tracking |
| `pair_manager.py` | CE/PE pair locking with persistence |
| `symbols.json` | Symbol config (NIFTY only) |
| `start_app.bat` | Auto-restart launcher |

## Martingale Fix (KEY CHANGE)

### Old Behavior (Bug)
All three RSI timeframes were checked with **OR** logic at every martingale level:
```python
# OLD — WRONG: 5min oversold could trigger Level 0 add
if mg_level == 0:
    if rsi_1m_oversold OR rsi_5m_oversold OR rsi_15m_oversold:
        → MARTINGALE ADD
```

### New Behavior (Fix)
Each RSI timeframe **independently** triggers its specific martingale level:
```
Level 0 → 1:  ONLY fires when RSI 1min  < 30
Level 1 → 2:  ONLY fires when RSI 5min  < 30
Level 2 → 3:  ONLY fires when RSI 15min < 30
Level 3:      FORCE CLOSE (max level reached)
```

This ensures martingale adds escalate progressively across timeframes,
preventing premature position size increases.

## Configuration

### symbols.json
```json
{
  "indices": [{"symbol": "NIFTY", "index_symbol": "NSE:NIFTY50-INDEX", "qty_times": 1, "hedge": 500}],
  "max_balance_usage": 100000,
  "brake": 0
}
```

### Key Parameters (constants.py)
| Parameter | Value | Description |
|-----------|-------|-------------|
| `STRATEGY_HEDGE_INDEX` | 500 | ₹ profit target per pair |
| `MAX_MARTINGALE_LEVEL` | 3 | Max adds before force close |
| `RSI_OVERSOLD` | 30 | RSI threshold for martingale trigger |
| `GAP_RANGE_LOW` | 0.5 | Min GAP% for entry |
| `GAP_RANGE_HIGH` | 2.5 | Max GAP% for entry |
| `INNER_LOOP_INTERVAL` | 1s | Cycle time between evaluations |

## Running

```bat
REM Paper trading (demo mode)
start_app.bat demo

REM Real trading (live mode)
start_app.bat live
```

Or directly:
```bash
python application.py demo
python application.py live
```

## State Files

All state is written to `C:/Ballom_FYR/state/<mode>/`:

| File | Writer | Description |
|------|--------|-------------|
| `signal_state.json` | dev_updater_nifty | SHA + RSI signals (READ-ONLY) |
| `position_state.json` | dev_trading | Current positions snapshot |
| `account_state.json` | dev_trading | Account balance + P&L |
| `strategy_log.json` | dev_trading | Strategy decision log |
| `active_pairs.json` | dev_trading | Locked CE/PE pairs |
| `position_tracker.json` | dev_trading | Booked profit per symbol |
| `profit_history.json` | dev_trading | Time-series profit snapshots |
| `app_status.json` | dev_trading | App status for dashboard |
