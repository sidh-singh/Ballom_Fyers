"""
position_tracker.py — Tracks per-symbol booked profit to handle Fyers
position API cumulative realized_profit behavior.

    effective_pl = api_total_pl - booked_profit

After each HEDGE close:
    booked_profit = api_total_pl  (snapshot the total at close time)

Files (under C:/Ballom_FYR/state/<mode>/):
  position_tracker.json — per-symbol booked profit & daily trade stats
  profit_history.json   — time-series snapshots for dashboard line graph
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, date
from pathlib import Path

from constants import get_state_dir


class PositionTracker:
    """Persistent per-symbol profit tracker."""

    MAX_HISTORY_ENTRIES = 5000

    def __init__(self, mode: str = "demo") -> None:
        self.mode = mode
        self._dir = get_state_dir(mode)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tracker_file = self._dir / "position_tracker.json"
        self._history_file = self._dir / "profit_history.json"
        self._data: dict = self._read_json(self._tracker_file)
        self._current_date = date.today().isoformat()

        if self._data.get("_date") != self._current_date:
            self._data = {"_date": self._current_date}
            self._save_tracker()

    # ── atomic JSON I/O ────────────────────────────────────────────────────

    @staticmethod
    def _write_json_atomic(path: Path, data) -> None:
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

    @staticmethod
    def _read_json(path: Path):
        if not path.exists():
            return {}
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_tracker(self) -> None:
        self._write_json_atomic(self._tracker_file, self._data)

    def _load_history(self) -> list:
        data = self._read_json(self._history_file)
        return data if isinstance(data, list) else []

    def _save_history(self, entries: list) -> None:
        self._write_json_atomic(self._history_file, entries)

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _default_entry() -> dict:
        return {
            "booked_profit": 0.0,
            "close_count": 0,
            "total_profit_closed": 0.0,
            "last_cycle_profit": 0.0,
            "first_entry_time": "",
            "last_close_time": "",
            "current_qty": 0,
            "current_side": 0,
            "martingale_count": 0,
        }

    # ═══════════════════════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════

    def get_effective_pl(self, symbol: str, api_total_pl: float) -> float:
        entry = self._data.get(symbol)
        if not entry or not isinstance(entry, dict):
            return api_total_pl
        return api_total_pl - entry.get("booked_profit", 0.0)

    def record_close(
        self, symbol: str, api_total_pl: float, qty: int, effective_pl: float,
        ltp: float = 0.0, avg_price: float = 0.0,
    ) -> None:
        entry = self._data.get(symbol)
        if not entry or not isinstance(entry, dict):
            entry = self._default_entry()
            self._data[symbol] = entry

        entry["booked_profit"] = api_total_pl
        entry["close_count"] = entry.get("close_count", 0) + 1
        entry["total_profit_closed"] = round(
            entry.get("total_profit_closed", 0.0) + effective_pl, 2
        )
        entry["last_cycle_profit"] = round(max(0.0, effective_pl), 2)
        entry["last_close_time"] = self._ts()
        entry["current_qty"] = 0
        entry["current_side"] = 0
        self._save_tracker()

        self._append_history(
            symbol, effective_pl, api_total_pl,
            entry["booked_profit"], qty, "CLOSE",
            ltp=ltp, avg_price=avg_price,
        )

    def record_entry(self, symbol: str, qty: int, side: int,
                     ltp: float = 0.0, avg_price: float = 0.0) -> None:
        entry = self._data.get(symbol)
        if not entry or not isinstance(entry, dict):
            entry = self._default_entry()
            self._data[symbol] = entry

        entry["current_qty"] = qty
        entry["current_side"] = side
        entry["martingale_count"] = 0
        if not entry.get("first_entry_time"):
            entry["first_entry_time"] = self._ts()
        self._save_tracker()

        self._append_history(
            symbol, 0.0, 0.0,
            entry.get("booked_profit", 0.0), qty, "ENTRY",
            ltp=ltp, avg_price=avg_price,
        )

    def record_martingale(
        self, symbol: str, added_qty: int, total_qty: int,
        side: int, effective_pl: float, api_total_pl: float,
    ) -> None:
        entry = self._data.get(symbol)
        if not entry or not isinstance(entry, dict):
            entry = self._default_entry()
            self._data[symbol] = entry

        entry["current_qty"] = total_qty + added_qty
        entry["current_side"] = side
        entry["martingale_count"] = entry.get("martingale_count", 0) + 1
        self._save_tracker()

        self._append_history(
            symbol, effective_pl, api_total_pl,
            entry.get("booked_profit", 0.0),
            added_qty, "MARTINGALE",
        )

    def log_snapshot(
        self, symbol: str, effective_pl: float,
        api_total_pl: float, qty: int,
        ltp: float = 0.0, avg_price: float = 0.0,
    ) -> None:
        booked = 0.0
        entry = self._data.get(symbol)
        if entry and isinstance(entry, dict):
            booked = entry.get("booked_profit", 0.0)
        self._append_history(
            symbol, effective_pl, api_total_pl, booked, qty, "SNAPSHOT",
            ltp=ltp, avg_price=avg_price,
        )

    def get_martingale_count(self, symbol: str) -> int:
        entry = self._data.get(symbol)
        if not entry or not isinstance(entry, dict):
            return 0
        return entry.get("martingale_count", 0)

    def reset_for_new_day(self) -> None:
        self._current_date = date.today().isoformat()
        self._data = {"_date": self._current_date}
        self._save_tracker()

    def reset_booked_profits(self) -> None:
        for key, val in self._data.items():
            if key.startswith("_") or not isinstance(val, dict):
                continue
            val["booked_profit"] = 0.0
            val["total_profit_closed"] = 0.0
            val["close_count"] = 0
        self._save_tracker()

    def get_daily_summary(self) -> dict:
        total_closes = 0
        total_profit = 0.0
        symbols: dict = {}
        for key, val in self._data.items():
            if key.startswith("_") or not isinstance(val, dict):
                continue
            cc = val.get("close_count", 0)
            tp = val.get("total_profit_closed", 0.0)
            total_closes += cc
            total_profit += tp
            if cc > 0:
                symbols[key] = {"closes": cc, "profit": round(tp, 2)}
        return {
            "date": self._current_date,
            "total_closes_today": total_closes,
            "total_profit_today": round(total_profit, 2),
            "avg_profit_per_close": round(total_profit / max(total_closes, 1), 2),
            "symbols": symbols,
        }

    # ── internal ───────────────────────────────────────────────────────────

    def _append_history(
        self, symbol: str, effective_pl: float, api_total_pl: float,
        booked_profit: float, qty: int, action: str,
        ltp: float = 0.0, avg_price: float = 0.0,
    ) -> None:
        entries = self._load_history()
        actual_date = date.today().isoformat()
        entries.append({
            "timestamp": self._ts(),
            "date": actual_date,
            "symbol": symbol,
            "effective_pl": round(effective_pl, 2),
            "api_total_pl": round(api_total_pl, 2),
            "booked_profit": round(booked_profit, 2),
            "qty": qty,
            "action": action,
            "ltp": round(ltp, 2),
            "avg_price": round(avg_price, 2),
        })
        if len(entries) > self.MAX_HISTORY_ENTRIES:
            entries = entries[-self.MAX_HISTORY_ENTRIES:]
        self._save_history(entries)
