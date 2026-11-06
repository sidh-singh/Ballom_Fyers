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
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from constants import (
    STATE_DIR, APP_STATUS_FILE, STRATEGY_LOG_DIR, now_ist,
    SIGNAL_STATE_FILE, ACCOUNT_STATE_FILE, OHLC_STATE_FILE,
    OHLC_HIST_DIR, HISTORY_RETENTION_DAYS,
)

_signal_lock = threading.Lock()   # serialise per-symbol signal_state read-merge-write
_ohlc_lock = threading.Lock()     # serialise per-symbol ohlc_state read-merge-write
_OHLC_MAX_BARS = 220              # a full session of M5 candles (NSE ~75, MCX ~175) for the price chart

_MAX_LOG_ENTRIES = 500
_MAX_LOG_DAYS = HISTORY_RETENTION_DAYS   # keep 1 week of per-day logs + OHLC snapshots


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


def write_signal_state(symbol: str, payload: dict) -> None:
    """Upsert one symbol's SHA/RSI/signal/pair analysis (dashboard reads this)."""
    with _signal_lock:
        data = _read_json(SIGNAL_STATE_FILE)
        if not isinstance(data, dict):
            data = {}
        payload = dict(payload)
        payload["timestamp"] = _ts()
        data[symbol] = payload
        _write_json_atomic(SIGNAL_STATE_FILE, data)


def write_account_state(payload: dict) -> None:
    """Balance / equity / margin / unrealized snapshot (dashboard KPI row)."""
    payload = dict(payload)
    payload["timestamp"] = _ts()
    _write_json_atomic(ACCOUNT_STATE_FILE, payload)


def write_ohlc_state(symbol: str, leg: str, side: str, tf: str,
                     t: list, o: list, h: list, l: list, c: list, active: bool) -> None:
    """Upsert one symbol's recent OHLC candles for the dashboard price chart.

    `leg` is the instrument the candles belong to (the active CE/PE option while a
    position is open, else the underlying index). Arrays are column-wise; the dashboard
    plots go.Candlestick(x=t, open=o, high=h, low=l, close=c) and overlays the fills
    from profit_history.json. Pandas stays in the worker — this takes plain lists."""
    n = _OHLC_MAX_BARS
    with _ohlc_lock:
        data = _read_json(OHLC_STATE_FILE)
        if not isinstance(data, dict):
            data = {}
        data[symbol] = {
            "leg": leg, "side": side, "tf": tf, "active": bool(active),
            "t": list(t)[-n:], "o": list(o)[-n:], "h": list(h)[-n:],
            "l": list(l)[-n:], "c": list(c)[-n:], "timestamp": _ts(),
        }
        _write_json_atomic(OHLC_STATE_FILE, data)
        # per-day snapshot so the dashboard date picker can show a past day's price chart
        today = now_ist().strftime("%Y-%m-%d")
        _write_json_atomic(OHLC_HIST_DIR / f"{today}.json", data)
        _prune_dir(OHLC_HIST_DIR)


def backfill_ohlc_state(symbol: str, leg: str, tf: str, rows_by_date: dict) -> None:
    """One-time: seed PAST-day OHLC snapshots for `symbol` from history, so the dashboard date
    picker shows past charts immediately (not just going forward). `rows_by_date` maps
    'YYYY-MM-DD' -> {t,o,h,l,c} lists. Merges into ohlc_hist/<date>.json; never overwrites a day
    that already has this symbol's candles (a live-written day wins)."""
    n = _OHLC_MAX_BARS
    with _ohlc_lock:
        for d, bars in rows_by_date.items():
            path = OHLC_HIST_DIR / f"{d}.json"
            data = _read_json(path)
            if not isinstance(data, dict):
                data = {}
            if data.get(symbol, {}).get("t"):      # already populated (e.g. live) — keep it
                continue
            data[symbol] = {
                "leg": leg, "side": "", "tf": tf, "active": False,
                "t": list(bars["t"])[-n:], "o": list(bars["o"])[-n:], "h": list(bars["h"])[-n:],
                "l": list(bars["l"])[-n:], "c": list(bars["c"])[-n:], "timestamp": _ts(),
            }
            _write_json_atomic(path, data)
        _prune_dir(OHLC_HIST_DIR)


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


def _prune_dir(d: Path) -> None:
    """Delete YYYY-MM-DD.json files in `d` older than the retention window (keeps ~1 week)."""
    cutoff = (now_ist() - timedelta(days=_MAX_LOG_DAYS)).strftime("%Y-%m-%d")
    if not d.exists():
        return
    for f in d.iterdir():
        if f.suffix == ".json" and f.stem < cutoff:
            try:
                f.unlink()
            except Exception:
                pass


def _cleanup_old_logs() -> None:
    _prune_dir(STRATEGY_LOG_DIR)
