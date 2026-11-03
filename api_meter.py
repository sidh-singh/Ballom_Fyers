"""
api_meter.py — Local Fyers API counter + throttle (ARCHITECTURE §6).

Why: Fyers Standard plan caps DATA calls at 5 / sec, 50 / min, **5,000 / day**.
Exceeding returns -429; breaching the per-minute cap >3× in a day blocks the
account for the rest of the day. So EVERY Fyers call is routed through this
meter, which:

  • counts calls per category (DATA / ORDER / USERINFO) across rolling
    second / minute / day windows,
  • **throttles** — blocks a call until a slot is free (never breaches),
  • honors -429 Retry-After cooldowns,
  • writes a snapshot to api_usage.json for the dashboard.

Thread-safe: one shared `meter` is used by every per-symbol worker thread.

Usage (wired from fyers_api):
    from api_meter import meter
    resp = meter.guard("quotes", model.quotes, data={...})
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
from collections import defaultdict, deque
from os import replace
from time import monotonic, sleep

import constants
from constants import (
    ENDPOINT_CATEGORY, DEFAULT_CATEGORY, API_USAGE_FILE, now_ist,
)

logger = logging.getLogger("api_meter")
                           # limits/plan read live from constants (plan-aware)
_FLUSH_MIN_INTERVAL = 1.0  # seconds — debounce snapshot writes


class RateLimitExceeded(Exception):
    """Raised internally when a per-day cap is hit (cannot be waited out)."""


class ApiMeter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # one monotonic-timestamp deque per category, pruned to a 60s window
        self._win: dict[str, deque[float]] = defaultdict(deque)
        self._day: dict[str, int] = defaultdict(int)          # per-day counters
        self._day_date = now_ist().date()
        self._blocked_until: dict[str, float] = defaultdict(float)  # -429 cooldown
        self._minute_breaches = 0
        self._last_flush = 0.0

    # ── classification ──────────────────────────────────────────────────────
    @staticmethod
    def classify(method_name: str) -> str:
        return ENDPOINT_CATEGORY.get(method_name, DEFAULT_CATEGORY)

    # ── public entry point ──────────────────────────────────────────────────
    def guard(self, method_name: str, fn, *args, **kwargs):
        """Throttle, then call `fn`. Returns fn's response (or a synthetic
        -429 dict if the local daily cap is already hit)."""
        cat = self.classify(method_name)
        try:
            self._acquire(cat)
        except RateLimitExceeded as e:
            logger.error("Local daily cap hit for %s: %s", cat, e)
            return {"s": "error", "code": -429, "message": f"local daily cap: {cat}"}

        resp = fn(*args, **kwargs)

        # react to a real -429 from Fyers
        if isinstance(resp, dict) and resp.get("code") == -429:
            retry_after = self._retry_after_seconds(resp)
            self._note_429(cat, retry_after)
            logger.warning("Fyers -429 on %s (%s) — cooling down %.1fs",
                           method_name, cat, retry_after)
        return resp

    def note_response_headers(self, cat: str, headers: dict) -> None:
        """Optional: honor Retry-After / X-Retry-After-Ms headers if available."""
        secs = 0.0
        if headers.get("X-Retry-After-Ms"):
            secs = float(headers["X-Retry-After-Ms"]) / 1000.0
        elif headers.get("Retry-After"):
            secs = float(headers["Retry-After"])
        if secs:
            self._note_429(cat, secs)

    # ── core throttle ───────────────────────────────────────────────────────
    def _acquire(self, cat: str) -> None:
        lim = constants.active_api_limits()[cat]
        while True:
            acquired = False
            wait = 0.0
            snap = None                      # computed under lock, written outside it
            with self._lock:
                now = monotonic()
                self._roll_day_if_needed()
                dq = self._win[cat]
                while dq and dq[0] <= now - 60:        # prune >60s old
                    dq.popleft()
                min_count = len(dq)
                sec_count = sum(1 for t in dq if t > now - 1)
                day_count = self._day[cat]

                if now < self._blocked_until[cat]:
                    wait = self._blocked_until[cat] - now
                elif day_count >= lim["day"]:
                    raise RateLimitExceeded(
                        f"{cat} day cap {lim['day']} reached ({day_count})")
                elif sec_count >= lim["sec"]:
                    oldest_in_sec = min(t for t in dq if t > now - 1)
                    wait = max(oldest_in_sec + 1 - now, 0.01)
                elif min_count >= lim["min"]:
                    wait = max(dq[0] + 60 - now, 0.01)
                else:
                    dq.append(now)
                    self._day[cat] += 1
                    acquired = True
                    if now - self._last_flush >= _FLUSH_MIN_INTERVAL:
                        self._last_flush = now
                        snap = self._snapshot_unlocked()
            if snap is not None:
                self._safe_write(snap)       # disk I/O outside the lock
            if acquired:
                return
            sleep(min(wait, 1.0))   # slice sleeps so shutdown/day-reset stay responsive

    def _note_429(self, cat: str, retry_after_s: float) -> None:
        with self._lock:
            self._blocked_until[cat] = monotonic() + max(retry_after_s, 1.0)
            self._minute_breaches += 1
            if self._minute_breaches > 3:
                logger.error("More than 3 rate breaches today — backing off hard")

    @staticmethod
    def _retry_after_seconds(resp: dict) -> float:
        # Fyers may embed a hint; default to a safe 2s
        for k in ("retry_after", "Retry-After", "retryAfter"):
            if k in resp:
                try:
                    return float(resp[k])
                except (TypeError, ValueError):
                    pass
        return 2.0

    def _roll_day_if_needed(self) -> None:
        d = now_ist().date()
        if d != self._day_date:
            self._day_date = d
            self._day.clear()
            self._minute_breaches = 0
            logger.info("API meter: new day %s — daily counters reset", d)

    # ── snapshot / persistence ──────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict:
        """Build the usage snapshot. Caller must hold the lock."""
        now = monotonic()
        cats = {}
        for cat, lim in constants.active_api_limits().items():
            dq = self._win[cat]
            min_count = sum(1 for t in dq if t > now - 60)
            sec_count = sum(1 for t in dq if t > now - 1)
            day_count = self._day[cat]
            cats[cat] = {
                "sec": {"used": sec_count, "limit": lim["sec"]},
                "min": {"used": min_count, "limit": lim["min"]},
                "day": {"used": day_count, "limit": lim["day"],
                        "pct": round(100 * day_count / lim["day"], 1)},
            }
        return {"timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                "plan": constants.API_PLAN, "categories": cats}

    def _safe_write(self, snap: dict) -> None:
        try:
            self._write_atomic(snap)
        except Exception as e:                     # never let telemetry break a call
            logger.debug("api_usage flush failed: %s", e)

    @staticmethod
    def _write_atomic(data: dict) -> None:
        API_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(API_USAGE_FILE.parent))
        import os
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        replace(tmp, API_USAGE_FILE)


# shared singleton — import this everywhere
meter = ApiMeter()
