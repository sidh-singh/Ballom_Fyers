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
from time import sleep, monotonic

import constants
from constants import (
    SYMBOLS_JSON, SUPERVISOR_TICK, DAILY_MAINTENANCE_AT, MARKET_HOURS,
    MARKET_STATUS_STALE_S, COMMANDS_DIR, now_ist, today_ist,
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
    special_sessions: list = field(default_factory=list)   # auto-detected live (see _refresh_special_sessions)
    option_df: object = None
    commodity_df: object = None
    prime_renewal: str = ""
    _last_money: dict = field(default_factory=dict)   # last balance/positions snapshot (reused when markets closed)
    market_status: dict = field(default_factory=dict)  # live {asset_type: is_open} from the broker
    market_status_ts: float = 0.0                      # monotonic() of the last good market_status poll

    def is_trading_day(self, d: date) -> bool:
        """Weekend + NSE-holiday aware (special sessions override)."""
        ds = d.isoformat()
        if any(s.get("date") == ds for s in self.special_sessions):
            return True
        if d.weekday() >= 5:
            return False
        return ds not in self.holidays

    def is_segment_open(self, asset_type: str) -> bool | None:
        """Broker-authoritative live open/close for this asset's segment, or None when there is no
        FRESH reading (poll failed / too old / segment missing) — in which case the caller falls
        back to the static schedule. Refreshed every tick by the supervisor. This is the signal
        that lets the engine trade Diwali Muhurat / Budget / any unusual day with no hardcoded dates."""
        if not self.market_status:
            return None
        if monotonic() - self.market_status_ts > MARKET_STATUS_STALE_S:
            return None
        return self.market_status.get(asset_type)


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
    """True if any enabled symbol's market is open right now. Prefers the broker's LIVE status
    (so a Muhurat / Budget / any unusual session counts too); falls back to the static schedule
    (INDEX 09:15–15:30, COMMODITY 09:00–23:30) when no fresh live reading exists. Used to skip
    read-API polling off-hours."""
    enabled = [e for e in config.get("symbols", []) if e.get("enabled")]
    # live broker status first
    for entry in enabled:
        if ctx.is_segment_open(entry.get("asset_type", "INDEX")) is True:
            return True
    # fallback: static schedule
    if not ctx.is_trading_day(now_ist().date()):
        return False
    t = now_ist().time()
    for entry in enabled:
        hrs = MARKET_HOURS.get(entry.get("asset_type", "INDEX"), MARKET_HOURS["INDEX"])
        if hrs["open"] <= t <= hrs["close"]:
            return True
    return False


def _refresh_market_status(ctx: EngineContext) -> None:
    """Poll the broker's live market_status and cache it on the context (only overwrite on a good
    read, so a transient failure keeps the last value until it goes stale — see is_segment_open)."""
    try:
        ms = ctx.api.get_market_status()
    except Exception as e:
        logger.debug("market_status poll failed: %s", e)
        return
    if ms:
        ctx.market_status = ms
        ctx.market_status_ts = monotonic()


def _refresh_special_sessions(ctx: EngineContext) -> None:
    """Auto-detect an unusual OPEN day (Diwali Muhurat / Union Budget / anything NSE keeps open) and
    inject TODAY as a special session so the data fetch + is_trading_day include it. Driven entirely
    by the live broker status — no hardcoded dates/times. On a normal scheduled day, or when nothing
    is open, special_sessions is cleared. The actual trade gate still uses the live status directly
    (symbol_worker._should_trade_now); this only widens the data window to cover today."""
    today = now_ist().date()
    ds = today.isoformat()
    any_open = any(bool(v) for v in (ctx.market_status or {}).values())
    scheduled = (today.weekday() < 5 and ds not in ctx.holidays)
    if any_open and not scheduled:
        ctx.special_sessions = [{
            "date": ds,
            "name": "Auto-detected special session (live market status)",
            "open": "00:00", "close": "23:59",   # wide → data fetch covers all of today
        }]
    else:
        ctx.special_sessions = []


def _market_summary(ctx: EngineContext, config: dict) -> dict:
    """Classify TODAY's market for the dashboard status badge — purely from the live broker
    status + holiday calendar the engine already tracks (zero hardcoded dates). Exactly one of:
      • open    — a segment this bot trades is OPEN right now (normal session)
      • unusual — exchange OPEN on a statically-closed day (Diwali Muhurat / Union-Budget / any
                  special session the engine auto-detected off the live market status)
      • holiday — weekend or NSE holiday, nothing open
      • closed  — a normal trading day, but outside market hours right now
    `live` says whether the call came from a FRESH broker reading (vs the static-schedule
    fallback, used only when the market_status poll is stale/missing). Written into
    app_status.json so the dashboard renders it next to the engine-status dot."""
    now = now_ist()
    today = now.date()
    ds = today.isoformat()

    def _hrs(a):
        return MARKET_HOURS.get(a, MARKET_HOURS["INDEX"])

    assets = sorted({s.get("asset_type", "INDEX")
                     for s in config.get("symbols", []) if s.get("enabled")}) or ["INDEX"]
    seg_live = {a: ctx.is_segment_open(a) for a in assets}   # True / False / None (no fresh reading)
    fresh = any(v is not None for v in seg_live.values())
    open_live = any(v is True for v in seg_live.values())

    is_holiday = ds in ctx.holidays
    is_weekend = today.weekday() >= 5
    scheduled = not (is_weekend or is_holiday)

    t = now.time()
    within_hours = any(_hrs(a)["open"] <= t <= _hrs(a)["close"] for a in assets)
    open_now = open_live if fresh else (scheduled and within_hours)

    if open_now and not scheduled:
        state, label = "unusual", "Unusual open"
        detail = "Exchange open on a non-scheduled day — special session (auto-detected)"
    elif open_now:
        state, label = "open", "Market open"
        detail = "Normal trading session in progress"
    elif not scheduled:
        state, label = "holiday", "Holiday"
        detail = "Weekend — markets shut" if is_weekend else "NSE holiday — markets shut"
    else:
        state, label = "closed", "Market closed"
        first = min(_hrs(a)["open"] for a in assets)
        detail = (f"Pre-open — trading starts {first.strftime('%H:%M')}" if t < first
                  else f"Closed for the day — reopens {first.strftime('%H:%M')}")

    return {
        "state": state, "label": label, "detail": detail, "live": fresh, "date": ds,
        "segments": {a: (("open" if v else "closed") if v is not None else "unknown")
                     for a, v in seg_live.items()},
    }


def _process_commands(ctx: EngineContext, workers: dict, config: dict) -> None:
    """Execute dashboard-issued commands. Each is a JSON file in COMMANDS_DIR, processed once then
    deleted (cross-process channel: the dashboard writes, the engine executes — like the brake flag).
      • force_close: flag the symbol's worker to flatten its open basket (live = real sell order,
        demo = simulated) on its next tick.
      • add_balance: top up the DEMO balance (ignored on live, which has no add_balance)."""
    if not COMMANDS_DIR.exists():
        return
    for f in sorted(COMMANDS_DIR.glob("*.json")):
        cmd = None
        try:
            cmd = json.loads(f.read_text())
        except Exception:
            pass
        try:
            action = (cmd or {}).get("action")
            if action == "force_close":
                sym = cmd.get("symbol")
                w = workers.get(sym)
                if w is not None:
                    w.force_close = True
                    logger.info("force-close requested for %s (dashboard)", sym)
                    log_strategy_event(sym, "DASH", "FORCE_CLOSE_REQUESTED")
                else:
                    logger.warning("force-close: unknown/!running symbol %s", sym)
            elif action == "add_balance":
                amt = float(cmd.get("amount") or 0)
                if amt > 0 and hasattr(ctx.api, "add_balance"):
                    new_bal = ctx.api.add_balance(amt)
                    logger.info("demo balance +Rs%.2f (dashboard) -> Rs%s", amt, new_bal)
                    log_strategy_event("SYSTEM", "DASH", "ADD_BALANCE",
                                       details=f"+Rs{amt:.2f} -> Rs{new_bal}")
        except Exception as e:
            logger.exception("command %s failed: %s", f.name, e)
        finally:
            try:
                f.unlink()
            except Exception:
                pass


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

    # prime the live market status so workers have it from their first tick
    _refresh_market_status(ctx)
    _refresh_special_sessions(ctx)

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

            # live broker market status → auto-detects Muhurat / Budget / any unusual open day
            # (no hardcoded dates); drives the worker trade gate + widens today's data window.
            _refresh_market_status(ctx)
            _refresh_special_sessions(ctx)

            # re-read config only when symbols.json changed on disk (lighter loop)
            try:
                _m = SYMBOLS_JSON.stat().st_mtime
                if _m != cfg_mtime:
                    config = load_config()
                    cfg_mtime = _m
            except Exception:
                pass
            market = _market_summary(ctx, config)   # today: open / closed / holiday / unusual
            if config.get("brake", 0) == 1:
                ctx.brake_event.set()
                write_app_status("brake", "Brake ON — workers paused", market=market)
            else:
                ctx.brake_event.clear()

            # supervise workers
            supervise_workers(workers, config, ctx)
            # dashboard commands (force-close / add demo balance)
            try:
                _process_commands(ctx, workers, config)
            except Exception:
                pass
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
                    market=market,
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
