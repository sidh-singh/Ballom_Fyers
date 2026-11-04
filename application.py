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

import constants
from constants import (
    SYMBOLS_JSON, SUPERVISOR_TICK, DAILY_MAINTENANCE_AT, MARKET_HOURS, now_ist, today_ist,
)
from fyers_auth import FyersAuth
from fyers_api import FyersAPI
from api_meter import meter
from symbol_worker import SymbolWorker
from position_tracker import PositionTracker
from state_writer import write_app_status, log_strategy_event, write_account_state

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
    commodity_df: object = None
    prime_renewal: str = ""
    _last_money: dict = field(default_factory=dict)   # last balance/positions snapshot (reused when markets closed)

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


def daily_maintenance(ctx: EngineContext, reauth: bool, config: dict | None = None) -> None:
    """Re-auth (optional) + re-download CSV(s) + refresh holidays (pt 1)."""
    if reauth:
        ctx.auth.get_model(force=True)
    ctx.option_df = FyersAPI.download_option_data()

    # MCX commodity master — only when a commodity symbol is enabled
    if config and any(s.get("asset_type") == "COMMODITY" and s.get("enabled")
                      for s in config.get("symbols", [])):
        try:
            ctx.commodity_df = FyersAPI.download_commodity_data()
        except Exception as e:
            logger.warning("commodity CSV download failed: %s", e)

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


def _markets_open(ctx: EngineContext, config: dict) -> bool:
    """True if any enabled symbol's market window is open right now (coarse static hours:
    INDEX 09:15–15:30, COMMODITY 09:00–23:30). Used to skip read-API polling off-hours."""
    if not ctx.is_trading_day(now_ist().date()):
        return False
    t = now_ist().time()
    for entry in config.get("symbols", []):
        if not entry.get("enabled"):
            continue
        hrs = MARKET_HOURS.get(entry.get("asset_type", "INDEX"), MARKET_HOURS["INDEX"])
        if hrs["open"] <= t <= hrs["close"]:
            return True
    return False


def _write_account(ctx: EngineContext, markets_open: bool = True) -> None:
    """Snapshot balance/equity/margin/unrealized (+ API plan & renewal) for the dashboard.
    When all markets are CLOSED we SKIP the balance/positions read-API calls (nothing moves while
    the market is shut) and reuse the last snapshot — so no unnecessary API traffic off-hours. The
    plan badge + renewal countdown (no API) still refresh, and a market_open flag is published."""
    renewal_days = None
    if ctx.prime_renewal:
        try:
            renewal_days = (date.fromisoformat(ctx.prime_renewal) - today_ist()).days
        except Exception:
            renewal_days = None
    if markets_open:
        bal = ctx.api.get_balance()
        resp = ctx.api.positions()
        opens = resp.get("netPositions", []) if isinstance(resp, dict) else []
        unreal = sum(float(p.get("pl", 0) or 0) for p in opens)
        margin = sum(abs(int(p.get("netQty", 0))) * float(p.get("netAvg", 0) or 0) for p in opens)
        ctx._last_money = {
            "balance": round(bal, 2), "unrealized": round(unreal, 2),
            "equity": round(bal + unreal, 2), "margin_used": round(margin, 2),
            "open_positions": len(opens),
        }
    money = ctx._last_money or {
        "balance": 0, "unrealized": 0, "equity": 0, "margin_used": 0, "open_positions": 0}
    write_account_state({
        **money,
        "api_plan": constants.API_PLAN,
        "prime_renewal_days": renewal_days,
        "market_open": bool(markets_open),
    })


def detect_api_plan(ctx: EngineContext) -> str:
    """Probe a Prime-only capability: a >50-symbol quotes call succeeds only on Prime.
    Best-effort — falls back to 'standard' (the safe default) on any doubt."""
    try:
        df = ctx.option_df
        if df is None:
            return "standard"
        syms = list(df["Symbol ticker"].dropna().astype(str).head(60))
        if len(syms) < 55:
            return "standard"
        resp = ctx.api.safe_api_call(ctx.api.model.quotes, data={"symbols": ",".join(syms)})
        if isinstance(resp, dict) and resp.get("s") == "ok" and len(resp.get("d", [])) > 50:
            return "prime"
    except Exception as e:
        logger.warning("API-plan probe failed (assuming standard): %s", e)
    return "standard"


def _resolve_plan(ctx: EngineContext, config: dict) -> None:
    """Set the API plan from config ('standard'|'prime'|'auto') → limits + brokerage."""
    plan = str(config.get("api_plan", "standard")).lower()
    if plan == "auto":
        plan = detect_api_plan(ctx)
        logger.info("API plan auto-detected: %s", plan)
    constants.set_api_plan(plan)
    ctx.prime_renewal = str(config.get("prime_renewal", "") or "")
    logger.info("API plan = %s | DATA %s/day | brokerage ₹%s/order",
                constants.API_PLAN, constants.active_api_limits()["DATA"]["day"],
                constants.brokerage_per_order())


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
        daily_maintenance(ctx, reauth=False, config=config)
    except Exception as e:
        logger.exception("Initial daily_maintenance failed: %s", e)
    _resolve_plan(ctx, config)

    workers: dict[str, SymbolWorker] = {}
    supervise_workers(workers, config, ctx)
    write_app_status("started", f"{len(workers)} worker(s) spawned")

    current_day = today_ist()
    did_maintenance_today = True        # startup already ran it
    try:
        cfg_mtime = SYMBOLS_JSON.stat().st_mtime
    except Exception:
        cfg_mtime = None

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
                    daily_maintenance(ctx, reauth=True, config=config)
                    did_maintenance_today = True
                    log_strategy_event("SYSTEM", "LOOP1", "DAILY_MAINTENANCE_OK")
                except Exception as e:
                    logger.exception("daily_maintenance failed: %s", e)
                    log_strategy_event("SYSTEM", "LOOP1", "DAILY_MAINTENANCE_FAIL",
                                       details=str(e)[:200])

            # auth health (pt 2)
            if not _ensure_auth(auth):
                write_app_status("auth_failed", "Auth health check failing")

            # re-read config only when symbols.json changed on disk (lighter loop)
            try:
                _m = SYMBOLS_JSON.stat().st_mtime
                if _m != cfg_mtime:
                    config = load_config()
                    cfg_mtime = _m
            except Exception:
                pass
            if config.get("brake", 0) == 1:
                ctx.brake_event.set()
                write_app_status("brake", "Brake ON — workers paused")
            else:
                ctx.brake_event.clear()

            # supervise workers
            supervise_workers(workers, config, ctx)
            try:
                # Off-hours we skip the balance/positions read-API poll (nothing changes when the
                # market is shut) — only auth keep-alive + daily reauth run outside market hours.
                _write_account(ctx, _markets_open(ctx, config))
            except Exception:
                pass

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
