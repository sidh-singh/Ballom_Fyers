"""
auto_tune.py — adaptive daily hill-climb of the HEDGING ₹ profit target (``hedge_per_unit``).

Once per trading day, ~5 min before the symbol's session close, the worker calls ``maybe_tune()``:
it reads today's closed trades for that symbol from ``profit_history.json``, computes the day's
**win-ratio** and **total net ₹**, compares them to the previous recorded day, and nudges the
per-unit target like a one-parameter hill-climb (owner: "train the model to get the best amount"):

    both metrics UP    → amount + STEP
    both metrics DOWN  → amount − STEP
    mixed / < MIN_TRADES / first day → hold

clamped to ``[FLOOR, CAP]``. The new amount is **auto-applied** — the worker reads
``current_amount()`` on start and after each tune. State lives in ``<STATE_DIR>/auto_tune.json``::

    { "<symbol>": { "amount": <float>, "last_date": "YYYY-MM-DD",
                    "history": [ {date, amount, win, total, trades, decision}, … ] } }

HEDGING branches only — netting's ``hedge_per_unit`` is a book *floor* (a different role) and is
left untuned. The dashboard reads the ``history`` to draw the daily-target chart + best-fit line.
"""
import json
import os
import tempfile
from pathlib import Path

from constants import STATE_DIR

STEP = 100.0          # ₹ step per day
FLOOR = 100.0         # never below the start value
CAP = 1000.0          # ceiling
MIN_TRADES = 3        # need at least this many closed trades that day to move the amount
MAX_DAYS = 60         # history kept per symbol (for the dashboard chart)

_STATE = STATE_DIR / "auto_tune.json"
_HIST = STATE_DIR / "profit_history.json"


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _write_atomic(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clamp(a):
    return float(min(CAP, max(FLOOR, a)))


def day_metrics(symbol, date_iso, hist_path=None):
    """(win_ratio, total_net_pl, n_closes) for one symbol on one date, from profit_history.json."""
    rows = [r for r in (_read(hist_path or _HIST, []) or [])
            if r.get("action") == "CLOSE" and r.get("date") == date_iso and r.get("symbol") == symbol]
    n = len(rows)
    if n == 0:
        return 0.0, 0.0, 0
    wins = sum(1 for r in rows if float(r.get("net_pl", 0)) > 0)
    total = round(sum(float(r.get("net_pl", 0)) for r in rows), 2)
    return round(wins / n, 4), total, n


def current_amount(symbol, base, state_path=None):
    """The amount to use right now for ``symbol`` — the tuned value if present, else the base."""
    rec = (_read(state_path or _STATE, {}) or {}).get(symbol)
    if isinstance(rec, dict) and isinstance(rec.get("amount"), (int, float)):
        return _clamp(rec["amount"])
    return float(base)


def maybe_tune(symbol, base, date_iso, state_path=None, hist_path=None):
    """Run ONCE per day (near close). Records today's metrics, steps the amount for TOMORROW by
    comparing today vs the previous recorded day, persists, and returns the amount to use now.
    Idempotent within a day (guarded by ``last_date``)."""
    sp = state_path or _STATE
    state = _read(sp, {}) or {}
    rec = state.get(symbol) or {"amount": float(base), "last_date": None, "history": []}
    if rec.get("last_date") == date_iso:                      # already tuned today
        return _clamp(rec.get("amount", base))

    win, total, n = day_metrics(symbol, date_iso, hist_path)
    amount_used = float(rec.get("amount", base))              # the amount that produced today's metrics
    amount = amount_used
    hist = rec.get("history", [])

    if n == 0:                                                # no trades today → no signal; mark done
        rec["last_date"] = date_iso
        state[symbol] = rec
        _write_atomic(sp, state)
        return _clamp(amount)

    prev = hist[-1] if hist else None
    decision = "hold"
    if n >= MIN_TRADES and prev is not None:
        pw, pt = prev.get("win", 0), prev.get("total", 0)
        # Pareto step: BOTH metrics non-worse AND at least one strictly better → up (symmetric for
        # down). Using ≥/≤ (not strict on both) so a saturated win-ratio — common here, trades often
        # all win — still lets a rising net ₹ push the target up instead of stalling.
        up = (win >= pw and total >= pt) and (win > pw or total > pt)
        down = (win <= pw and total <= pt) and (win < pw or total < pt)
        if up:
            amount = min(CAP, amount + STEP); decision = "up"
        elif down:
            amount = max(FLOOR, amount - STEP); decision = "down"

    hist.append({"date": date_iso, "amount": round(amount_used, 2),
                 "win": win, "total": total, "trades": n, "decision": decision})
    rec.update(amount=_clamp(amount), last_date=date_iso, history=hist[-MAX_DAYS:])
    state[symbol] = rec
    _write_atomic(sp, state)
    return rec["amount"]
