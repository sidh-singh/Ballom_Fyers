"""
scanner.py — carry-oriented CE/PE strike selection (TRADING_LOGIC pts 7–8).

Improved over the old dev_scanner scorer:
  • Signals are index-driven (O2), so we DROP the per-candidate candle-history
    check (the old scanner fired a 500-candle history call for ~10 candidates ×
    every expiry — the biggest DATA-budget hog). Selection is now purely on
    liquidity + moneyness → far fewer API calls, faster, carry-suited.
  • Expiry = the MONTH-END (monthly) expiry — the last expiry of each calendar
    month — choosing the closest that is ≥ SCAN_ROLL_DTE (2 weeks) out, and rolling
    to next month's month-end if the current one is nearer than that.
  • Returns the best CE + best PE for a fresh basket.

Every API call goes through FyersAPI.safe_api_call → api_meter (throttled).
For COMMODITY, the worker sets `_resolved_underlying` to the active future symbol.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime

from constants import (
    SCAN_STRIKE_COUNT, SCAN_MIN_DTE, SCAN_MAX_DTE, SCAN_ROLL_DTE, SCAN_MIN_OI,
    SCAN_MAX_PREMIUM_PER_LOT, SCAN_MIN_PREMIUM_PER_LOT, SCAN_ROUND_STRIKE_STEP,
)

logger = logging.getLogger("scanner")

# ── strike-selection mode ────────────────────────────────────────────────────
# "deterministic" (DEFAULT): pick a STABLE strike from spot + the round-strike grid, so two bots
# scanning the same market minute on SEPARATE machines converge on the SAME pair — netting and
# hedging then trade an identical basket and differ ONLY in their exit (a clean A/B). "liquidity":
# the old OI/volume/spread-scored pick — best single-bot liquidity, but it drifts second-to-second,
# so independent branches land on DIFFERENT strikes (which is why netting traded and hedging didn't).
# Override per deployment with the SCAN_MODE / SCAN_OTM_STEPS env vars.
SCAN_MODE = os.environ.get("SCAN_MODE", "deterministic").strip().lower()
try:
    SCAN_OTM_STEPS = int(os.environ.get("SCAN_OTM_STEPS", "0"))   # OTM offset in round_step units
except ValueError:
    SCAN_OTM_STEPS = 0


def _ok(r) -> bool:
    return isinstance(r, dict) and r.get("s") == "ok"


def _lot(symbol: str, lot_df):
    """Lot size for a leg symbol from the cached CSV; None if unknown (cap then skipped)."""
    if lot_df is None or not symbol:
        return None
    try:
        from fyers_api import FyersAPI
        return FyersAPI.get_lot_size(symbol, lot_df)
    except Exception:
        return None


def _moneyness(strike: float, spot: float, otype: str) -> float:
    """Slight-OTM preference for buying options (carry)."""
    m = (strike - spot) / spot if otype == "CE" else (spot - strike) / spot
    if 0.005 <= m <= 0.02:   return 1.0
    if 0.0 <= m <= 0.04:     return 0.85
    if -0.01 <= m < 0:       return 0.75
    if -0.03 <= m < -0.01:   return 0.55
    if 0.04 < m <= 0.07:     return 0.4
    return 0.2


def _spread_eff(bid: float, ask: float) -> float:
    if ask <= 0 or bid <= 0 or ask < bid:
        return 0.0
    mid = (ask + bid) / 2
    return max(0.0, 1 - (ask - bid) / mid) if mid > 0 else 0.0


def _roundness(strike: float, step: float) -> float:
    """1.0 at a round multiple of `step`, decaying to 0 at the half-step. Rewards the liquid,
    long-history strikes (e.g. NIFTY 24000/25000/26000 for step=1000). No step → neutral 0.5."""
    if not step or step <= 0:
        return 0.5
    d = abs(strike - round(strike / step) * step)   # distance to the nearest round multiple
    return max(0.0, 1.0 - d / (step / 2.0))


def _default_round_step(underlying: str) -> int:
    """Round-strike grid to prefer when symbols.json gives no `round_strike_step`:
    NIFTY/BANKNIFTY → 500 (famous 24000/24500/25000/25500/26000); GOLD/SILVER → 1000.
    Falls back to SCAN_ROUND_STRIKE_STEP for anything else."""
    s = (underlying or "").upper()
    if "SILVER" in s or "GOLD" in s:
        return 1000
    if "NIFTY" in s:
        return 500
    return SCAN_ROUND_STRIKE_STEP


def _score(o: dict, spot: float, otype: str, lot_size, max_per_lot: float,
           min_per_lot: float, round_step: float):
    ltp = float(o.get("ltp", 0) or 0)
    oi = float(o.get("oi", 0) or 0)
    vol = float(o.get("volume", 0) or 0)
    bid = float(o.get("bid", 0) or 0)
    ask = float(o.get("ask", 0) or 0)
    strike = float(o.get("strike_price", 0) or 0)
    if ltp <= 0 or strike <= 0:
        return None
    if oi < SCAN_MIN_OI and vol <= 0:          # illiquid
        return None
    # Per-lot premium BAND: skip strikes that cost more than the cap or less than the floor,
    # so we land near-ATM options with real value / history (not cheap far-OTM). Needs lot_size.
    if lot_size:
        per_lot = ltp * lot_size
        if max_per_lot and per_lot > max_per_lot:
            return None
        if min_per_lot and per_lot < min_per_lot:
            return None
    score = (_moneyness(strike, spot, otype) * 0.30     # closeness to ATM (slight-OTM preferred)
             + _roundness(strike, round_step) * 0.20    # prefer round (liquid, long-history) strikes
             + _spread_eff(bid, ask) * 0.20             # tight spread
             + min(oi / 1_000_000, 1.0) * 0.20          # open interest
             + min(math.log1p(vol) / 14.0, 1.0) * 0.10) # volume
    return score, strike, ltp


def _pick_deterministic(chain, spot: float, round_step: float):
    """Deterministic CE/PE strike pick (SCAN_MODE='deterministic'). Snap spot to the round-strike grid
    (ATM) and take the chain option AT that strike (± SCAN_OTM_STEPS grid steps) per side. A PURE
    function of (spot, grid, chain) — NO OI/volume/spread/premium inputs — so two bots scanning the
    same minute, even on different machines, pick the SAME strikes. A target exactly between two listed
    strikes breaks to the lower strike, so both bots still agree. Returns
    {'CE': (strike, ltp, symbol), 'PE': (...)} or None if either side has no usable strike."""
    if round_step and round_step > 0:
        atm = round(spot / round_step) * round_step
        targets = {"CE": atm + SCAN_OTM_STEPS * round_step,
                   "PE": atm - SCAN_OTM_STEPS * round_step}
    else:
        targets = {"CE": spot, "PE": spot}          # no grid → nearest listed strike to spot
    out = {}
    for otype, target in targets.items():
        best = None                                  # (distance, strike, ltp, symbol)
        for o in chain:
            if o.get("option_type") != otype:
                continue
            strike = float(o.get("strike_price", 0) or 0)
            ltp = float(o.get("ltp", 0) or 0)
            sym = o.get("symbol", "")
            oi = float(o.get("oi", 0) or 0)
            vol = float(o.get("volume", 0) or 0)
            if strike <= 0 or ltp <= 0 or not sym or (oi <= 0 and vol <= 0):
                continue    # skip illiquid strikes -> pick the nearest TRADEABLE strike to target
            d = abs(strike - target)
            if best is None or d < best[0] or (d == best[0] and strike < best[1]):
                best = (d, strike, ltp, sym)
        if best is None:
            return None
        out[otype] = (best[1], best[2], best[3])     # strike, ltp, symbol
    return out


def scan_best_pair(symbol_cfg: dict, api, meter=None, lot_df=None) -> dict | None:
    """Return {CE_Symbol, PE_Symbol, CE_Strike, PE_Strike, CE_Premium, PE_Premium,
    Expiry, DTE, VIX} for a fresh basket, or None. Strikes whose premium × lot_size
    exceeds the per-lot cap are skipped (needs lot_df)."""
    underlying = symbol_cfg.get("_resolved_underlying", symbol_cfg["underlying"])
    max_per_lot = symbol_cfg.get("max_premium_per_lot", SCAN_MAX_PREMIUM_PER_LOT)
    min_per_lot = symbol_cfg.get("min_premium_per_lot", SCAN_MIN_PREMIUM_PER_LOT)
    _cfg_step = symbol_cfg.get("round_strike_step")   # symbols.json override wins (incl. 0 = off)
    round_step = _cfg_step if _cfg_step is not None else _default_round_step(underlying)
    model = api.model
    try:
        q = api.safe_api_call(model.quotes, data={"symbols": underlying})
        if not _ok(q):
            return None
        spot = q["d"][0]["v"].get("lp")
        if not spot:
            return None

        base = api.safe_api_call(model.optionchain,
                                 data={"symbol": underlying, "strikecount": SCAN_STRIKE_COUNT})
        if not _ok(base):
            return None
        data = base.get("data", {})
        vix = data.get("indiavixData", {}).get("ltp", 0)

        expiries = []
        for e in data.get("expiryData", []):
            try:
                expiries.append((datetime.strptime(e["date"], "%d-%m-%Y"), e["expiry"]))
            except Exception:
                continue
        expiries.sort(key=lambda x: x[0])
        now = datetime.now()
        # MONTH-END expiry: the last expiry of each calendar month (NSE monthly = last weekly of the
        # month). Pick the closest month-end that is ≥ SCAN_ROLL_DTE (2 weeks) away; if the nearest
        # month-end is inside 2 weeks, roll to next month's — carry-friendly, data-rich pairs.
        monthly = {}
        for d, ep in expiries:          # ascending → last write per (year, month) = that month's end
            monthly[(d.year, d.month)] = (d, ep)
        monthlies = sorted(monthly.values(), key=lambda x: x[0])
        _future = [(d, ep) for d, ep in monthlies if (d - now).days >= SCAN_ROLL_DTE]
        pick = (_future or monthlies or expiries)[:1]
        if not pick:
            logger.info("scan(%s): no expiries", symbol_cfg.get("symbol"))
            return None
        exp_date, exp_epoch = pick[0]
        dte = max((exp_date - now).days, 0)

        oc = api.safe_api_call(model.optionchain, data={
            "symbol": underlying, "strikecount": SCAN_STRIKE_COUNT, "timestamp": str(exp_epoch)})
        if not _ok(oc):
            return None
        chain = oc.get("data", {}).get("optionsChain", [])

        if SCAN_MODE == "liquidity":
            # OLD behaviour: OI/volume/spread-scored best strike per side (drifts → branches diverge).
            best = {}
            for otype in ("CE", "PE"):
                ranked = []
                for o in chain:
                    if o.get("option_type") != otype:
                        continue
                    s = _score(o, spot, otype, _lot(o.get("symbol", ""), lot_df),
                               max_per_lot, min_per_lot, round_step)
                    if s:
                        ranked.append((s[0], o, s[1], s[2]))
                if not ranked:
                    logger.info("scan(%s): no %s candidate", symbol_cfg.get("symbol"), otype)
                    return None
                ranked.sort(key=lambda x: x[0], reverse=True)
                best[otype] = ranked[0]
            ce, pe = best["CE"], best["PE"]
            ce_sym, ce_strike, ce_prem = ce[1]["symbol"], ce[2], ce[3]
            pe_sym, pe_strike, pe_prem = pe[1]["symbol"], pe[2], pe[3]
            mode_dbg = f"liquidity ce={ce[0]:.2f} pe={pe[0]:.2f}"
        else:
            # DEFAULT: deterministic strike from spot + the round grid → identical across branches.
            picked = _pick_deterministic(chain, spot, round_step)
            if not picked:
                logger.info("scan(%s): no deterministic strike", symbol_cfg.get("symbol"))
                return None
            ce_strike, ce_prem, ce_sym = picked["CE"]
            pe_strike, pe_prem, pe_sym = picked["PE"]
            mode_dbg = f"deterministic otm_steps={SCAN_OTM_STEPS}"

        return {
            "CE_Symbol": ce_sym, "PE_Symbol": pe_sym,
            "CE_Strike": ce_strike, "PE_Strike": pe_strike,
            "CE_Premium": ce_prem, "PE_Premium": pe_prem,
            "Expiry": exp_date.strftime("%Y-%m-%d"), "DTE": dte, "VIX": float(vix or 0),
            "Debug": f"spot={spot} dte={dte} mode={SCAN_MODE} {mode_dbg}",
        }
    except Exception as e:
        logger.warning("scan(%s) error: %s", symbol_cfg.get("symbol"), e)
        return None
