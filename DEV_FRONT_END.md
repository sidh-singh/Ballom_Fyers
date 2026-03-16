# DEV_FRONT_END — Dashboard Branch

## Overview

The `dev_front_end` branch is the **visualization layer** of the Ballom FYR system.
It provides a real-time Dash/Plotly web dashboard that reads JSON state files
written by all other branches and displays them in a unified, glassmorphism-styled UI.

**This branch is strictly read-only.** It never writes to state files, never
places orders, and never performs SHA/RSI computation.

## Branch Dependency Chain

```
dev_scanner → dev_updater_nifty → dev_trading → DEV_FRONT_END (this)
   (auth)       (SHA + RSI)        (trading)      (dashboard)
```

## Architecture

### Data Flow

```
┌──────────────────────────────────────────────────────────────┐
│  C:/Ballom_FYR/state/<mode>/                                 │
│                                                              │
│  signal_state.json     ← dev_updater_nifty (SHA + RSI)      │
│  position_state.json   ← dev_trading (open positions)        │
│  account_state.json    ← dev_trading (balance, P&L)          │
│  strategy_log.json     ← dev_trading (strategy decisions)    │
│  profit_history.json   ← dev_trading (P&L time-series)       │
│  position_tracker.json ← dev_trading (daily booked profit)   │
│  app_status.json       ← dev_scanner / dev_trading           │
│                                                              │
│        ▼ READ ONLY ▼                                         │
│                                                              │
│  ┌──────────────────┐                                        │
│  │   dashboard.py    │ → http://127.0.0.1:8050               │
│  └──────────────────┘                                        │
└──────────────────────────────────────────────────────────────┘
```

### Dashboard Sections

| Section               | Data Source             | Description                                      |
|----------------------|-------------------------|--------------------------------------------------|
| KPI Cards            | account_state.json      | Balance, Realized P&L, Unrealized P&L, Total P&L|
| Daily Stats          | position_tracker.json   | Today's Booked Profit, Avg/Close, Closes, Martingales|
| Profit History Chart | profit_history.json     | Plotly spline chart with close stars, martingale diamonds|
| Open Positions       | position_state.json     | DataTable of active positions (qty, avg, ltp, P&L)|
| Traded Positions     | profit_history.json     | ENTRY→CLOSE pairs for the selected date          |
| SHA Signal Analysis  | signal_state.json       | Signal + Trend SHA (power bars, candle dots)      |
| SHA Analysis         | signal_state.json       | GAP% + Relationship (DIVERGING/CONVERGING/etc.)   |
| RSI Multi-Timeframe  | signal_state.json       | 5 timeframes (1m, 5m, 15m, 30m, 1h) for CE/PE/IDX|
| Strategy Log         | strategy_log.json       | Rolling log with action badges and hover popups   |

### Signal State JSON Structure (from dev_updater_nifty)

```json
{
  "NIFTY": {
    "ce": {"power": 5, "list": [1,1,0,1,1,1,1], "sha": "BULLISH"},
    "pe": {"power": 3, "list": [0,1,1,0,0,1,0], "sha": "BEARISH"},
    "idx": {"power": 4, "list": [1,0,1,1,0,1,1], "sha": "BULLISH"},
    "ce_trend": {"power": 6, "list": [...], "sha": "BULLISH"},
    "pe_trend": {"power": 2, "list": [...], "sha": "BEARISH"},
    "idx_trend_sha": {"power": 5, "list": [...], "sha": "BULLISH"},
    "ce_gap": {"gap_pct": 1.25, "signal_sha": "BULLISH", "trend_sha": "BULLISH"},
    "pe_gap": {"gap_pct": -0.85, ...},
    "idx_gap": {"gap_pct": 0.42, ...},
    "ce_relationship": {"status": "CONVERGING", "strength": 0.8, "avg_gap": 1.1, "delta": -0.3},
    "pe_relationship": {...},
    "idx_relationship": {...},
    "rsi": {
      "1m": {"ce": 45.2, "pe": 62.1, "idx": 51.0},
      "5m": {"ce": 38.5, "pe": 55.8, "idx": 48.2},
      "15m": {"ce": 42.1, "pe": 58.3, "idx": 50.5},
      "30m": {"ce": 40.0, "pe": 60.2, "idx": 49.8},
      "1h": {"ce": 35.5, "pe": 65.0, "idx": 52.1}
    },
    "ce_rsi": 45.2, "pe_rsi": 62.1,
    "ce_rsi_5m": 38.5, "pe_rsi_5m": 55.8,
    "ce_rsi_15m": 42.1, "pe_rsi_15m": 58.3,
    "idx_trend": "BULLISH",
    "ce_symbol": "NSE:NIFTY25JUNCE24000",
    "pe_symbol": "NSE:NIFTY25JUNPE24000",
    "timestamp": "2025-06-15 10:30:00"
  }
}
```

