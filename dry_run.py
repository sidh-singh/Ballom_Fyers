"""
dry_run.py — ONE-SHOT shadow validation of fyers_live against LIVE Fyers.

Runs real auth + live market data + the real scan/signal pipeline for each enabled
symbol and prints WHAT IT WOULD DO. **Places NO orders.** Safe to run any time
(post-market just reflects the last available data). Use before the real-money run.

Run:  python dry_run.py        (Python 3.11)
"""

from __future__ import annotations

import json
import logging

from constants import (
    SYMBOLS_JSON, SHA_TIMEFRAME, SHA_CANDLE_COUNT, SHA_LENGTH, SHA_MA_TYPE,
    SHA_TREND_LENGTH, SHA_TREND_MA_TYPE, RSI_LENGTH, RSI_MA_TYPE, RSI_CANDLE_COUNT,
    RSI_MTF_TIMEFRAMES, TIMEFRAME_TO_FYERS_RES, STEP_UP_BALANCE, UNIT_MAX_LIMIT,
    HEDGE_PER_UNIT, now_ist,
)
from fyers_auth import FyersAuth
from fyers_api import FyersAPI
from indicator import Indicator
from strategy import Strategy, Signal
from api_meter import meter
import scanner

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")


def main() -> None:
    print("=" * 68)
    print("  fyers_live DRY RUN  (shadow — NO ORDERS)  " + now_ist().strftime("%Y-%m-%d %H:%M IST"))
    print("=" * 68)

    config = json.load(open(SYMBOLS_JSON))
    auth = FyersAuth(read_only=False)
    print("Authenticating (real TOTP)…")
    auth.get_model()
    api = FyersAPI(auth)
    ind, strat = Indicator(), Strategy()

    option_df = FyersAPI.download_option_data()
    commodity_df = None
    if any(s.get("asset_type") == "COMMODITY" and s.get("enabled") for s in config["symbols"]):
        commodity_df = FyersAPI.download_commodity_data()

    bal = api.get_balance()
    print(f"Account balance (Total): ₹{bal:,.2f}")

    for s in config.get("symbols", []):
        sym, atype = s["symbol"], s.get("asset_type", "INDEX")
        if not s.get("enabled"):
            print(f"\n[{sym}] disabled — skipped")
            continue
        step = s.get("step_up_balance", STEP_UP_BALANCE)
        cap = s.get("unit_max_limit", UNIT_MAX_LIMIT)
        hpu = s.get("hedge_per_unit", HEDGE_PER_UNIT)

        underlying, session = s["underlying"], None
        if atype == "COMMODITY" and commodity_df is not None:
            res = FyersAPI.resolve_commodity(s.get("root", sym), commodity_df)
            if res:
                underlying, session = res

        print("\n" + "-" * 68)
        print(f"[{sym}] {atype}  underlying={underlying}" + (f"  session={session}" if session else ""))
        units = max(1, min(int(bal // step) + 1, cap))
        print(f"  units={units}  (step ₹{step:,}, cap {cap})  →  profit target ≈ ₹{hpu * units} + charges")

        try:
            df = api.fetch_historical_data(underlying, SHA_TIMEFRAME, SHA_CANDLE_COUNT, market_type=atype)
        except Exception as e:
            print(f"  candle fetch FAILED: {e}")
            continue
        sha = ind.calculate_sha_v3(df, SHA_LENGTH, SHA_MA_TYPE)
        shat = ind.calculate_sha_v3(df, SHA_TREND_LENGTH, SHA_TREND_MA_TYPE)

        rsi_mtf = {}
        for tf in RSI_MTF_TIMEFRAMES:
            try:
                d = api.fetch_historical_data(underlying, TIMEFRAME_TO_FYERS_RES.get(tf, "5"),
                                              RSI_CANDLE_COUNT, market_type=atype)
                rsi_mtf[tf] = round(float(ind.calculate_rsi(d["Close"], RSI_LENGTH, RSI_MA_TYPE).iloc[-1]), 1)
            except Exception:
                rsi_mtf[tf] = 50.0

        buy, sell, ad = strat.calculate_signal(
            df, sha, shat, None, None, units, close_threshold=hpu * units,
            rsi_value=rsi_mtf.get("TIMEFRAME_M1", 50.0), rsi_mtf=rsi_mtf)
        sl, tl = ad.get("sha_power_list", []), ad.get("sha_trend_power_list", [])
        print(f"  SHA signal[0]={sl[0] if sl else '?'} trend[0]={tl[0] if tl else '?'}   RSI {rsi_mtf}")
        print(f"  SIGNAL → buy={buy.name}  sell={sell.name}  (blocked={ad.get('rsi_mtf_blocked')})")

        s["_resolved_underlying"] = underlying
        pair = scanner.scan_best_pair(s, api)
        if not pair:
            print("  SCAN → no pair returned")
            continue
        lotdf = commodity_df if atype == "COMMODITY" else option_df
        try:
            lot = FyersAPI.get_lot_size(pair["CE_Symbol"], lotdf)
        except Exception:
            lot = None
        qty = units * lot if lot else "?"
        print(f"  SCAN → CE {pair['CE_Symbol']} ({pair['CE_Strike']}) @~{pair.get('CE_Premium')}  |  "
              f"PE {pair['PE_Symbol']} ({pair['PE_Strike']}) @~{pair.get('PE_Premium')}")
        print(f"         expiry {pair['Expiry']} (DTE {pair.get('DTE')})  lot(CSV)={lot}  entry_qty={qty}")
        if buy == Signal.BUY:
            print(f"  >> WOULD BUY CE {pair['CE_Symbol']} qty {qty}   (NO ORDER PLACED)")
        elif sell == Signal.SELL:
            print(f"  >> WOULD BUY PE {pair['PE_Symbol']} qty {qty}   (NO ORDER PLACED)")
        else:
            print("  >> no fresh entry this snapshot (flat; SHA/RSI not aligned)")

    snap = meter.snapshot()["categories"]
    print("\nAPI calls used this run:", {k: v["day"]["used"] for k, v in snap.items()})
    print("DRY RUN COMPLETE — NO ORDERS PLACED.")


if __name__ == "__main__":
    main()
