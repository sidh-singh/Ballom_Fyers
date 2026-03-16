"""
state_writer.py — Atomic JSON state-file management for the dashboard.

All state files live under  C:/Ballom_FYR/state/<mode>/  and are written
atomically (temp + rename) so the Dash dashboard never reads a
half-written file.

Call ``configure(mode)`` once at startup before any writes.
Demo and live modes write to separate directories.

State files (updater writes)
─────────────────────────────
  app_status.json     — updater lifecycle status
  signal_state.json   — per-symbol SHA analysis + multi-TF RSI

State files (read by updater, written by other branches)
─────────────────────────────────────────────────────────
  position_state.json — open positions snapshot (written by dev_trading)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from constants import STATE_DIR_DEMO, get_state_dir

# ── module-level state (set via configure) ─────────────────────────────────────
_mode: str = "demo"
STATE_DIR: Path = STATE_DIR_DEMO

APP_STATUS_FILE: Path     = STATE_DIR / "app_status.json"
SIGNAL_STATE_FILE: Path   = STATE_DIR / "signal_state.json"
STRATEGY_LOG_FILE: Path   = STATE_DIR / "strategy_log.json"
STRATEGY_LOG_DIR: Path    = STATE_DIR / "strategy_log"

# Max strategy-log entries per date (FIFO)
_MAX_LOG_ENTRIES = 500

# How many days of logs to retain
_MAX_LOG_DAYS = 14


def configure(mode: str) -> None:
    """
    Set the active mode ('demo' or 'live') — must be called once at startup.
    """
    global _mode, STATE_DIR
    global APP_STATUS_FILE, SIGNAL_STATE_FILE
    global STRATEGY_LOG_FILE, STRATEGY_LOG_DIR

    _mode = mode.lower()
    STATE_DIR = get_state_dir(_mode)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    APP_STATUS_FILE     = STATE_DIR / "app_status.json"
    SIGNAL_STATE_FILE   = STATE_DIR / "signal_state.json"
    STRATEGY_LOG_FILE   = STATE_DIR / "strategy_log.json"
    STRATEGY_LOG_DIR    = STATE_DIR / "strategy_log"
    STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_current_mode() -> str:
    return _mode


# ═══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _write_json_atomic(path: Path, data: Any) -> None:
    """Write *data* to *path* atomically via temp-file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        shutil.move(tmp, str(path))
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════════
#  APP STATUS  (updater lifecycle)
# ═══════════════════════════════════════════════════════════════════════════════

