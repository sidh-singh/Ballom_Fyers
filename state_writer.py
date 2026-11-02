"""
state_writer.py — Atomic JSON state writes (LIVE only, trimmed).

The engine writes these; the dashboard (separate process) reads them.
All writes are atomic (temp + os.replace) so the dashboard never sees a
half-written file. Demo/live mode split removed (ARCHITECTURE D5).

Kept lean on purpose — signal/account/position-history writers from the old
multi-branch design are added back only when those features land.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from constants import (
    STATE_DIR, APP_STATUS_FILE, STRATEGY_LOG_DIR, now_ist,
)

_MAX_LOG_ENTRIES = 500
_MAX_LOG_DAYS = 14


def _ts() -> str:
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


# ── app status (engine lifecycle/health) ────────────────────────────────────
def write_app_status(status: str = "running", message: str = "", **extra) -> None:
    data = {"timestamp": _ts(), "status": status, "message": message}
    data.update(extra)
    _write_json_atomic(APP_STATUS_FILE, data)


# ── strategy log (date-partitioned) ─────────────────────────────────────────
def log_strategy_event(symbol: str, leg: str, action: str,
                       qty: int = 0, pl: float = 0.0, details: str = "") -> None:
    today = now_ist().strftime("%Y-%m-%d")
    STRATEGY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = STRATEGY_LOG_DIR / f"{today}.json"

    entries = _read_json(log_file)
    if not isinstance(entries, list):
        entries = []
    entries.append({
        "timestamp": _ts(), "date": today, "symbol": symbol,
        "leg": leg, "action": action, "qty": qty, "pl": pl, "details": details,
    })
    if len(entries) > _MAX_LOG_ENTRIES:
        entries = entries[-_MAX_LOG_ENTRIES:]
    _write_json_atomic(log_file, entries)
    _cleanup_old_logs()


def _cleanup_old_logs() -> None:
    cutoff = (now_ist() - timedelta(days=_MAX_LOG_DAYS)).strftime("%Y-%m-%d")
    if not STRATEGY_LOG_DIR.exists():
        return
    for f in STRATEGY_LOG_DIR.iterdir():
        if f.suffix == ".json" and f.stem < cutoff:
            try:
                f.unlink()
            except Exception:
                pass
