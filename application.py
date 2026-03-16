"""
application.py — Trading engine for the dev_trading branch.

Usage:  python application.py [demo|live]

Architecture
────────────
This branch focuses ONLY on trading logic:
  - Reads Fyers token from C:/Ballom_FYR/fyers_token.json (written by dev_scanner)
  - Reads option pairs from C:/Ballom_FYR/option_pairs.json (written by dev_scanner)
  - Reads signal data from C:/Ballom_FYR/state/<mode>/signal_state.json (written by dev_updater_nifty)
  - Passes signal data to strategy.evaluate() — NO SHA/RSI computation
  - Places trades, monitors positions, dumps state for dashboard

Dependency chain:
    dev_scanner → dev_updater_nifty → **dev_trading (this)** → dev_front_end (dashboard)

Outer Loop (runs forever):
  Step 1 — Day-change detection  → reload token + fetch holidays
  Step 2 — Time-window check     → indices 9:15 – 15:30
  Step 3 — Read signal_state.json from dev_updater_nifty
  Step 4 — Inner loop            → strategy.evaluate() → execute_orders → monitor

Inner Loop (per symbol pair):
  Step A — Read signal_state.json (CE, PE, IDX SHA + RSI)
  Step B — strategy.evaluate(signal_data) → (ce_action, pe_action)
  Step C — strategy.execute_orders() → place trades
  Step D — Monitor positions; if all closed → clear pair lock → break
  Step E — Day-change inside inner loop → reload token
"""

import sys
import json
from dataclasses import asdict
from datetime import date, datetime, time as dt_time
from pathlib import Path
from time import sleep

import pandas as pd