## Files

| File                 | Lines | Purpose                                           |
|---------------------|-------|---------------------------------------------------|
| dashboard.py        | ~1650 | Dash web app — layout, callbacks, chart builder    |
| constants.py        | ~55   | Paths, display settings, SHA/RSI thresholds        |
| symbols.json        | ~15   | Symbol configuration (NIFTY indices)               |
| start_dashboard.bat | ~60   | Windows launcher (auto-installs dash if needed)    |

## constants.py — Key Settings

| Constant             | Value                        | Description                        |
|---------------------|------------------------------|------------------------------------|
| STATE_DIR_BASE      | C:/Ballom_FYR/state          | Root state directory               |
| STATE_DIR_DEMO      | C:/Ballom_FYR/state/demo     | Demo mode state files              |
| STATE_DIR_LIVE      | C:/Ballom_FYR/state/live     | Live mode state files              |
| DASHBOARD_PORT      | 8050                         | Default HTTP port                  |
| DASHBOARD_REFRESH_MS| 2000                         | Auto-refresh interval (ms)         |
| SHA_LENGTH          | 3                            | Signal SHA candle count displayed   |
| SHA_TREND_LENGTH    | 6                            | Trend SHA candle count displayed    |
| RSI_OVERSOLD        | 30                           | RSI oversold threshold             |
| RSI_OVERBOUGHT      | 70                           | RSI overbought threshold           |
| GAP_RANGE_LOW       | 0.5                          | GAP% narrow range boundary         |
| GAP_RANGE_HIGH      | 2.5                          | GAP% wide range boundary           |

## Mode Toggle

The dashboard supports switching between **DEMO** and **LIVE** modes:

- **Auto-detect:** Compares `app_status.json` timestamps between demo/live directories
- **iOS-style toggle:** Click to switch modes without page reload
- **CLI override:** Pass `demo` or `live` as first argument

## Profit Chart Features

- Plotly spline chart with area fill
- **Close events:** Star markers (⭐)
- **Martingale adds:** Diamond markers (💎)
- **Range slider** for time-window zoom
- **Date selector dropdown** showing up to 7 recent trading days
- **Demo fallback:** Loads from `C:/Ballom_FYR/demo/demo_trade_history.json`

## Visual Design

- **Theme:** Glassmorphism dark (deep void black #050810)
- **Fonts:** Inter (UI) + JetBrains Mono (numbers/code)
- **Animations:** fadeIn, shimmer gradient bar, live dot pulse
- **Cards:** Blur-backdrop glass panels with subtle glow borders
- **Color system:** Purple accents (#7c3aed), teal positive (#00c4a0), crimson negative (#ff4444)

## Launch

```bash
# Auto-detect mode, default port 8050
python dashboard.py

# Force demo mode
python dashboard.py demo

# Live mode on custom port
python dashboard.py live 8060

# Or use the batch file
start_dashboard.bat
start_dashboard.bat demo 8060
```

## Dependencies

- `dash` — Web framework (auto-installed by batch file)
- `plotly` — Chart library (installed with dash)
- Python 3.11+ standard library (`json`, `pathlib`, `datetime`)
