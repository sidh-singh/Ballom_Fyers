"""
application.py — SHA Signal Analysis updater service (NIFTY ONLY).

Branch: dev_updater_nifty
Usage:  python application.py [demo|live]

Architecture
────────────
Forever Loop (runs 24×7, every 3 seconds):
  Step 1 — Load Fyers token from C:/Ballom_FYR/fyers_token.json
           (written by dev_scanner).  If invalid, flag expired so
           scanner re-authenticates.
  Step 2 — Day-change detection → reload token + holidays
  Step 3 — Read CE/PE pairs from option_pairs.json (from dev_scanner),
           filter to NIFTY only
  Step 4 — Fetch history candles for CE, PE, IDX at multiple timeframes:
             1min  (500 candles) — for SHA + 1min RSI
             5min  (200 candles) — for 5min RSI
             15min (100 candles) — for 15min RSI
             30min (100 candles) — for 30min RSI
             60min (100 candles) — for 1hr RSI
  Step 5 — Compute 6 SHAs on 1min data:
             Signal SHA (length=3) × CE, PE, IDX
             Trend SHA  (length=6) × CE, PE, IDX
  Step 6 — Compute RSI for all 5 timeframes × CE, PE, IDX
  Step 7 — Compute GAP% and SHA Relationship
  Step 8 — Dump signal_state.json for dashboard

Output:
  C:/Ballom_FYR/state/<mode>/signal_state.json
  C:/Ballom_FYR/state/<mode>/app_status.json
  C:/Ballom_FYR/state/<mode>/strategy_log/

Token sharing:
  Reads token from C:/Ballom_FYR/fyers_token.json (written by dev_scanner).
  NEVER performs TOTP login.  On auth failure, flags token as expired.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dt_time
from pathlib import Path
from time import sleep

import pandas as pd

from fyers_api import FyersAPI
from indicator import SmoothedHeikenAshi, RSI
from constants import (
    OPTION_PAIRS_JSON,
    INDICES_START,
    INDICES_END,
    SHA_LENGTH,
    SHA_MA_TYPE,
    SHA_TREND_LENGTH,
    SHA_TREND_MA_TYPE,
    SHA_TIMEFRAME,
    SHA_CANDLES,
    RSI_PERIOD,
    RSI_TIMEFRAMES,
    UPDATE_INTERVAL,
    ACTIVE_SYMBOLS,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    write_signal_state,
    log_strategy_event,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-14s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("application")


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — file I/O
# ═══════════════════════════════════════════════════════════════════════════════

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — token loading (read-only, from scanner's shared file)
# ═══════════════════════════════════════════════════════════════════════════════

def load_fyers_session(api: FyersAPI, force: bool = False) -> bool:
    """
    Load the Fyers token from the shared file written by dev_scanner.

    Updaters are strictly read-only — they NEVER perform TOTP login.
    Anti-collision jitter staggers file reads so that if the scanner is
    mid-write, updaters retry after a short randomized delay.

    Returns True if session is ready, False otherwise.
    """
    try:
        if api.load_token(force=force):
            return True
    except Exception:
        pass

    # Anti-collision jitter — scanner may be mid-write
    jitter = random.uniform(2, 10)
    log_strategy_event(
        "SYSTEM", "AUTH", "TOKEN_WAIT",
        details=f"Token unavailable — waiting {jitter:.1f}s for scanner to write",
    )
    sleep(jitter)

    # Retry after jitter
    try:
        if api.load_token(force=True):
            log_strategy_event(
                "SYSTEM", "AUTH", "TOKEN_LOADED_AFTER_WAIT",
                details="Token loaded from shared file after wait",
            )
            return True
    except Exception as e:
        log_strategy_event(
            "SYSTEM", "AUTH", "TOKEN_LOAD_FAIL",
            details=f"No valid token in shared file: {str(e)[:120]}",
        )
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS — holiday awareness
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
#  SHA SIGNAL COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

def get_symbol_details(
    raw_df: pd.DataFrame,
    sha_length: int = SHA_LENGTH,
    sha_type: str = SHA_MA_TYPE,
) -> tuple:
    """
    Compute Smoothed Heiken-Ashi on *raw_df* (OHLCV) and derive:
        lt_symbol_power  — count of bullish candles in last 7
        lt_symbol_list   — [1|0, ...] most-recent-first
        sha_debug        — last 7 SHA OHLC dicts (most-recent-first)

    Returns (lt_symbol_power, lt_symbol_list, sha_debug).
    """
    lt_sha = SmoothedHeikenAshi.calculate(
        df=raw_df,
        smooth_length=sha_length,
        smooth_ma_type=sha_type,
        after_smooth_length=sha_length,
        after_smooth_ma_type=sha_type,
    )

    threshold = 0
    lt_symbol_power = 0
    lt_symbol_list = []
    sha_debug = []

    for i in range(-1, -8, -1):
        sha_o = lt_sha["Open"].iloc[i]
        sha_h = lt_sha["High"].iloc[i]
        sha_l = lt_sha["Low"].iloc[i]
        sha_c = lt_sha["Close"].iloc[i]

        # Guard against NaN SHA values (insufficient candles)
        if math.isnan(sha_o) or math.isnan(sha_h) or math.isnan(sha_l) or math.isnan(sha_c):
            lt_symbol_list.append(0)
            sha_debug.append({
                "ts": str(raw_df["Timestamp"].iloc[i]) if "Timestamp" in raw_df.columns else "",
                "O": 0, "H": 0, "L": 0, "C": 0, "dir": "NaN",
            })
            continue

        ha_range = abs(sha_h - sha_l)
        if ha_range == 0:
            ha_range = 1e-9

        lt_diff = (sha_c - sha_o) / ha_range

        lt_sha_diff = 1 if lt_diff >= threshold else 0
        lt_symbol_list.append(lt_sha_diff)
        lt_symbol_power += lt_sha_diff

        ts = str(raw_df["Timestamp"].iloc[i]) if "Timestamp" in raw_df.columns else ""
        sha_debug.append({
            "ts": ts,
            "O": round(float(sha_o), 2),
            "H": round(float(sha_h), 2),
            "L": round(float(sha_l), 2),
            "C": round(float(sha_c), 2),
            "dir": "BULL" if lt_sha_diff == 1 else "BEAR",
        })

    return lt_symbol_power, lt_symbol_list, sha_debug


def get_trend_details(
    raw_df: pd.DataFrame,
    sha_length: int = SHA_TREND_LENGTH,
    sha_type: str = SHA_TREND_MA_TYPE,
) -> tuple:
    """Compute Trend SHA (longer-period) — same logic with longer SHA length."""
    return get_symbol_details(raw_df, sha_length=sha_length, sha_type=sha_type)


# ═══════════════════════════════════════════════════════════════════════════════
#  GAP% AND SHA RELATIONSHIP
# ═══════════════════════════════════════════════════════════════════════════════

def compute_sha_gap(signal_sha_debug: list, trend_sha_debug: list) -> dict:
    """
    Compute GAP% between Signal SHA and Trend SHA using means.

    GAP% = ((mean_signal_mid - mean_trend_mid) / mean_trend_mid) × 100
    """
    n = min(len(signal_sha_debug), len(trend_sha_debug))
    if n == 0:
        return {"gap_pct": 0.0, "signal_mean": 0.0, "trend_mean": 0.0, "per_candle": []}

    sig_mids = []
    trd_mids = []
    per_candle = []

    for i in range(n):
        sig = signal_sha_debug[i]
        trd = trend_sha_debug[i]

        sig_mid = (sig.get("H", 0) + sig.get("L", 0)) / 2
        trd_mid = (trd.get("H", 0) + trd.get("L", 0)) / 2

        sig_mids.append(sig_mid)
        trd_mids.append(trd_mid)

        if abs(trd_mid) > 1e-9:
            candle_gap = ((sig_mid - trd_mid) / abs(trd_mid)) * 100
        else:
            candle_gap = 0.0

        per_candle.append({
            "gap_pct": round(candle_gap, 2),
            "signal_mid": round(sig_mid, 2),
            "trend_mid": round(trd_mid, 2),
        })

    signal_mean = sum(sig_mids) / len(sig_mids)
    trend_mean = sum(trd_mids) / len(trd_mids)

    if abs(trend_mean) > 1e-9:
        gap_pct = ((signal_mean - trend_mean) / abs(trend_mean)) * 100
    else:
        gap_pct = 0.0

    return {
        "gap_pct": round(gap_pct, 2),
        "signal_mean": round(signal_mean, 2),
        "trend_mean": round(trend_mean, 2),
        "per_candle": per_candle,
    }


def compute_sha_relationship(gap_data: dict) -> dict:
    """
    Analyze the relationship between Signal SHA and Trend SHA.

    Returns dict with:
        status   : "DIVERGING" | "CONVERGING" | "PARALLEL" | "CLOSE"
        strength : 0.0 – 1.0
        avg_gap  : absolute mean-based GAP%
        delta    : change rate between recent and older halves
    """
    per_candle = gap_data.get("per_candle", [])
    gap_pct = gap_data.get("gap_pct", 0.0)

    if not per_candle or len(per_candle) < 2:
        return {"status": "UNKNOWN", "strength": 0.0, "avg_gap": 0.0, "delta": 0.0}

    avg_gap = abs(gap_pct)

    # CLOSE: SHAs nearly overlapping
    CLOSE_THRESHOLD = 1.0
    if avg_gap < CLOSE_THRESHOLD:
        strength = round(1.0 - avg_gap / CLOSE_THRESHOLD, 4)
        return {"status": "CLOSE", "strength": strength,
                "avg_gap": round(avg_gap, 2), "delta": 0.0}

    # Trend analysis: compare recent half vs older half
    abs_gaps = [abs(g["gap_pct"]) for g in per_candle]
    mid = len(abs_gaps) // 2
    recent = abs_gaps[:max(mid, 1)]
    older = abs_gaps[max(mid, 1):]

    avg_recent = sum(recent) / len(recent)
    avg_older = sum(older) / len(older) if older else avg_recent

    delta = avg_recent - avg_older

    PARALLEL_THRESHOLD = 0.5
    if abs(delta) < PARALLEL_THRESHOLD:
        strength = round(1.0 - abs(delta) / PARALLEL_THRESHOLD, 4)
        return {"status": "PARALLEL", "strength": strength,
                "avg_gap": round(avg_gap, 2), "delta": round(delta, 2)}
    elif delta > 0:
        strength = round(min(1.0, delta / 5.0), 4)
        return {"status": "DIVERGING", "strength": strength,
                "avg_gap": round(avg_gap, 2), "delta": round(delta, 2)}
    else:
        strength = round(min(1.0, abs(delta) / 5.0), 4)
        return {"status": "CONVERGING", "strength": strength,
                "avg_gap": round(avg_gap, 2), "delta": round(delta, 2)}


# ═══════════════════════════════════════════════════════════════════════════════
#  RSI COMPUTATION (single timeframe)
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> float | None:
    """Compute RSI on df, return None if insufficient data or NaN."""
    if len(df) <= period:
        return None
    val = RSI.calculate(df, length=period).iloc[-1]
    if math.isnan(val):
        return None
    return round(float(val), 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  PROCESS ONE SYMBOL PAIR  (fetch data → SHA → RSI → dump JSON)
# ═══════════════════════════════════════════════════════════════════════════════

def process_symbol(
    api: FyersAPI,
    symbol_key: str,
    ce_symbol: str,
    pe_symbol: str,
    underlying: str,
    market_type: str,
    holidays: set[str],
    special_sessions: list[dict],
) -> bool:
    """
    Fetch historical data, compute SHAs and multi-TF RSI, write signal_state.

    Fetches 15 data series in parallel:
      3 symbols (CE, PE, IDX) × 5 timeframes (1m, 5m, 15m, 30m, 1h)

    Returns True on success, False on error.
    """
    try:
        # ── Define all fetch tasks ─────────────────────────────────────────
        # Key format: "{leg}_{tf_label}"  e.g. "ce_1m", "idx_5m"
        fetch_tasks: dict[str, tuple[str, str, int]] = {}

        for leg, sym in [("ce", ce_symbol), ("pe", pe_symbol), ("idx", underlying)]:
            # 1min — for SHA + RSI
            fetch_tasks[f"{leg}_1m"] = (sym, SHA_TIMEFRAME, SHA_CANDLES)
            # Other timeframes — for RSI only
            for tf_label, tf_cfg in RSI_TIMEFRAMES.items():
                if tf_label == "1m":
                    continue  # already covered by SHA fetch
                fetch_tasks[f"{leg}_{tf_label}"] = (
                    sym, tf_cfg["resolution"], tf_cfg["candles"],
                )

        # ── Fetch all in parallel ──────────────────────────────────────────
        results: dict[str, pd.DataFrame] = {}

        def _fetch(sym: str, tf: str, cnt: int) -> pd.DataFrame:
            return api.fetch_historical_data(
                sym, tf, cnt,
                market_type=market_type,
                holidays=holidays,
                special_sessions=special_sessions,
            )

        with ThreadPoolExecutor(max_workers=15) as pool:
            futures = {
                key: pool.submit(_fetch, sym, tf, cnt)
                for key, (sym, tf, cnt) in fetch_tasks.items()
            }
            for key, fut in futures.items():
                results[key] = fut.result()

        # ── Log data quality issues ────────────────────────────────────────
        for key, df_check in results.items():
            dq = df_check.attrs.get("_data_quality", {})
            if dq.get("was_unsorted") or dq.get("duplicates_removed", 0) > 0:
                unsorted_str = "UNSORTED→sorted" if dq.get("was_unsorted") else "ok"
                dupes = dq.get("duplicates_removed", 0)
                dupes_str = f", {dupes} dupes removed" if dupes else ""
                cnt = dq.get("candles_returned", "?")
                req = dq.get("candles_requested", "?")
                log_strategy_event(
                    symbol_key, "UPDATER", "DATA_QUALITY_FIX",
                    details=f"{key}: {unsorted_str}{dupes_str} | candles={cnt}/{req}",
                )

        # ── Extract 1min DataFrames (for SHA) ──────────────────────────────
        ce_df = results["ce_1m"]
        pe_df = results["pe_1m"]
        idx_df = results["idx_1m"]

        # ── Signal SHA (length=3, 1min) ────────────────────────────────────
        ce_power, ce_list, ce_sha_dbg = get_symbol_details(ce_df)
        pe_power, pe_list, pe_sha_dbg = get_symbol_details(pe_df)
        idx_power, idx_list, idx_sha_dbg = get_symbol_details(idx_df)

        # ── Trend SHA (length=6, 1min) ─────────────────────────────────────
        ce_t_power, ce_t_list, ce_t_sha_dbg = get_trend_details(ce_df)
        pe_t_power, pe_t_list, pe_t_sha_dbg = get_trend_details(pe_df)
        idx_t_power, idx_t_list, idx_t_sha_dbg = get_trend_details(idx_df)

        # ── GAP% between Signal SHA and Trend SHA ──────────────────────────
        ce_gap = compute_sha_gap(ce_sha_dbg, ce_t_sha_dbg)
        pe_gap = compute_sha_gap(pe_sha_dbg, pe_t_sha_dbg)
        idx_gap = compute_sha_gap(idx_sha_dbg, idx_t_sha_dbg)

        # ── SHA Relationship ───────────────────────────────────────────────
        ce_rel = compute_sha_relationship(ce_gap)
        pe_rel = compute_sha_relationship(pe_gap)
        idx_rel = compute_sha_relationship(idx_gap)

        # ── RSI multi-timeframe ────────────────────────────────────────────
        rsi_data: dict[str, dict] = {}

        for tf_label in RSI_TIMEFRAMES:
            ce_rsi_val = _safe_rsi(results[f"ce_{tf_label}"])
            pe_rsi_val = _safe_rsi(results[f"pe_{tf_label}"])
            idx_rsi_val = _safe_rsi(results[f"idx_{tf_label}"])

            rsi_data[tf_label] = {
                "ce": ce_rsi_val,
                "pe": pe_rsi_val,
                "idx": idx_rsi_val,
            }

        # ── Dump to signal_state.json ──────────────────────────────────────
        write_signal_state(
            symbol_key=symbol_key,
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            underlying=underlying,
            # Signal SHA
            ce_power=ce_power,
            ce_list=ce_list,
            pe_power=pe_power,
            pe_list=pe_list,
            idx_power=idx_power,
            idx_list=idx_list,
            ce_sha_debug=ce_sha_dbg,
            pe_sha_debug=pe_sha_dbg,
            idx_sha_debug=idx_sha_dbg,
            # Trend SHA
            ce_trend_power=ce_t_power,
            ce_trend_list=ce_t_list,
            pe_trend_power=pe_t_power,
            pe_trend_list=pe_t_list,
            idx_trend_power=idx_t_power,
            idx_trend_list=idx_t_list,
            ce_trend_sha_debug=ce_t_sha_dbg,
            pe_trend_sha_debug=pe_t_sha_dbg,
            idx_trend_sha_debug=idx_t_sha_dbg,
            # GAP%
            ce_gap=ce_gap,
            pe_gap=pe_gap,
            idx_gap=idx_gap,
            # Relationship
            ce_relationship=ce_rel,
            pe_relationship=pe_rel,
            idx_relationship=idx_rel,
            # RSI multi-TF
            rsi_data=rsi_data,
            market_type=market_type,
        )

        return True

    except Exception as e:
        log_strategy_event(
            symbol_key, "UPDATER", "PROCESS_ERROR",
            details=str(e)[:200],
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "demo").lower()
    logger.info("=" * 60)
    logger.info("  dev_updater_nifty starting — mode=%s", mode)
    logger.info("=" * 60)

    # ── Configure state writer ─────────────────────────────────────────────
    configure_state_writer(mode)

    # ── Initialize Fyers API (read-only) ───────────────────────────────────
    api = FyersAPI()

    # ── Load token (from scanner's shared file) ────────────────────────────
    current_day = date.today()
    session_ok = load_fyers_session(api, force=False)

    if session_ok:
        log_strategy_event(
            "SYSTEM", "INIT", "TOKEN_LOADED",
            details=f"Updater started — token loaded for {current_day}",
        )
    else:
        log_strategy_event(
            "SYSTEM", "INIT", "TOKEN_MISSING",
            details="Waiting for dev_scanner to write token...",
        )

    # ── Load holidays ──────────────────────────────────────────────────────
    holidays: set[str] = set()
    special_sessions: list = []
    try:
        holidays, special_sessions = FyersAPI.load_holiday_set(current_day.year)
        if current_day.month == 12:
            FyersAPI.fetch_trading_holidays(current_day.year + 1)
    except Exception:
        pass

    write_app_status(
        mode, str(current_day), status="started",
        message=f"Updater started — session {'OK' if session_ok else 'WAITING'}",
    )

    logger.info(
        "Entering main loop (interval=%ds, session=%s)",
        UPDATE_INTERVAL,
        "OK" if session_ok else "PENDING",
    )

    # ══════════════════════════════════════════════════════════════════════════
    #  INFINITE LOOP
    # ══════════════════════════════════════════════════════════════════════════
    while True:
        try:
            today = date.today()
            now = datetime.now()
            current_time = now.time()

            # ── Step 1: Day-change → reload token + holidays ───────────────
            if today != current_day:
                logger.info("Day change: %s → %s", current_day, today)
                current_day = today
                session_ok = False

                # Retry a few times since scanner might still be authenticating
                for attempt in range(6):
                    session_ok = load_fyers_session(api, force=True)
                    if session_ok:
                        break
                    sleep(10)

                if session_ok:
                    log_strategy_event(
                        "SYSTEM", "UPDATER", "NEW_DAY_TOKEN_OK",
                        details=f"Day change — token reloaded for {current_day}",
                    )
                else:
                    log_strategy_event(
                        "SYSTEM", "UPDATER", "NEW_DAY_TOKEN_FAIL",
                        details="Token reload failed — will retry each cycle",
                    )

                # Refresh holidays
                try:
                    holidays, special_sessions = FyersAPI.load_holiday_set(today.year)
                    if today.month == 12:
                        FyersAPI.fetch_trading_holidays(today.year + 1)
                except Exception:
                    pass

                write_app_status(
                    mode, str(current_day), status="new_day",
                    message=f"Day change — token {'OK' if session_ok else 'WAITING'}",
                )

            # ── Step 2: Retry session if not ready ─────────────────────────
            if not session_ok:
                session_ok = load_fyers_session(api, force=True)
                if not session_ok:
                    write_app_status(
                        mode, str(current_day), status="waiting_token",
                        message="Auth failed — retrying in 15s...",
                    )
                    sleep(15)
                    continue

            # ── Step 3: Periodic token health check ────────────────────────
            if not api.verify_session():
                session_ok = False
                write_app_status(
                    mode, str(current_day), status="reauth",
                    message="Token expired mid-session — flagged for scanner re-auth",
                )
                sleep(5)
                continue

            # ── Step 4: Holiday check ──────────────────────────────────────
            if not is_trading_day(today, holidays, special_sessions):
                write_app_status(
                    mode, str(current_day), status="holiday",
                    message="Market holiday — updater idle",
                )
                sleep(60)
                continue

            # ── Step 5: Market window check ────────────────────────────────
            in_idx_window = INDICES_START <= current_time <= INDICES_END

            if not in_idx_window:
                write_app_status(
                    mode, str(current_day), status="idle",
                    message=f"Outside market hours ({now.strftime('%H:%M')})",
                )
                sleep(UPDATE_INTERVAL)
                continue

            # ── Step 6: Load INDEX pairs (from scanner) & process ──────────
            symbols_processed = 0
            symbols_failed = 0

            idx_pairs = _load_json(OPTION_PAIRS_JSON)

            if not idx_pairs:
                write_app_status(
                    mode, str(current_day), status="waiting_pairs",
                    in_indices_window=True,
                    message="option_pairs.json not found — waiting for scanner",
                )
                sleep(UPDATE_INTERVAL)
                continue

            for symbol_key, info in idx_pairs.items():
                if symbol_key not in ACTIVE_SYMBOLS:
                    continue

                ce = info.get("CE", "")
                pe = info.get("PE", "")
                underlying = info.get("indices", "")

                if not ce or not pe or not underlying:
                    log_strategy_event(
                        symbol_key, "UPDATER", "INVALID_PAIR",
                        details=f"Missing CE/PE/indices: CE={ce!r} PE={pe!r} IDX={underlying!r}",
                    )
                    continue

                ok = process_symbol(
                    api, symbol_key, ce, pe, underlying,
                    market_type="INDEX",
                    holidays=holidays,
                    special_sessions=special_sessions,
                )

                if ok:
                    symbols_processed += 1
                else:
                    # Auth error? Try reload + retry once
                    if not api.verify_session():
                        session_ok = False
                        log_strategy_event(
                            symbol_key, "AUTH", "REAUTH_DURING_PROCESS",
                            details="Token died during processing — will re-auth next cycle",
                        )
                        break

                    # Token refreshed — retry this symbol once
                    ok = process_symbol(
                        api, symbol_key, ce, pe, underlying,
                        market_type="INDEX",
                        holidays=holidays,
                        special_sessions=special_sessions,
                    )
                    if ok:
                        symbols_processed += 1
                    else:
                        symbols_failed += 1

            # ── Step 7: Update status ──────────────────────────────────────
            write_app_status(
                mode, str(current_day), status="running",
                in_indices_window=in_idx_window,
                indices_scanned=True,
                message=(
                    f"[NIFTY] Updated {symbols_processed} symbol(s) "
                    f"{'| ' + str(symbols_failed) + ' failed ' if symbols_failed else ''}"
                    f"@ {now.strftime('%H:%M:%S')}"
                ),
            )

        except KeyboardInterrupt:
            logger.info("Shutdown requested (Ctrl+C)")
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

        sleep(UPDATE_INTERVAL)


if __name__ == "__main__":
    main()
