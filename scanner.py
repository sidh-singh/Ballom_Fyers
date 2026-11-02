"""
scanner.py — pick the CE/PE strikes for a fresh basket.

For now this reuses the (verified) option-pair scorer carried over from
dev_scanner (`FyersAPI.fetch_option_pair`), which already scores CE/PE on 7
weighted criteria. The "improved" scan (TRADING_LOGIC pts 7–8) can replace the
body later without changing the worker contract. Every API call inside
`fetch_option_pair` is routed through `api_meter`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("scanner")


def scan_best_pair(symbol_cfg: dict, api, meter=None) -> dict | None:
    """Return a dict with at least CE_Symbol, PE_Symbol, CE_Strike, PE_Strike,
    Expiry (or None if no suitable pair)."""
    underlying = symbol_cfg["underlying"]
    asset_type = symbol_cfg.get("asset_type", "INDEX")
    try:
        pair = api.fetch_option_pair(underlying, asset_type=asset_type)
    except Exception as e:
        logger.warning("scan_best_pair(%s) error: %s", symbol_cfg.get("symbol"), e)
        return None
    if not isinstance(pair, dict) or not pair.get("CE_Symbol") or not pair.get("PE_Symbol"):
        logger.info("scan_best_pair(%s): no valid pair (%s)",
                    symbol_cfg.get("symbol"), (pair or {}).get("Message"))
        return None
    return pair
