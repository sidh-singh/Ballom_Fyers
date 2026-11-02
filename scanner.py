"""
scanner.py — carry-oriented CE/PE strike selection (TRADING_LOGIC pts 7–8).

Improved over the old dev_scanner scorer:
  • Signals are index-driven (O2), so we DROP the per-candidate candle-history
    check (the old scanner fired a 500-candle history call for ~10 candidates ×
    every expiry — the biggest DATA-budget hog). Selection is now purely on
    liquidity + moneyness → far fewer API calls, faster, carry-suited.
  • Expiry preference tuned for carry: nearest expiry with DTE in
    [SCAN_MIN_DTE, SCAN_MAX_DTE] (monthly-ish), else nearest available.
  • Returns the best CE + best PE for a fresh basket.

Every API call goes through FyersAPI.safe_api_call → api_meter (throttled).
For COMMODITY, the worker sets `_resolved_underlying` to the active future symbol.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from constants import (
    SCAN_STRIKE_COUNT, SCAN_MIN_DTE, SCAN_MAX_DTE, SCAN_MIN_OI, SCAN_MAX_PREMIUM_PER_LOT,
)

logger = logging.getLogger("scanner")


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


def _score(o: dict, spot: float, otype: str, lot_size, max_per_lot: float):
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
    if max_per_lot and lot_size and ltp * lot_size > max_per_lot:   # per-lot premium cap
        return None
    score = (_moneyness(strike, spot, otype) * 0.35
             + _spread_eff(bid, ask) * 0.25
             + min(oi / 1_000_000, 1.0) * 0.20
             + min(math.log1p(vol) / 14.0, 1.0) * 0.10
             + (1.0 if ltp < spot * 0.05 else 0.5) * 0.10)
    return score, strike, ltp


def scan_best_pair(symbol_cfg: dict, api, meter=None, lot_df=None) -> dict | None:
    """Return {CE_Symbol, PE_Symbol, CE_Strike, PE_Strike, CE_Premium, PE_Premium,
    Expiry, DTE, VIX} for a fresh basket, or None. Strikes whose premium × lot_size
    exceeds the per-lot cap are skipped (needs lot_df)."""
    underlying = symbol_cfg.get("_resolved_underlying", symbol_cfg["underlying"])
    max_per_lot = symbol_cfg.get("max_premium_per_lot", SCAN_MAX_PREMIUM_PER_LOT)
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
        carry = [(d, ep) for d, ep in expiries if SCAN_MIN_DTE <= (d - now).days <= SCAN_MAX_DTE]
        pick = (carry or expiries)[:1]
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

        best = {}
        for otype in ("CE", "PE"):
            ranked = []
            for o in chain:
                if o.get("option_type") != otype:
                    continue
                s = _score(o, spot, otype, _lot(o.get("symbol", ""), lot_df), max_per_lot)
                if s:
                    ranked.append((s[0], o, s[1], s[2]))
            if not ranked:
                logger.info("scan(%s): no %s candidate", symbol_cfg.get("symbol"), otype)
                return None
            ranked.sort(key=lambda x: x[0], reverse=True)
            best[otype] = ranked[0]

        ce, pe = best["CE"], best["PE"]
        return {
            "CE_Symbol": ce[1]["symbol"], "PE_Symbol": pe[1]["symbol"],
            "CE_Strike": ce[2], "PE_Strike": pe[2],
            "CE_Premium": ce[3], "PE_Premium": pe[3],
            "Expiry": exp_date.strftime("%Y-%m-%d"), "DTE": dte, "VIX": float(vix or 0),
            "Debug": f"spot={spot} dte={dte} ce={ce[0]:.2f} pe={pe[0]:.2f}",
        }
    except Exception as e:
        logger.warning("scan(%s) error: %s", symbol_cfg.get("symbol"), e)
        return None