from fyers import Fyers
from demo_fyers import DemoFyers
from strategy import HeikenAshiMartingale
from position_tracker import PositionTracker
from pair_manager import PairManager
from constants import (
    Transaction,
    SYMBOLS_JSON,
    OPTION_PAIRS_JSON,
    INDICES_START,
    INDICES_END,
    INNER_LOOP_INTERVAL,
    STRATEGY_HEDGE_INDEX,
    ACTIVE_SYMBOLS,
    get_signal_state_file,
)
from state_writer import (
    configure as configure_state_writer,
    write_app_status,
    write_position_state,
    write_account_state,
    log_strategy_event,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  JSON I/O HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def load_symbols_config() -> dict:
    """Load symbols.json (indices config, max_balance_usage, brake)."""
    with open(SYMBOLS_JSON, "r") as f:
        return json.load(f)


def load_signal_state(mode: str) -> dict:
    """
    Read signal_state.json written by dev_updater_nifty.
    Returns the full dict keyed by symbol_key (e.g. {"NIFTY": {...}}).
    """
    path = get_signal_state_file(mode)
    return _load_json(path)


# ═══════════════════════════════════════════════════════════════════════════════
#  TOKEN LOADING
# ═══════════════════════════════════════════════════════════════════════════════

def load_fyers_session(fyers_obj: Fyers, max_attempts: int = 10) -> bool:
    """
    Load the Fyers token from C: drive (written by dev_scanner).
    No TOTP re-auth — if token is missing or invalid, flag expired and return False.
    Retries every 30s up to *max_attempts* on first startup.
    """
    for attempt in range(max_attempts):
        ok = fyers_obj.load_token()
        if ok:
            log_strategy_event("SYSTEM", "AUTH", "TOKEN_LOADED",
                               details=f"Token loaded from C: drive (attempt {attempt + 1})")
            return True

        log_strategy_event("SYSTEM", "AUTH", "TOKEN_RETRY",
                           details=f"Attempt {attempt + 1}/{max_attempts} failed — waiting 30s")
        if attempt < max_attempts - 1:
            sleep(30)

    # All retries exhausted — flag token expired so dev_scanner re-auths
    fyers_obj.invalidate()
    log_strategy_event("SYSTEM", "AUTH", "TOKEN_EXPIRED",
                       details="All attempts failed — flagged expired for dev_scanner")
    return False


# ═══════════════════════════════════════════════════════════════════════════════
#  HOLIDAY AWARENESS
# ═══════════════════════════════════════════════════════════════════════════════

def is_trading_day(holidays: set, special_sessions: list) -> bool:
    """Return True if today is a valid trading day."""
    today = date.today()
    day_str = today.strftime("%Y-%m-%d")

    for ss in special_sessions:
        if ss.get("date") == day_str:
            return True

    if today.weekday() >= 5:
        return False

    return day_str not in holidays


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE-DUMP HELPERS  (writes data for dashboard to consume)
# ═══════════════════════════════════════════════════════════════════════════════

def _dump_positions_and_account(fyers: Fyers) -> None:
    """Snapshot current positions + account state to JSON."""
    pos_df, overall = fyers.position()

    if not pos_df.empty:
        float_cols = pos_df.select_dtypes(include=["float", "float64"]).columns
        pos_df[float_cols] = pos_df[float_cols].round(2)

    rows = pos_df.to_dict(orient="records") if not pos_df.empty else []
    overall_dict = asdict(overall)
    for k, v in overall_dict.items():
        if isinstance(v, float):
            overall_dict[k] = round(v, 2)
    write_position_state(rows, overall_dict)

    funds = fyers.funds()
    fund_map = {}
    for item in funds.get("fund_limit", []):
        fund_map[item.get("title", "")] = item.get("equityAmount", 0)

    write_account_state(
        balance=round(fund_map.get("Total Balance", 0), 2),
        utilized=round(fund_map.get("Utilized Amount", 0), 2),
        realized_pnl=round(fund_map.get("Realized P&L", overall.pl_realized), 2),
        unrealized_pnl=round(overall.pl_unrealized, 2),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _has_open_positions(fyers: Fyers, ce_symbol: str, pe_symbol: str) -> bool:
    """Return True if either CE or PE has a non-zero qty in positions."""
    pos_df, _ = fyers.position()
    if pos_df.empty:
        return False
    for sym in (ce_symbol, pe_symbol):
        row = pos_df[
            (pos_df["symbol"] == sym)
            & (pos_df["productType"] == "MARGIN")
        ]
        if not row.empty and int(row["netQty"].iloc[0]) != 0:
            return True
    return False


def _is_in_trading_window() -> bool:
    """Check if we are currently inside the indices trading window."""
    now = datetime.now().time()
    return INDICES_START <= now <= INDICES_END


# ═══════════════════════════════════════════════════════════════════════════════
#  INNER LOOP — the blocking trading loop
# ═══════════════════════════════════════════════════════════════════════════════

def inner_loop(
    fyers: Fyers,
    strategy: HeikenAshiMartingale,
    holidays: set,
    special_sessions: list,
    inner_day_ref: date,
    mode: str = "demo",
    tracker: PositionTracker | None = None,
    pair_manager: PairManager | None = None,
) -> date:
    """
    Blocking inner loop — for every symbol in option_pairs.json:

      1. Read signal_state.json (SHA + RSI from dev_updater_nifty)
      2. strategy.evaluate(signal_data) → (ce_action, pe_action)
      3. strategy.execute_orders() → place trades
      4. Monitor positions — if all closed → clear pair lock → break
      5. If day changes → reload token inside the loop

    Returns the (possibly updated) current_day so the outer loop stays in sync.
    """
    pairs = _load_json(OPTION_PAIRS_JSON)
    if not pairs:
        write_app_status(mode, str(inner_day_ref), status="idle",
                         message="No pairs in option_pairs.json — waiting for dev_scanner")
        log_strategy_event("SYSTEM", "INNER", "NO_PAIRS",
                           details=f"Empty file: {OPTION_PAIRS_JSON.name}")
        sleep(30)
        return inner_day_ref

    current_day = inner_day_ref

    # Filter to valid pairs matching ACTIVE_SYMBOLS
    valid_pairs = {}
    for symbol_key, info in pairs.items():
        if symbol_key not in ACTIVE_SYMBOLS:
            continue

        ce_symbol  = info.get("CE", "")
        pe_symbol  = info.get("PE", "")
        underlying = info.get("indices", "")
        base_qty   = info.get("qty", 0)

        if not ce_symbol or not pe_symbol or not underlying:
            log_strategy_event(symbol_key, "INNER", "SKIP_INCOMPLETE",
                               details=f"CE={ce_symbol!r} PE={pe_symbol!r} UND={underlying!r}")
            continue

        if base_qty <= 0:
            log_strategy_event(symbol_key, "INNER", "SKIP_QTY",
                               details=f"Invalid qty={base_qty}")
            continue

        valid_pairs[symbol_key] = info

    if not valid_pairs:
        log_strategy_event("SYSTEM", "INNER", "NO_VALID_PAIRS",
                           details=f"{len(pairs)} pairs in file, 0 valid")
        return inner_day_ref

    log_strategy_event("SYSTEM", "INNER", "TRADING",
                       details=f"{len(valid_pairs)} valid pairs: {', '.join(valid_pairs.keys())}")

    for symbol_key, info in valid_pairs.items():
        ce_symbol  = info["CE"]
        pe_symbol  = info["PE"]
        underlying = info.get("indices", "")
        base_qty   = info["qty"]
        pair_hedge = info.get("hedge", STRATEGY_HEDGE_INDEX)

        write_app_status(mode, str(current_day), status="trading",
                         message=f"Trading {symbol_key} | CE={ce_symbol} PE={pe_symbol}")

        snapshot_counter = 0
        had_positions_ever = _has_open_positions(fyers, ce_symbol, pe_symbol)

        # ── trading loop for this symbol pair ──────────────────────────────
        while True:
            # ── Day-change detection — reload token ────────────────────────
            today = date.today()
            if today != current_day:
                token_ok = load_fyers_session(fyers, max_attempts=3)
                if not token_ok:
                    log_strategy_event("SYSTEM", "INNER", "TOKEN_RELOAD_FAIL",
                                       details="Day changed but could not reload token")
                holidays_new, ss_new = Fyers.load_holiday_set(today.year)
                holidays = holidays_new
                special_sessions = ss_new
                current_day = today
                write_app_status(mode, str(current_day), status="re-auth",
                                 message=f"Day changed → token reloaded for {current_day}")

            # ── Check trading window ──────────────────────────────────────
            if not _is_in_trading_window():
                if not _has_open_positions(fyers, ce_symbol, pe_symbol):
                    if had_positions_ever and pair_manager:
                        pair_manager.clear_pair(symbol_key)
                        log_strategy_event(symbol_key, "PAIR_MGR", "PAIR_CLEARED_WINDOW_END",
                                           details=f"CE={ce_symbol} PE={pe_symbol} — outside window, no positions")
                    write_app_status(mode, str(current_day), status="idle",
                                     message=f"{symbol_key}: outside window & no positions")
                    break
                # Has open positions outside window — keep monitoring
                sleep(INNER_LOOP_INTERVAL)
                continue

            try:
                # ── Step A: Read signal_state.json (from dev_updater_nifty)
                all_signals = load_signal_state(mode)
                signal_data = all_signals.get(symbol_key, {})

                if not signal_data:
                    log_strategy_event(symbol_key, "INNER", "NO_SIGNAL_DATA",
                                       details="signal_state.json has no data for this symbol — waiting")
                    sleep(INNER_LOOP_INTERVAL * 5)
                    continue

                # ── Step B: Strategy evaluation ───────────────────────────
                pos_df, _ = fyers.position()

                ce_action, pe_action = strategy.evaluate(
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    base_qty=base_qty,
                    signal_data=signal_data,
                    position_df=pos_df,
                    hedge=pair_hedge,
                )

                # ── Step C: Execute orders ────────────────────────────────
                strategy.execute_orders(fyers, ce_action, pe_action)

                # ── Dump positions + account after execution ──────────────
                _dump_positions_and_account(fyers)

                # ── Snapshot profit history for dashboard (~15s) ──────────
                snapshot_counter += 1
                if snapshot_counter % 15 == 0 and tracker:
                    for _act in (ce_action, pe_action):
                        if _act.position_qty != 0:
                            tracker.log_snapshot(
                                _act.symbol, _act.pl,
                                _act.api_total_pl, abs(_act.position_qty),
                                ltp=_act.ltp, avg_price=_act.avg_price)

                # ── Step D: Position lifecycle tracking ────────────────────
                has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)

                if has_pos_now:
                    had_positions_ever = True

                # After an actionable order, re-check positions (fill delay)
                if ce_action.is_actionable or pe_action.is_actionable:
                    sleep(2)
                    has_pos_now = _has_open_positions(fyers, ce_symbol, pe_symbol)
                    if has_pos_now:
                        had_positions_ever = True

                # Confirm pending closes that have now filled
                if not has_pos_now:
                    pending_ce = strategy.is_pending_close(ce_symbol)
                    pending_pe = strategy.is_pending_close(pe_symbol)
                    if pending_ce or pending_pe:
                        pos_df_fresh, _ = fyers.position()
                        for _sym in (ce_symbol, pe_symbol):
                            if strategy.is_pending_close(_sym):
                                _, _, _, fresh_pl, _, _ = HeikenAshiMartingale._read_position(
                                    pos_df_fresh, _sym, "MARGIN")
                                strategy.confirm_close(_sym, current_api_total_pl=fresh_pl)

                # All positions closed after having been open → cycle complete
                if not has_pos_now and had_positions_ever:
                    if pair_manager:
                        pair_manager.clear_pair(symbol_key)
                        log_strategy_event(symbol_key, "PAIR_MGR", "PAIR_CLEARED_AFTER_CLOSE",
                                           details=f"CE={ce_symbol} PE={pe_symbol} — lock released")
                    log_strategy_event(symbol_key, "INNER", "ALL_CLOSED",
                                       details="Position cycle complete — returning to outer loop for fresh pair")
                    break

            except Exception as e:
                log_strategy_event(symbol_key, "INNER", "ERROR", details=str(e))

            sleep(INNER_LOOP_INTERVAL)

    return current_day


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN  —  single infinite loop
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "demo").lower()

    # Configure state writer to use mode-specific directory
    configure_state_writer(mode)

    config = load_symbols_config()
    brake = config.get("brake", 0)

    # ── Instantiate Fyers (Demo or Live) ──────────────────────────────────
    fyers = DemoFyers() if mode == "demo" else Fyers()

    # ── Load token from C: drive (written by dev_scanner) ─────────────────
    # First prefer auth from token from C: drive; if auth fails, flag expired
    # so that dev_scanner branch can re-auth.
    token_ok = load_fyers_session(fyers)
    if not token_ok:
        log_strategy_event("SYSTEM", "INIT", "TOKEN_LOAD_FATAL",
                           details="Could not load Fyers token after 10 attempts — exiting")
        write_app_status(mode, str(date.today()), status="fatal",
                         message="Token load failed — ensure dev_scanner has written fyers_token.json")
        sys.exit(1)

    # ── Position Tracker (per-symbol booked profit, profit history) ───────
    tracker = PositionTracker(mode=mode)

    # One-time cleanup: reset corrupted booked_profit values
    if not tracker._data.get("_pl_fix_applied"):
        tracker.reset_booked_profits()
        tracker._data["_pl_fix_applied"] = True
        tracker._save_tracker()
        log_strategy_event("SYSTEM", "INIT", "PL_FIX_RESET",
                           details="Booked profits reset — pl-formula fix deployed")

    # ── Pair Manager (lock CE/PE pairs per symbol) ────────────────────────
    pair_manager = PairManager(mode=mode)

    # ── Strategy ──────────────────────────────────────────────────────────
    strategy = HeikenAshiMartingale(
        mode=mode,
        brake=bool(brake),
        max_balance_usage=config.get("max_balance_usage", 0),
        tracker=tracker,
    )

    # ── Dump initial account state so dashboard shows balance immediately ─
    try:
        _dump_positions_and_account(fyers)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "ACCOUNT_DUMP_FAIL", details=str(e))

    # ── Holidays ──────────────────────────────────────────────────────────
    current_day = date.today()
    holidays: set = set()
    special_sessions: list = []

    try:
        holidays, special_sessions = Fyers.load_holiday_set()
        log_strategy_event("SYSTEM", "INIT", "HOLIDAYS_LOADED",
                           details=f"Loaded {len(holidays)} holidays for {current_day.year}")
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_LOAD_FAIL", details=str(e))

    try:
        Fyers.fetch_trading_holidays(current_day.year)
        if current_day.month == 12:
            Fyers.fetch_trading_holidays(current_day.year + 1)
    except Exception as e:
        log_strategy_event("SYSTEM", "INIT", "HOLIDAY_FETCH_FAIL", details=str(e))

    write_app_status(mode, str(current_day), status="started",
                     message=f"Trading engine started | mode={mode} | brake={'ON' if brake else 'OFF'}")

    # ══════════════════════════════════════════════════════════════════════
    #  OUTER LOOP  (single infinite loop — as requested)
    # ══════════════════════════════════════════════════════════════════════
    while True:
        today = date.today()
        now   = datetime.now().time()

        # ── Step 1: Day-change → reload token, reset tracker ─────────────
        if today != current_day:
            current_day = today
            tracker.reset_for_new_day()

            # Reload token (dev_scanner writes fresh token daily).
            # If auth fails, flag expired so dev_scanner re-auths.
            token_ok = load_fyers_session(fyers, max_attempts=5)
            if not token_ok:
                log_strategy_event("SYSTEM", "OUTER", "TOKEN_RELOAD_FAIL",
                                   details="Day changed — token reload failed, flagged expired for dev_scanner")
                write_app_status(mode, str(current_day), status="token_failed",
                                 message="Token reload failed — ensure dev_scanner is running")
                sleep(60)
                continue

            # Refresh holidays
            try:
                holidays, special_sessions = Fyers.load_holiday_set(today.year)
                if today.month == 12:
                    Fyers.fetch_trading_holidays(today.year + 1)
            except Exception as e:
                log_strategy_event("SYSTEM", "OUTER", "HOLIDAY_REFRESH_FAIL", details=str(e))

            write_app_status(mode, str(current_day), status="new_day",
                             message=f"New day — token reloaded for {current_day}")

        # ── Holiday / weekend check ──────────────────────────────────────
        if not is_trading_day(holidays, special_sessions):
            write_app_status(mode, str(current_day), status="holiday",
                             message=f"Not a trading day ({current_day})")
            sleep(300)
            continue

        # ── Step 2: Check if inside indices trading window ────────────────
        in_window = INDICES_START <= now <= INDICES_END

        # ── Always dump account state so dashboard has fresh data ────────
        write_app_status(
            mode, str(current_day),
            in_indices_window=in_window,
            status="running",
        )

        try:
            _dump_positions_and_account(fyers)
        except Exception:
            pass

        # ── Step 3: If inside indices window → enter inner loop ──────────
        if in_window:
            log_strategy_event("SYSTEM", "OUTER", "INDEX_TRADING",
                               details="Entering inner loop for INDEX pairs")
            try:
                current_day = inner_loop(
                    fyers, strategy,
                    holidays=holidays,
                    special_sessions=special_sessions,
                    inner_day_ref=current_day,
                    mode=mode,
                    tracker=tracker,
                    pair_manager=pair_manager,
                )
            except Exception as e:
                log_strategy_event("SYSTEM", "INNER", "INDEX_LOOP_FAIL",
                                   details=str(e))
        else:
            write_app_status(mode, str(current_day), status="idle",
                             message=f"Outside trading hours ({now.strftime('%H:%M')})")

        sleep(1)


if __name__ == "__main__":
    main()
