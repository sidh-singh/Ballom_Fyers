"""
state_writer.py — Atomic JSON state-file management for the dashboard.

All state files live under  C:/Ballom_FYR/state/<mode>/  and are written
atomically (temp + rename) so the Dash dashboard never reads a
half-written file.

Call ``configure(mode)`` once at startup before any writes.
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
POSITION_STATE_FILE: Path = STATE_DIR / "position_state.json"
ACCOUNT_STATE_FILE: Path  = STATE_DIR / "account_state.json"
STRATEGY_LOG_FILE: Path   = STATE_DIR / "strategy_log.json"
STRATEGY_LOG_DIR: Path    = STATE_DIR / "strategy_log"

_MAX_LOG_ENTRIES = 500
_MAX_LOG_DAYS = 14


def configure(mode: str) -> None:
    """Set the active mode ('demo' or 'live')."""
    global _mode, STATE_DIR
    global APP_STATUS_FILE, SIGNAL_STATE_FILE, POSITION_STATE_FILE
    global ACCOUNT_STATE_FILE, STRATEGY_LOG_FILE, STRATEGY_LOG_DIR

    _mode = mode.lower()
    STATE_DIR = get_state_dir(_mode)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    APP_STATUS_FILE     = STATE_DIR / "app_status.json"
    SIGNAL_STATE_FILE   = STATE_DIR / "signal_state.json"
    POSITION_STATE_FILE = STATE_DIR / "position_state.json"
    ACCOUNT_STATE_FILE  = STATE_DIR / "account_state.json"
    STRATEGY_LOG_FILE   = STATE_DIR / "strategy_log.json"
    STRATEGY_LOG_DIR    = STATE_DIR / "strategy_log"
    STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_current_mode() -> str:
    return _mode


# ═══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _write_json_atomic(path: Path, data: Any) -> None:
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
#  APP STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def write_app_status(
    mode: str,
    current_day: str,
    in_indices_window: bool = False,
    status: str = "running",
    message: str = "",
) -> None:
    _write_json_atomic(APP_STATUS_FILE, {
        "timestamp": _ts(),
        "mode": mode,
        "current_day": current_day,
        "in_indices_window": in_indices_window,
        "status": status,
        "message": message,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION STATE
# ═══════════════════════════════════════════════════════════════════════════════

def write_position_state(position_rows: list[dict], overall: dict) -> None:
    _write_json_atomic(POSITION_STATE_FILE, {
        "timestamp": _ts(),
        "positions": position_rows,
        "overall": overall,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  ACCOUNT STATE
# ═══════════════════════════════════════════════════════════════════════════════

def write_account_state(
    balance: float,
    utilized: float,
    realized_pnl: float,
    unrealized_pnl: float,
    total_trades: int = 0,
    winning_trades: int = 0,
    losing_trades: int = 0,
) -> None:
    _write_json_atomic(ACCOUNT_STATE_FILE, {
        "timestamp": _ts(),
        "balance": round(balance, 2),
        "utilized": round(utilized, 2),
        "available": round(balance - utilized, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": round((winning_trades / max(total_trades, 1)) * 100, 2),
    })


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
    """Append a strategy decision to the date-partitioned log."""
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

    # Legacy single file for backward compatibility
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
    cutoff = datetime.now() - timedelta(days=_MAX_LOG_DAYS)
    try:
        for f in STRATEGY_LOG_DIR.glob("*.json"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink(missing_ok=True)
            except ValueError:
                continue
    except Exception:
        pass
