"""
application.py — dev_scanner branch entry point.

Usage:  python application.py [demo|live]

Architecture
────────────
  Infinite while-loop (runs 24×7):
    Step 1 — Day-change detection  → re-auth token + re-download CSVs
    Step 2 — Auth health           → verify / retry auth
    Step 3 — Holiday check         → skip scan on NSE holidays
    Step 4 — Hourly scan trigger   → scan best CE/PE pairs for NIFTY index
    Step 5 — Write results         → option_pairs.json on C: drive
    Step 6 — Sleep & repeat

Responsibilities (this branch ONLY):
  • Fyers authentication (via fyers_auth.py)
  • Hourly index option CE/PE pair scanning (via fyers_api.py)
  • Writing option_pairs.json for dev_trading to consume
  • Writing state files for dev_front_end dashboard

Skips scanning when:
  • Today is a holiday / weekend
  • Outside index market hours (09:15 – 15:30)
  • Open positions exist for a symbol (read from dev_trading's position_state.json)
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path
from time import sleep

from fyers_auth import FyersAuth
from fyers_api import FyersAPI
from constants import (
    SYMBOLS_JSON,
    OPTION_PAIRS_JSON,
    INDICES_START,
    INDICES_END,
    STRATEGY_HEDGE_INDEX,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    log_strategy_event,
    POSITION_STATE_FILE,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-14s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("application")

# ═══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

POLL_INTERVAL = 30   # seconds between each main-loop iteration


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — config & file I/O
# ═══════════════════════════════════════════════════════════════════════════════

def load_symbols_config() -> dict:
    """Load symbols.json (indices config)."""
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically via temp-file + move."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=4)
        shutil.move(tmp, str(path))
    except Exception:
        if Path(tmp).exists():
            Path(tmp).unlink()
        raise


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — position check (reads dev_trading's state file)
# ═══════════════════════════════════════════════════════════════════════════════

def get_symbols_with_open_positions() -> set[str]:
    """Return the set of symbols that have open positions.

    Reads position_state.json (written by dev_trading branch) and
    collects the 'symbol' field from every row whose netQty != 0.

    Returns an empty set on any read error so the scanner defaults
    to scanning when the file is missing or corrupt.
    """
    try:
        if not POSITION_STATE_FILE.exists():
            return set()
        with open(POSITION_STATE_FILE, "r") as f:
            data = json.load(f)
        open_symbols: set[str] = set()
        for pos in data.get("positions", []):
            net_qty = pos.get("netQty", pos.get("qty", 0))
            if int(net_qty) == 0:
                continue
            pos_symbol = pos.get("symbol", "").upper()
            open_symbols.add(pos_symbol)
        return open_symbols
    except Exception:
        return set()


def _symbol_has_open_position(symbol_key: str, open_positions: set[str]) -> bool:
    """Check if *symbol_key* matches any open position symbol."""
    key_upper = symbol_key.upper()
    return any(key_upper in pos_sym for pos_sym in open_positions)


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — trading-day awareness
# ═══════════════════════════════════════════════════════════════════════════════

def is_trading_day(
    today: date,
    holidays: set[str],
    special_sessions: list[dict],
) -> bool:
    """True if *today* is a valid trading day."""
    today_str = today.isoformat()
    if any(ss.get("date") == today_str for ss in special_sessions):
        return True
    if today.weekday() >= 5:
        return False
    if today_str in holidays:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — auth with retries
# ═══════════════════════════════════════════════════════════════════════════════

def _startup_auth(auth: FyersAuth, max_retries: int = 10) -> bool:
    """Attempt initial authentication with generous retries."""
    for attempt in range(1, max_retries + 1):
        try:
            auth.get_model(force=False)
            logger.info("Startup auth succeeded (attempt %d)", attempt)
            return True
        except Exception as e:
            logger.warning("Startup auth %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                sleep(min(30 * attempt, 300))
    return False


def _day_change_auth(auth: FyersAuth) -> bool:
    """Force re-auth on day change. Returns True on success."""
    for attempt in range(1, 6):
        try:
            auth.get_model(force=True)
            logger.info("Day-change re-auth succeeded (attempt %d)", attempt)
            return True
        except Exception as e:
            logger.warning("Day-change auth %d/5 failed: %s", attempt, e)
            if attempt < 5:
                sleep(min(30 * attempt, 120))
    return False


def _check_token_health(auth: FyersAuth) -> bool:
    """Ensure the token is still valid; attempt re-auth if not."""
    if auth.is_authenticated:
        return True
    try:
        auth.get_model(force=False)
        return True
    except Exception as e:
        logger.warning("Token health check failed: %s — re-authenticating", e)
        try:
            auth.re_authenticate()
            return True
        except Exception as re_err:
            logger.error("Re-auth also failed: %s", re_err)
            return False


# ═══════════════════════════════════════════════════════════════════════════════
#  DAILY SETUP  (auth + CSV download + holidays)
# ═══════════════════════════════════════════════════════════════════════════════

def daily_setup(auth: FyersAuth, force_auth: bool = False):
    """
    Run once per new trading day (or at startup):
      1. Authenticate (writes token to C:/Ballom_FYR/fyers_token.json)
      2. Download NSE F&O symbol CSV
      3. Fetch / cache trading holidays for the year
    Returns (option_df, holidays, special_sessions).
    """
    auth.get_model(force=force_auth)
    option_df = FyersAPI.download_option_data()
    holidays, special_sessions = FyersAPI.load_holiday_set()
    return option_df, holidays, special_sessions


# ═══════════════════════════════════════════════════════════════════════════════
#  INDEX PAIR SCANNING
# ═══════════════════════════════════════════════════════════════════════════════

def scan_index_pairs(
    api: FyersAPI,
    indices: list,
    option_df,
    open_positions: set[str] | None = None,
) -> dict:
    """
    Scan all index symbols from symbols.json and return best CE/PE pairs.

    Returns dict keyed by symbol_key (e.g. "NIFTY") with structure:
        { "CE": ..., "PE": ..., "CE_Strike": ..., "PE_Strike": ...,
          "Expiry": ..., "Trend_Score": ..., "VIX": ...,
          "indices": ..., "qty": ..., "hedge": ... }
    """
    result: dict = {}
    open_positions = open_positions or set()

    for entry in indices:
        symbol_key = entry["symbol"]
        underlying = entry["indices"]

        # ── Skip if dev_trading has an open position for this symbol ────
        if _symbol_has_open_position(symbol_key, open_positions):
            log_strategy_event(
                symbol_key, "SCAN", "SKIP_OPEN_POSITION",
                details=f"Position open for {symbol_key} — skipping scan",
            )
            continue

        qty_times = entry.get("qty_times", 1)
        hedge     = entry.get("hedge", STRATEGY_HEDGE_INDEX)

        try:
            pair = api.fetch_option_pair(underlying, asset_type="INDEX")
        except Exception as e:
            log_strategy_event(
                symbol_key, "SCAN", "INDEX_ERROR", details=str(e),
            )
            continue

        debug_trail = pair.get("Debug", "")
        if not pair.get("Recommended"):
            if not pair.get("CE_Symbol") or not pair.get("PE_Symbol"):
                log_strategy_event(
                    symbol_key, "SCAN", "SKIP_INDEX",
                    details=f"{pair.get('Message', 'skipped')} || {debug_trail}",
                )
                continue
            log_strategy_event(
                symbol_key, "SCAN", "FALLBACK_INDEX",
                details=f"Using fallback pair || {debug_trail}",
            )

        try:
            lot = FyersAPI.get_lot_size(pair["CE_Symbol"], option_df)
        except ValueError as e:
            log_strategy_event(
                symbol_key, "SCAN", "LOT_ERROR", details=str(e),
            )
            continue

        qty    = int(lot * qty_times)
        ce_sym = pair.get("CE_Symbol", "")
        pe_sym = pair.get("PE_Symbol", "")

        if not ce_sym or not pe_sym:
            log_strategy_event(
                symbol_key, "SCAN", "INVALID_PAIR",
                details=f"Empty symbol: CE={ce_sym!r} PE={pe_sym!r}",
            )
            continue

        result[symbol_key] = {
            "CE": ce_sym,
            "PE": pe_sym,
            "CE_Strike": pair["CE_Strike"],
            "PE_Strike": pair["PE_Strike"],
            "Expiry": pair["Expiry"],
            "Trend_Score": pair["Trend_Score"],
            "VIX": pair["VIX"],
            "indices": underlying,
            "qty": qty,
            "hedge": hedge,
        }
        log_strategy_event(
            symbol_key, "SCAN", "INDEX_PAIR_FOUND", qty=qty,
            details=(
                f"CE={ce_sym} PE={pe_sym} Exp={pair['Expiry']} || {debug_trail}"
            ),
        )

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "demo").lower()
    logger.info("=" * 60)
    logger.info("  dev_scanner starting — mode=%s", mode)
    logger.info("=" * 60)

    # ── Configure state writer ─────────────────────────────────────────────
    configure_state_writer(mode)

    # ── Load symbols config ────────────────────────────────────────────────
    config  = load_symbols_config()
    indices = config.get("indices", [])

    # ── Initialize Fyers auth + API ────────────────────────────────────────
    auth = FyersAuth(read_only=False)   # scanner is the primary auth writer
    api  = FyersAPI(auth)

    # ── Initial authentication + daily setup ───────────────────────────────
    current_day = date.today()
    option_df   = None
    holidays: set[str]     = set()
    special_sessions: list = []
    setup_ok    = False
    auth_ok     = False

    auth_ok = _startup_auth(auth)
    if auth_ok:
        log_strategy_event(
            "SYSTEM", "INIT", "AUTH_OK",
            details=f"Startup auth succeeded for {current_day} (mode={mode})",
        )
        auth.start_heartbeat()

        try:
            option_df, holidays, special_sessions = daily_setup(
                auth, force_auth=False,
            )
            setup_ok = True
            log_strategy_event(
                "SYSTEM", "INIT", "DAILY_SETUP_OK",
                details=f"Auth + CSV done for {current_day}",
            )
        except Exception as e:
            log_strategy_event(
                "SYSTEM", "INIT", "DAILY_SETUP_FAIL", details=str(e),
            )
    else:
        log_strategy_event(
            "SYSTEM", "INIT", "AUTH_FAIL",
            details=f"Startup auth FAILED for {current_day} — will retry",
        )

    # Pre-fetch holidays for this year (and next if December)
    try:
        FyersAPI.fetch_trading_holidays(current_day.year)
        if current_day.month == 12:
            FyersAPI.fetch_trading_holidays(current_day.year + 1)
    except Exception as e:
        log_strategy_event(
            "SYSTEM", "INIT", "HOLIDAY_FETCH_FAIL", details=str(e),
        )

    write_app_status(
        mode, str(current_day), status="started",
        message=f"Scanner started — setup {'OK' if setup_ok else 'FAILED'}",
    )

    last_scan_hour = -1   # -1 forces a scan on the very first eligible hour

    logger.info(
        "Entering main loop (poll=%ds, auth=%s, setup=%s)",
        POLL_INTERVAL,
        "OK" if auth_ok else "PENDING",
        "OK" if setup_ok else "PENDING",
    )

    # ══════════════════════════════════════════════════════════════════════════
    #  INFINITE LOOP
    # ══════════════════════════════════════════════════════════════════════════
    while True:
        try:
            today = date.today()
            now   = datetime.now()
            current_time = now.time()

            # ── Step 1: Day-change → re-auth + re-download CSVs ────────────
            if today != current_day:
                logger.info("Day change: %s → %s", current_day, today)
                current_day = today
                setup_ok = False
                last_scan_hour = -1

                auth_ok = _day_change_auth(auth)
                if auth_ok:
                    try:
                        option_df, holidays, special_sessions = daily_setup(
                            auth, force_auth=False,
                        )
                        setup_ok = True
                        log_strategy_event(
                            "SYSTEM", "SCANNER", "NEW_DAY_SETUP_OK",
                            details=f"Day change — re-auth + CSV for {current_day}",
                        )
                    except Exception as e:
                        log_strategy_event(
                            "SYSTEM", "SCANNER", "NEW_DAY_SETUP_FAIL",
                            details=str(e),
                        )
                else:
                    log_strategy_event(
                        "SYSTEM", "AUTH", "DAY_CHANGE_FAIL",
                        details=f"Re-auth FAILED for {today}",
                    )

                # Refresh holiday calendar
                try:
                    FyersAPI.fetch_trading_holidays(today.year)
                    if today.month == 12:
                        FyersAPI.fetch_trading_holidays(today.year + 1)
                except Exception:
                    pass

                write_app_status(
                    mode, str(current_day), status="new_day",
                    message=f"Day change — setup {'OK' if setup_ok else 'FAILED'}",
                )

            # ── Step 2: Retry auth + setup if not yet successful ───────────
            if not auth_ok:
                try:
                    auth.get_model(force=True)
                    auth_ok = True
                    auth.start_heartbeat()
                    log_strategy_event(
                        "SYSTEM", "AUTH", "RETRY_OK",
                        details="Auth retry succeeded",
                    )
                except Exception as e:
                    write_app_status(
                        mode, str(current_day), status="auth_failed",
                        message=f"Auth failing: {str(e)[:80]}",
                    )
                    sleep(30)
                    continue

            if not setup_ok:
                try:
                    option_df, holidays, special_sessions = daily_setup(
                        auth, force_auth=False,
                    )
                    setup_ok = True
                    log_strategy_event(
                        "SYSTEM", "SCANNER", "SETUP_RETRY_OK",
                        details="daily_setup retry succeeded",
                    )
                except Exception as e:
                    write_app_status(
                        mode, str(current_day), status="setup_failed",
                        message=f"daily_setup failing: {str(e)[:80]}",
                    )
                    sleep(30)
                    continue

            # ── Step 3: Token health check ─────────────────────────────────
            if not _check_token_health(auth):
                auth_ok = False
                write_app_status(
                    mode, str(current_day), status="auth_failed",
                    message="Token health check failed — retrying",
                )
                sleep(10)
                continue

            # ── Step 4: Holiday check → skip scanning on holidays ──────────
            if not is_trading_day(today, holidays, special_sessions):
                write_app_status(
                    mode, str(current_day), status="holiday",
                    message="Market holiday — scanner idle",
                )
                sleep(60)
                continue

            # ── Step 5: Brake check (kill-switch in symbols.json) ──────
            if config.get("brake", 0) == 1:
                write_app_status(
                    mode, str(current_day), status="brake",
                    message="Brake ON in symbols.json — scanning paused",
                )
                sleep(POLL_INTERVAL)
                continue
            # Re-read config each loop in case symbols.json changed on disk
            try:
                config = load_symbols_config()
                indices = config.get("indices", [])
            except Exception:
                pass  # keep previous config on read error

            # ── Step 6: Hourly scan trigger ────────────────────────────
            current_hour = now.hour

            if current_hour != last_scan_hour:
                open_positions = get_symbols_with_open_positions()
                in_idx_window = INDICES_START <= current_time <= INDICES_END

                if in_idx_window and option_df is not None:
                    write_app_status(
                        mode, str(current_day), status="scanning",
                        in_indices_window=True,
                        message=f"Hourly scan at {now.strftime('%H:%M')}",
                    )

                    idx_count = 0
                    try:
                        idx_result = scan_index_pairs(
                            api, indices, option_df, open_positions,
                        )
                        if idx_result:
                            _write_json_atomic(OPTION_PAIRS_JSON, idx_result)
                            idx_count = len(idx_result)
                            log_strategy_event(
                                "SYSTEM", "SCAN", "INDEX_SCAN_DONE",
                                details=f"{idx_count} index pair(s) written",
                            )
                        else:
                            log_strategy_event(
                                "SYSTEM", "SCAN", "INDEX_SCAN_EMPTY",
                                details=(
                                    "Scan returned 0 pairs — keeping existing "
                                    "option_pairs.json"
                                ),
                            )
                    except Exception as e:
                        if FyersAuth.is_auth_failure_exception(e):
                            log_strategy_event(
                                "SYSTEM", "AUTH", "REAUTH_INDEX_SCAN",
                                details=f"Auth failure during scan: {e}",
                            )
                            try:
                                auth.re_authenticate()
                                idx_result = scan_index_pairs(
                                    api, indices, option_df, open_positions,
                                )
                                if idx_result:
                                    _write_json_atomic(
                                        OPTION_PAIRS_JSON, idx_result,
                                    )
                                    idx_count = len(idx_result)
                                    log_strategy_event(
                                        "SYSTEM", "SCAN", "INDEX_SCAN_DONE",
                                        details=(
                                            f"{idx_count} pair(s) written "
                                            f"(after re-auth)"
                                        ),
                                    )
                            except Exception as re_err:
                                log_strategy_event(
                                    "SYSTEM", "SCAN", "INDEX_SCAN_FAIL",
                                    details=f"Re-auth retry failed: {re_err}",
                                )
                        else:
                            log_strategy_event(
                                "SYSTEM", "SCAN", "INDEX_SCAN_FAIL",
                                details=str(e),
                            )

                    last_scan_hour = current_hour
                    next_hour = (current_hour + 1) % 24
                    write_app_status(
                        mode, str(current_day), status="idle",
                        indices_scanned=idx_count > 0,
                        in_indices_window=True,
                        message=(
                            f"Scan done — IDX={idx_count} | "
                            f"next at {next_hour:02d}:00"
                        ),
                    )
                else:
                    # Outside index market window
                    last_scan_hour = current_hour
                    write_app_status(
                        mode, str(current_day), status="idle",
                        message=f"Outside market hours ({now.strftime('%H:%M')})",
                    )

        except KeyboardInterrupt:
            logger.info("Shutdown requested (Ctrl+C)")
            auth.stop_heartbeat()
            write_app_status(
                mode, str(current_day), status="stopped",
                message="Shutdown by user (Ctrl+C)",
            )
            break

        except Exception as e:
            logger.exception("Unhandled error in main loop: %s", e)
            log_strategy_event(
                "SYSTEM", "ERROR", "MAIN_LOOP_EXCEPTION",
                details=str(e)[:200],
            )
            if FyersAuth.is_auth_failure_exception(e):
                auth_ok = False
                auth.invalidate()

        sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
