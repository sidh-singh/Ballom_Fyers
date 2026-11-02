"""
position_tracker.py — Per-leg booked-profit tracker for Fyers netting.

    effective_pl = api_total_pl - booked_profit      (see FYERS_PNL_LOGIC.md)
    on confirmed close → booked_profit = api_total_pl

Ported from dev_trading with two fyers_live changes:
  • LIVE only — single state dir (no demo/live).
  • CARRY FIX — on day-change, booked_profit/qty/side/martingale are PRESERVED for
    any leg still holding an open position (current_qty != 0); only fully-closed
    legs are reset. The old intraday port wiped everything, which would corrupt
    effective_pl for positions carried overnight (TRADING_LOGIC.md §5).

Keyed by the OPTION LEG symbol (e.g. "NSE:NIFTY25NOV23500CE").
Thread-safe: a single shared instance is used by all worker threads.

⚠️ Overnight reconciliation of Fyers' `pl` for carried NRML positions must be
validated live — preserving booked_profit is the safe structural default.

Files under C:/Ballom_FYR/state/:
  position_tracker.json — per-leg booked profit & stats
  profit_history.json   — time-series for the dashboard booking chart
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from constants import STATE_DIR, now_ist, today_ist


class PositionTracker:
    MAX_HISTORY_ENTRIES = 5000

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dir = STATE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tracker_file = self._dir / "position_tracker.json"
        self._history_file = self._dir / "profit_history.json"
        self._data: dict = self._read_json(self._tracker_file)
        self._current_date = today_ist().isoformat()
        self._roll_day()  # carry-aware day reset

    # ── atomic JSON I/O ─────────────────────────────────────────────────────
    @staticmethod
    def _write_json_atomic(path: Path, data) -> None:
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

    @staticmethod
    def _read_json(path: Path):
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _save_tracker(self) -> None:
        self._write_json_atomic(self._tracker_file, self._data)

    @staticmethod
    def _ts() -> str:
        return now_ist().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _default_entry() -> dict:
        return {
            "booked_profit": 0.0, "close_count": 0, "total_profit_closed": 0.0,
            "last_cycle_profit": 0.0, "first_entry_time": "", "last_close_time": "",
            "current_qty": 0, "current_side": 0, "martingale_count": 0,
        }

    # ── CARRY-AWARE day reset ───────────────────────────────────────────────
    def _roll_day(self) -> None:
        today = today_ist().isoformat()
        if self._data.get("_date") == today:
            return
        preserved: dict = {"_date": today}
        for k, v in self._data.items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            if int(v.get("current_qty", 0)) != 0:      # carried overnight → KEEP
                v["close_count"] = 0                    # reset daily stats only
                v["total_profit_closed"] = 0.0
                v["last_cycle_profit"] = 0.0
                preserved[k] = v                        # booked_profit / qty / side / mg kept
        self._data = preserved
        self._current_date = today
        self._save_tracker()

    # ═══════════════════════════════════════════════════════════════════════
    #  PUBLIC API (thread-safe)
    # ═══════════════════════════════════════════════════════════════════════
    def get_effective_pl(self, symbol: str, api_total_pl: float) -> float:
        with self._lock:
            self._roll_day()
            entry = self._data.get(symbol)
            if not entry or not isinstance(entry, dict):
                return api_total_pl
            return api_total_pl - entry.get("booked_profit", 0.0)

    def record_entry(self, symbol: str, qty: int, side: int,
                     ltp: float = 0.0, avg_price: float = 0.0) -> None:
        with self._lock:
            entry = self._data.get(symbol)
            if not entry or not isinstance(entry, dict):
                entry = self._default_entry(); self._data[symbol] = entry
            entry["current_qty"] = qty
            entry["current_side"] = side
            entry["martingale_count"] = 0
            if not entry.get("first_entry_time"):
                entry["first_entry_time"] = self._ts()
            self._save_tracker()
            self._append_history(symbol, 0.0, 0.0, entry.get("booked_profit", 0.0),
                                 qty, "ENTRY", ltp=ltp, avg_price=avg_price)

    def record_martingale(self, symbol: str, added_qty: int, total_qty: int,
                          side: int, effective_pl: float, api_total_pl: float) -> None:
        with self._lock:
            entry = self._data.get(symbol)
            if not entry or not isinstance(entry, dict):
                entry = self._default_entry(); self._data[symbol] = entry
            entry["current_qty"] = total_qty + added_qty
            entry["current_side"] = side
            entry["martingale_count"] = entry.get("martingale_count", 0) + 1
            self._save_tracker()
            self._append_history(symbol, effective_pl, api_total_pl,
                                 entry.get("booked_profit", 0.0), added_qty, "MARTINGALE")

    def record_close(self, symbol: str, api_total_pl: float, qty: int,
                     effective_pl: float, ltp: float = 0.0, avg_price: float = 0.0) -> None:
        with self._lock:
            entry = self._data.get(symbol)
            if not entry or not isinstance(entry, dict):
                entry = self._default_entry(); self._data[symbol] = entry
            entry["booked_profit"] = api_total_pl           # snapshot on confirmed close
            entry["close_count"] = entry.get("close_count", 0) + 1
            entry["total_profit_closed"] = round(
                entry.get("total_profit_closed", 0.0) + effective_pl, 2)
            entry["last_cycle_profit"] = round(max(0.0, effective_pl), 2)
            entry["last_close_time"] = self._ts()
            entry["current_qty"] = 0
            entry["current_side"] = 0
            entry["martingale_count"] = 0
            self._save_tracker()
            self._append_history(symbol, effective_pl, api_total_pl,
                                 entry["booked_profit"], qty, "CLOSE",
                                 ltp=ltp, avg_price=avg_price)

    def log_snapshot(self, symbol: str, effective_pl: float, api_total_pl: float,
                     qty: int, ltp: float = 0.0, avg_price: float = 0.0) -> None:
        with self._lock:
            booked = 0.0
            entry = self._data.get(symbol)
            if entry and isinstance(entry, dict):
                booked = entry.get("booked_profit", 0.0)
            self._append_history(symbol, effective_pl, api_total_pl, booked, qty,
                                 "SNAPSHOT", ltp=ltp, avg_price=avg_price)

    def get_martingale_count(self, symbol: str) -> int:
        with self._lock:
            entry = self._data.get(symbol)
            return entry.get("martingale_count", 0) if isinstance(entry, dict) else 0

    # ── internal history writer ─────────────────────────────────────────────
    def _append_history(self, symbol, effective_pl, api_total_pl, booked_profit,
                        qty, action, ltp=0.0, avg_price=0.0) -> None:
        entries = self._read_json(self._history_file)
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "timestamp": self._ts(), "date": today_ist().isoformat(), "symbol": symbol,
            "effective_pl": round(effective_pl, 2), "api_total_pl": round(api_total_pl, 2),
            "booked_profit": round(booked_profit, 2), "qty": qty, "action": action,
            "ltp": round(ltp, 2), "avg_price": round(avg_price, 2),
        })
        if len(entries) > self.MAX_HISTORY_ENTRIES:
            entries = entries[-self.MAX_HISTORY_ENTRIES:]
        self._write_json_atomic(self._history_file, entries)
