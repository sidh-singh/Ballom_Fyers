"""
demo_broker.py — local paper-trading broker for the fyers_demo branch.

Fyers has no demo account, so this simulates OPEN/CLOSE on local disk while keeping
ALL read APIs real (auth, quotes, optionchain, history, funds-ltp) — so API
throttling and the live scan/signal logic are genuinely exercised. Simulated
positions use the REAL last-traded price for P&L, so the close/target logic behaves
exactly as it would live. No real orders, no real money.

Store: C:/Ballom_FYR/demo/demo_broker.json  (positions, realized P&L, balance, trades)

Usage: the engine (application.py on this branch) instantiates DemoFyersAPI instead
of FyersAPI. Everything else is identical, so the logic under test is the real code.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

from constants import (
    DEMO_DIR, DEMO_BROKER_FILE, DEMO_START_BALANCE, PRODUCT_TYPE, now_ist,
)
from fyers_api import FyersAPI

logger = logging.getLogger("demo")


class DemoBroker:
    """Disk-backed simulated broker state (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if DEMO_BROKER_FILE.exists():
            try:
                return json.loads(DEMO_BROKER_FILE.read_text())
            except Exception:
                pass
        return {"balance": float(DEMO_START_BALANCE), "realized": 0.0,
                "positions": {}, "trades": []}

    def _save(self) -> None:
        DEMO_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(DEMO_DIR))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
            os.replace(tmp, DEMO_BROKER_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @staticmethod
    def _ts() -> str:
        return now_ist().strftime("%Y-%m-%d %H:%M:%S")

    def buy(self, symbol: str, qty: int, price: float) -> None:
        with self._lock:
            p = self._data["positions"].setdefault(symbol, {"netQty": 0, "cost": 0.0, "avg": 0.0})
            p["cost"] = round(p["cost"] + qty * price, 2)
            p["netQty"] = int(p["netQty"]) + int(qty)
            p["avg"] = round(p["cost"] / p["netQty"], 2) if p["netQty"] else 0.0
            self._data["trades"].append(
                {"time": self._ts(), "symbol": symbol, "side": "BUY",
                 "qty": int(qty), "tradePrice": round(price, 2)})
            self._trim(); self._save()

    def sell(self, symbol: str, qty: int, price: float) -> float:
        with self._lock:
            p = self._data["positions"].get(symbol)
            if not p or int(p.get("netQty", 0)) <= 0:
                return 0.0
            close_qty = min(int(qty), int(p["netQty"]))
            realized = round((price - float(p["avg"])) * close_qty, 2)
            self._data["realized"] = round(self._data["realized"] + realized, 2)
            self._data["balance"] = round(self._data["balance"] + realized, 2)
            p["netQty"] = int(p["netQty"]) - close_qty
            if p["netQty"] <= 0:
                p["netQty"] = 0; p["cost"] = 0.0; p["avg"] = 0.0
            else:
                p["cost"] = round(float(p["avg"]) * p["netQty"], 2)
            self._data["trades"].append(
                {"time": self._ts(), "symbol": symbol, "side": "SELL",
                 "qty": close_qty, "tradePrice": round(price, 2), "realized": realized})
            self._trim(); self._save()
            return realized

    def position(self, symbol: str):
        with self._lock:
            return dict(self._data["positions"].get(symbol, {})) or None

    def open_positions(self) -> dict:
        with self._lock:
            return {s: dict(p) for s, p in self._data["positions"].items()
                    if int(p.get("netQty", 0)) != 0}

    def last_fill(self, symbol: str):
        with self._lock:
            for t in reversed(self._data["trades"]):
                if t["symbol"] == symbol:
                    return float(t["tradePrice"])
        return None

    def balance(self) -> float:
        with self._lock:
            return float(self._data["balance"])

    def trades(self, n: int = 200) -> list:
        with self._lock:
            return list(self._data["trades"][-n:])

    def _trim(self) -> None:
        if len(self._data["trades"]) > 2000:
            self._data["trades"] = self._data["trades"][-2000:]


class DemoFyersAPI(FyersAPI):
    """Real reads (inherited + metered); OPEN/CLOSE simulated via DemoBroker.

    Overridden (simulated): buy, sell, positions, get_position, funds, get_balance,
    last_fill_price, tradebook. Everything else (quotes, optionchain, history,
    fetch_option_pair, download_*, resolve_commodity, auth) is the REAL FyersAPI.
    """

    def __init__(self, auth) -> None:
        super().__init__(auth)
        self._demo = DemoBroker()
        logger.warning("DEMO broker active — orders are SIMULATED on local disk (%s)",
                       DEMO_BROKER_FILE)

    def _ltp(self, symbol: str) -> float:
        """Real, metered last price (so simulated P&L tracks the live market)."""
        q = self.safe_api_call(self.model.quotes, data={"symbols": symbol})
        if isinstance(q, dict) and q.get("s") == "ok" and q.get("d"):
            try:
                return float(q["d"][0]["v"].get("lp") or 0.0)
            except Exception:
                return 0.0
        return 0.0

    # ── simulated orders ──────────────────────────────────────────────────────
    def buy(self, symbol: str, qty: int, product_type: str = PRODUCT_TYPE):
        px = self._ltp(symbol)
        self._demo.buy(symbol, int(qty), px)
        logger.info("[DEMO] BUY %s x%s @ %s", symbol, qty, px)
        return {"s": "ok", "code": 200, "message": "demo buy",
                "id": f"demo-{now_ist().strftime('%H%M%S%f')}", "demo": True}

    def sell(self, symbol: str, qty: int, product_type: str = PRODUCT_TYPE):
        px = self._ltp(symbol)
        r = self._demo.sell(symbol, int(qty), px)
        logger.info("[DEMO] SELL %s x%s @ %s realized=%s", symbol, qty, px, r)
        return {"s": "ok", "code": 200, "message": "demo sell",
                "id": f"demo-{now_ist().strftime('%H%M%S%f')}", "demo": True}

    # ── simulated positions / account (real LTP for P&L) ──────────────────────
    def get_position(self, symbol: str, product_type: str = PRODUCT_TYPE):
        p = self._demo.position(symbol)
        if not p or int(p.get("netQty", 0)) == 0:
            return None
        ltp = self._ltp(symbol)
        net, avg = int(p["netQty"]), float(p["avg"])
        return {"symbol": symbol, "netQty": net, "netAvg": avg, "buyAvg": avg,
                "ltp": ltp, "productType": product_type, "pl": round((ltp - avg) * net, 2)}

    def positions(self) -> dict:
        rows = []
        for sym, p in self._demo.open_positions().items():
            ltp = self._ltp(sym)
            net, avg = int(p["netQty"]), float(p["avg"])
            rows.append({"symbol": sym, "netQty": net, "netAvg": avg, "buyAvg": avg,
                         "ltp": ltp, "productType": PRODUCT_TYPE,
                         "pl": round((ltp - avg) * net, 2)})
        return {"s": "ok", "netPositions": rows}

    def funds(self) -> dict:
        return {"s": "ok", "fund_limit": [
            {"title": "Total Balance", "equityAmount": self._demo.balance()}]}

    def get_balance(self) -> float:
        return float(self._demo.balance())

    def last_fill_price(self, symbol: str):
        return self._demo.last_fill(symbol)

    def tradebook(self) -> dict:
        return {"s": "ok", "tradeBook": self._demo.trades()}