def write_app_status(
    mode: str,
    current_day: str,
    in_indices_window: bool = False,
    indices_scanned: bool = False,
    status: str = "running",
    message: str = "",
) -> None:
    _write_json_atomic(APP_STATUS_FILE, {
        "timestamp": _ts(),
        "mode": mode,
        "branch": "dev_updater_nifty",
        "current_day": current_day,
        "in_indices_window": in_indices_window,
        "indices_scanned": indices_scanned,
        "status": status,
        "message": message,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL STATE  (per-symbol SHA analysis + multi-TF RSI)
# ═══════════════════════════════════════════════════════════════════════════════

def write_signal_state(
    symbol_key: str,
    ce_symbol: str,
    pe_symbol: str,
    underlying: str,
    # ── Signal SHA (1min) ─────────────────────────────────────────────
    ce_power: int,
    ce_list: list,
    pe_power: int,
    pe_list: list,
    idx_power: int,
    idx_list: list,
    ce_sha_debug: list | None = None,
    pe_sha_debug: list | None = None,
    idx_sha_debug: list | None = None,
    # ── Trend SHA (1min) ──────────────────────────────────────────────
    ce_trend_power: int = 0,
    ce_trend_list: list | None = None,
    pe_trend_power: int = 0,
    pe_trend_list: list | None = None,
    idx_trend_power: int = 0,
    idx_trend_list: list | None = None,
    ce_trend_sha_debug: list | None = None,
    pe_trend_sha_debug: list | None = None,
    idx_trend_sha_debug: list | None = None,
    # ── GAP% between Signal SHA and Trend SHA ─────────────────────────
    ce_gap: dict | None = None,
    pe_gap: dict | None = None,
    idx_gap: dict | None = None,
    # ── SHA Relationship (DIVERGING / CONVERGING / PARALLEL / CLOSE) ──
    ce_relationship: dict | None = None,
    pe_relationship: dict | None = None,
    idx_relationship: dict | None = None,
    # ── RSI multi-timeframe ───────────────────────────────────────────
    rsi_data: dict | None = None,
    market_type: str = "INDEX",
) -> None:
    """
    Upsert one symbol's full signal data.

    *rsi_data* is a dict keyed by timeframe label:
        {
            "1m":  {"ce": 45.2, "pe": 55.3, "idx": 50.1},
            "5m":  {"ce": 42.0, "pe": 58.0, "idx": 48.0},
            "15m": {"ce": 40.0, "pe": 60.0, "idx": 46.0},
            "30m": {"ce": 38.0, "pe": 62.0, "idx": 44.0},
            "1h":  {"ce": 35.0, "pe": 65.0, "idx": 42.0},
        }
    """
    rsi_data = rsi_data or {}

    data = _read_json(SIGNAL_STATE_FILE)
    data[symbol_key] = {
        "timestamp": _ts(),
        "ce_symbol": ce_symbol,
        "pe_symbol": pe_symbol,
        "underlying": underlying,
        "market_type": market_type,

        # Signal SHA (1min)
        "ce": {"power": ce_power, "list": ce_list,
               "sha": ce_sha_debug or []},
        "pe": {"power": pe_power, "list": pe_list,
               "sha": pe_sha_debug or []},
        "idx": {"power": idx_power, "list": idx_list,
                "sha": idx_sha_debug or []},
        "idx_trend": "BULLISH" if idx_list and idx_list[0] == 1 else "BEARISH",

        # Trend SHA (1min)
        "ce_trend": {"power": ce_trend_power, "list": ce_trend_list or [],
                     "sha": ce_trend_sha_debug or []},
        "pe_trend": {"power": pe_trend_power, "list": pe_trend_list or [],
                     "sha": pe_trend_sha_debug or []},
        "idx_trend_sha": {"power": idx_trend_power, "list": idx_trend_list or [],
                          "sha": idx_trend_sha_debug or []},

        # GAP%
        "ce_gap": ce_gap or {},
        "pe_gap": pe_gap or {},
        "idx_gap": idx_gap or {},

        # SHA Relationship
        "ce_relationship": ce_relationship or {},
        "pe_relationship": pe_relationship or {},
        "idx_relationship": idx_relationship or {},

        # RSI multi-timeframe (1m, 5m, 15m, 30m, 1h)
        "rsi": rsi_data,

        # ── Legacy flat RSI fields (backward compatibility) ────────────
        "ce_rsi": rsi_data.get("1m", {}).get("ce"),
        "pe_rsi": rsi_data.get("1m", {}).get("pe"),
        "idx_rsi": rsi_data.get("1m", {}).get("idx"),
        "ce_rsi_5m": rsi_data.get("5m", {}).get("ce"),
        "pe_rsi_5m": rsi_data.get("5m", {}).get("pe"),
        "ce_rsi_15m": rsi_data.get("15m", {}).get("ce"),
        "pe_rsi_15m": rsi_data.get("15m", {}).get("pe"),
    }
    _write_json_atomic(SIGNAL_STATE_FILE, data)


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY LOG  (rolling decision log)
# ═══════════════════════════════════════════════════════════════════════════════

def log_strategy_event(
    symbol_key: str,
    leg: str,
    action: str,
    qty: int = 0,
    pl: float = 0.0,
    details: str = "",
) -> None:
    """
    Append a strategy/updater event to the date-partitioned log.

    Logs stored as  strategy_log/YYYY-MM-DD.json  so each trading
    day's events are preserved.  Old files beyond _MAX_LOG_DAYS pruned.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = STRATEGY_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{today}.json"

    entries = _read_json(log_file)
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "timestamp": _ts(),
        "date": today,
        "symbol": symbol_key,
        "leg": leg,
        "action": action,
        "qty": qty,
        "pl": pl,
        "details": details,
    })
    if len(entries) > _MAX_LOG_ENTRIES:
        entries = entries[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(log_file, entries)

    # Legacy single file
    legacy = _read_json(STRATEGY_LOG_FILE)
    if not isinstance(legacy, list):
        legacy = []
    legacy.append(entries[-1])
    if len(legacy) > _MAX_LOG_ENTRIES:
        legacy = legacy[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(STRATEGY_LOG_FILE, legacy)

    _cleanup_old_strategy_logs()


def _cleanup_old_strategy_logs() -> None:
    """Remove date-partitioned strategy log files older than _MAX_LOG_DAYS."""
    from datetime import timedelta

    log_dir = STRATEGY_LOG_DIR
    if not log_dir.exists():
        return

    cutoff = datetime.now().date() - timedelta(days=_MAX_LOG_DAYS)
    for f in log_dir.glob("*.json"):
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d").date()
            if file_date < cutoff:
                f.unlink()
        except (ValueError, OSError):
            continue
