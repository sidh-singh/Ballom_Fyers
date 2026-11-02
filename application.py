"""
application.py — `fyers_live` engine entry point (Loop-1 supervisor). LIVE only.

Run:  python application.py        (Python 3.11 — see ARCHITECTURE §10)

Two-loop design (ARCHITECTURE §5):
  • Loop 1 (this file, 1 thread): auth, daily CSV re-download, holidays, brake,
    and supervising the per-symbol worker threads. No market-data polling.
  • Loop 2 (symbol_worker.py, 1 thread per symbol): scan/manage per symbol.

Opening/closing positions is deferred (ARCHITECTURE §11).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from time import sleep

from constants import (
    SYMBOLS_JSON, SUPERVISOR_TICK, DAILY_MAINTENANCE_AT, now_ist, today_ist,
)
from fyers_auth import FyersAuth
from fyers_api import FyersAPI
from api_meter import meter
from symbol_worker import SymbolWorker
from position_tracker import PositionTracker
from state_writer import write_app_status, log_strategy_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-12s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("application")


# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED ENGINE CONTEXT  (passed to every worker)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class EngineContext:
    auth: FyersAuth
    api: FyersAPI
    meter: object
    tracker: object
    stop_event: threading.Event
    brake_event: threading.Event
    pairs_lock: threading.Lock
    holidays: set = field(default_factory=set)
    special_sessions: list = field(default_factory=list)
    option_df: object = None

    def is_trading_day(self, d: date) -> bool:
        """Weekend + NSE-holiday aware (special sessions override)."""
        ds = d.isoformat()
        if any(s.get("date") == ds for s in self.special_sessions):
            return True
        if d.weekday() >= 5:
            return False
        return ds not in self.holidays


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH + DAILY MAINTENANCE
# ═══════════════════════════════════════════════════════════════════════════════

def _startup_auth(auth: FyersAuth, max_retries: int = 10) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            auth.get_model(force=False)
            logger.info("Startup auth OK (attempt %d)", attempt)
            return True
        except Exception as e:
            logger.warning("Startup auth %d/%d failed: %s", attempt, max_retries, e)
            sleep(min(30 * attempt, 300))
    return False


def _ensure_auth(auth: FyersAuth) -> bool:
    """Re-auth on any failure (pt 2)."""
    if auth.is_authenticated:
        return True
    try:
        auth.get_model(force=False)
        return True
    except Exception:
        try:
            auth.re_authenticate()
            return True
        except Exception as e:
            logger.error("Re-auth failed: %s", e)
            return False


def daily_maintenance(ctx: EngineContext, reauth: bool) -> None:
    """Re-auth (optional) + re-download CSV + refresh holidays (pt 1)."""
    if reauth:
        ctx.auth.get_model(force=True)
    ctx.option_df = FyersAPI.download_option_data()

    today = today_ist()
    # Holidays: load (cached). Fetch is automatic on cache-miss.
    # TODO: force a yearly refresh on Jan 1 (delete cache then fetch).
    if (not ctx.holidays) or (today.month == 1 and today.day == 1):
        FyersAPI.fetch_trading_holidays(today.year)
        if today.month == 12:
            FyersAPI.fetch_trading_holidays(today.year + 1)
    ctx.holidays, ctx.special_sessions = FyersAPI.load_holiday_set(today.year)
    logger.info("Daily maintenance done — %d holidays loaded", len(ctx.holidays))


# ═══════════════════════════════════════════════════════════════════════════════
#  WORKER SUPERVISION
# ═══════════════════════════════════════════════════════════════════════════════

def supervise_workers(workers: dict, config: dict, ctx: EngineContext) -> None:
    """Spawn enabled symbols; restart any worker that has died."""
    for entry in config.get("symbols", []):
        if not entry.get("enabled"):
            continue
        sym = entry["symbol"]
        w = workers.get(sym)
        if w is None or not w.is_alive():
            if w is not None:
                logger.warning("[%s] worker died — restarting", sym)
            nw = SymbolWorker(entry, ctx)
            nw.start()
            workers[sym] = nw


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN  (Loop 1)
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("=" * 60)
    logger.info("  fyers_live engine starting (LIVE)")
    logger.info("=" * 60)

    config = load_config()
    auth = FyersAuth(read_only=False)
    api = FyersAPI(auth)
    tracker = PositionTracker()
    ctx = EngineContext(
        auth=auth, api=api, meter=meter, tracker=tracker,
        stop_event=threading.Event(), brake_event=threading.Event(),
        pairs_lock=threading.Lock(),
    )

    # startup
    if not _startup_auth(auth):
        write_app_status("auth_failed", "Startup auth failed")
        logger.error("Could not authenticate at startup — exiting")
        return
    auth.start_heartbeat()
    try:
        daily_maintenance(ctx, reauth=False)
    except Exception as e:
        logger.exception("Initial daily_maintenance failed: %s", e)

    workers: dict[str, SymbolWorker] = {}
    supervise_workers(workers, config, ctx)
    write_app_status("started", f"{len(workers)} worker(s) spawned")

    current_day = today_ist()
    did_maintenance_today = True        # startup already ran it

    # ── supervisor loop ─────────────────────────────────────────────────────
    while True:
        try:
            now = now_ist()
            today = now.date()

            if today != current_day:
                current_day = today
                did_maintenance_today = False

            # daily maintenance window (~00:10) — pt 1
            if not did_maintenance_today and now.time() >= DAILY_MAINTENANCE_AT:
                try:
                    daily_maintenance(ctx, reauth=True)
                    did_maintenance_today = True
                    log_strategy_event("SYSTEM", "LOOP1", "DAILY_MAINTENANCE_OK")
                except Exception as e:
                    logger.exception("daily_maintenance failed: %s", e)
                    log_strategy_event("SYSTEM", "LOOP1", "DAILY_MAINTENANCE_FAIL",
                                       details=str(e)[:200])

            # auth health (pt 2)
            if not _ensure_auth(auth):
                write_app_status("auth_failed", "Auth health check failing")

            # re-read config → brake (pt 4) + enable/disable
            try:
                config = load_config()
            except Exception:
                pass
            if config.get("brake", 0) == 1:
                ctx.brake_event.set()
                write_app_status("brake", "Brake ON — workers paused")
            else:
                ctx.brake_event.clear()

            # supervise workers
            supervise_workers(workers, config, ctx)

            if not ctx.brake_event.is_set():
                write_app_status(
                    "running",
                    f"{sum(w.is_alive() for w in workers.values())} worker(s) alive",
                    holidays_loaded=len(ctx.holidays),
                )

        except KeyboardInterrupt:
            logger.info("Shutdown requested (Ctrl+C)")
            ctx.stop_event.set()
            auth.stop_heartbeat()
            for w in workers.values():
                w.join(timeout=5)
            write_app_status("stopped", "Shutdown by user")
            break
        except Exception as e:
            logger.exception("Supervisor error: %s", e)

        sleep(SUPERVISOR_TICK)


if __name__ == "__main__":
    main()
