"""
position_tracker.py — per-leg cost-basis tracker for Fyers netting (carry-safe).

Design (fyers_live hardening): instead of Fyers' cumulative `pl` + booked_profit
(which is only intraday-correct and breaks across the day boundary for carried
positions), we track OUR OWN cost basis from our fills:

    cycle_pl(ltp) = ltp * net_qty - cost_basis          # open long basket P&L (₹)
    on close:  realized = exit_price * net_qty - cost_basis

This is day-boundary-independent (correct for overnight carry) and immune to
Fyers' blended-average contamination after same-day re-entry. Supersedes the
booked_profit model documented in FYERS_PNL_LOGIC.md for the OPEN-basket decision.

⚠️ Correctness depends on accurate FILL PRICES (see symbol_worker._fill_price,
sourced from tradebook with fallbacks) — validate during the live dry-run.

Keyed by the OPTION LEG symbol. Thread-safe (single shared instance).
Files under C:/Ballom_FYR/state/: position_tracker.json, profit_history.json.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from constants import STATE_DIR, now_ist, today_ist, HISTORY_RETENTION_DAYS


class PositionTracker:
    MAX_HISTORY_ENTRIES = 5000

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dir = STATE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tracker_file = self._dir / "position_tracker.json"
        self._history_file = self._dir / "profit_history.json"
        self._data: dict = self._read_json(self._tracker_file)
        self._roll_day()

    # ── io ──────────────────────────────────────────────────────────────────
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

    def _save(self) -> None:
        self._write_json_atomic(self._tracker_file, self._data)

    @staticmethod
    def _ts() -> str:
        return now_ist().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _default_entry() -> dict:
        return {
            "net_qty": 0, "cost_basis": 0.0, "avg_cost": 0.0, "side": 0,
            "martingale_count": 0, "first_entry_time": "", "last_close_time": "",
            "close_count": 0, "realized_total": 0.0, "last_cycle_profit": 0.0,
        }

    def _entry(self, symbol: str) -> dict:
        e = self._data.get(symbol)
        if not isinstance(e, dict):
            e = self._default_entry()
            self._data[symbol] = e
        return e

    # ── CARRY-AWARE day reset (cost_basis/net_qty survive for open legs) ─────
    def _roll_day(self) -> None:
        today = today_ist().isoformat()
        if self._data.get("_date") == today:
            return
        preserved = {"_date": today}
        for k, v in self._data.items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            if int(v.get("net_qty", 0)) != 0:      # carried → keep cost basis
                v["close_count"] = 0
                v["realized_total"] = 0.0
                v["last_cycle_profit"] = 0.0
                preserved[k] = v
        self._data = preserved
        self._save()

    # ═══════════════════════════════════════════════════════════════════════
    #  PUBLIC API
    # ═══════════════════════════════════════════════════════════════════════
    def record_entry(self, symbol: str, qty: int, price: float, side: int) -> None:
        with self._lock:
            e = self._entry(symbol)
            e["net_qty"] = int(qty)
            e["cost_basis"] = round(qty * price, 2)
            e["avg_cost"] = round(price, 2)
            e["side"] = side
            e["martingale_count"] = 0
            if not e["first_entry_time"]:
                e["first_entry_time"] = self._ts()
            self._save()
            self._hist(symbol, 0.0, qty, "ENTRY", ltp=price, avg=price)

    def record_add(self, symbol: str, add_qty: int, price: float) -> None:
        with self._lock:
            e = self._entry(symbol)
            e["net_qty"] = int(e["net_qty"]) + int(add_qty)
            e["cost_basis"] = round(e["cost_basis"] + add_qty * price, 2)
            e["avg_cost"] = round(e["cost_basis"] / max(e["net_qty"], 1), 2)
            e["martingale_count"] = e.get("martingale_count", 0) + 1
            self._save()
            self._hist(symbol, self.cycle_pl(symbol, price), add_qty, "MARTINGALE",
                       ltp=price, avg=e["avg_cost"])

    def record_close(self, symbol: str, exit_price: float, charges: float = 0.0) -> float:
        """Book the full close at *exit_price* with *charges* (estimated tax).
        Records gross P&L, tax and net; returns NET realized ₹ for the cycle."""
        with self._lock:
            e = self._entry(symbol)
            net = int(e.get("net_qty", 0))
            gross = round(exit_price * net - e.get("cost_basis", 0.0), 2)
            net_pl = round(gross - charges, 2)
            e["close_count"] = e.get("close_count", 0) + 1
            e["realized_total"] = round(e.get("realized_total", 0.0) + net_pl, 2)  # NET
            e["last_cycle_profit"] = round(net_pl, 2)
            e["last_close_time"] = self._ts()
            e["net_qty"] = 0
            e["cost_basis"] = 0.0
            e["avg_cost"] = 0.0
            e["side"] = 0
            e["martingale_count"] = 0
            self._save()
            self._hist(symbol, gross, net, "CLOSE", ltp=exit_price, avg=exit_price,
                       charges=charges, net_pl=net_pl)
            return net_pl

    def seed_from_broker(self, symbol: str, net_qty: int, avg_price: float, side: int) -> None:
        """Adopt a position opened outside the tracker (manual/restart): approximate
        cost basis from the broker's avg price. ⚠️ approximation — flagged."""
        with self._lock:
            e = self._entry(symbol)
            if int(e.get("net_qty", 0)) != 0:
                return  # already tracked
            e["net_qty"] = int(net_qty)
            e["cost_basis"] = round(net_qty * avg_price, 2)
            e["avg_cost"] = round(avg_price, 2)
            e["side"] = side
            self._save()

    def cycle_pl(self, symbol: str, ltp: float) -> float:
        """Open-basket P&L (₹) for a long leg: ltp*net_qty - cost_basis."""
        with self._lock:
            e = self._data.get(symbol)
            if not isinstance(e, dict) or int(e.get("net_qty", 0)) == 0:
                return 0.0
            return round(ltp * int(e["net_qty"]) - e.get("cost_basis", 0.0), 2)

    def get_net_qty(self, symbol: str) -> int:
        with self._lock:
            e = self._data.get(symbol)
            return int(e.get("net_qty", 0)) if isinstance(e, dict) else 0

    def get_martingale_count(self, symbol: str) -> int:
        with self._lock:
            e = self._data.get(symbol)
            return e.get("martingale_count", 0) if isinstance(e, dict) else 0

    def log_snapshot(self, symbol: str, ltp: float) -> None:
        with self._lock:
            self._hist(symbol, self.cycle_pl(symbol, ltp),
                       self.get_net_qty(symbol), "SNAPSHOT", ltp=ltp)

    # ── history ─────────────────────────────────────────────────────────────
    def _hist(self, symbol, effective_pl, qty, action, ltp=0.0, avg=0.0,
              charges=0.0, net_pl=None) -> None:
        entries = self._read_json(self._history_file)
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "timestamp": self._ts(), "date": today_ist().isoformat(), "symbol": symbol,
            "effective_pl": round(effective_pl, 2),          # gross
            "charges": round(charges, 2),                    # estimated tax
            "net_pl": round(net_pl if net_pl is not None else effective_pl, 2),  # actual
            "qty": qty, "action": action,
            "ltp": round(ltp, 2), "avg_price": round(avg, 2),
        })
        # retention: keep only ~1 week of trade history (matches the dashboard date picker)
        cutoff = (today_ist() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        entries = [e for e in entries if str(e.get("date", "")) >= cutoff]
        if len(entries) > self.MAX_HISTORY_ENTRIES:
            entries = entries[-self.MAX_HISTORY_ENTRIES:]
        self._write_json_atomic(self._history_file, entries)
