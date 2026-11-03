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
    SYMBOLS_JSON, RSI_LENGTH, RSI_MA_TYPE, RSI_CANDLE_COUNT,
    RSI_DCA_LADDER_TIMEFRAMES, TIMEFRAME_TO_FYERS_RES,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SLOPE_TIMEFRAMES, PEAK_LOOKBACK, PIVOT_WIDTH, RSI_PEAK_OB, RSI_PEAK_OS,
    STEP_UP_BALANCE, UNIT_MAX_LIMIT, HEDGE_PER_UNIT, now_ist,
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

        # Build per-TF RSI (ladder tiers) + RSI/MACD latest-pivot slopes (mirror the worker).
        needed, rsi_mtf, slopes, df = [], {}, {}, None
        for tf in (["TIMEFRAME_M1"] + list(RSI_DCA_LADDER_TIMEFRAMES) + list(SLOPE_TIMEFRAMES)):
            if tf not in needed:
                needed.append(tf)
        for tf in needed:
            try:
                d = api.fetch_historical_data(underlying, TIMEFRAME_TO_FYERS_RES.get(tf, "5"),
                                              RSI_CANDLE_COUNT, market_type=atype)
            except Exception:
                rsi_mtf[tf] = 50.0
                continue
            if tf == "TIMEFRAME_M5":
                df = d
            rsi_s = ind.calculate_rsi(d["Close"], RSI_LENGTH, RSI_MA_TYPE)
            rsi_mtf[tf] = round(float(rsi_s.iloc[-1]), 1) if len(rsi_s) else 50.0
            if tf in SLOPE_TIMEFRAMES:
                macd_df = ind.calculate_macd(d["Close"], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
                rsl = ind.latest_pivot_slope(rsi_s, lookback=PEAK_LOOKBACK, width=PIVOT_WIDTH,
                                             ob_level=RSI_PEAK_OB, os_level=RSI_PEAK_OS)
                sig = float(macd_df["signal"].iloc[-1]) if len(macd_df) else 0.0
                side = "high" if sig > 0 else ("low" if sig < 0 else None)
                msl = ind.latest_pivot_slope(macd_df["hist"], lookback=PEAK_LOOKBACK,
                                             width=PIVOT_WIDTH, side=side)
                slopes[tf.replace("TIMEFRAME_", "")] = {"rsi": rsl, "macd": msl}

        buy, sell, ad = strat.calculate_signal(
            df, None, None, units, close_threshold=hpu * units,
            rsi_value=rsi_mtf.get("TIMEFRAME_M1", 50.0), rsi_mtf=rsi_mtf,
            slopes=slopes, entry_allowed=True)
        print(f"  scores → open={ad.get('open_score')} book={ad.get('book_score')} "
              f"fc={ad.get('fc_score')}   entry_dir={ad.get('entry_direction')}")
        print(f"  RSI {rsi_mtf}")
        print(f"  SIGNAL → buy={buy.name}  sell={sell.name}  (chop={ad.get('entry_chop_block')})")

        s["_resolved_underlying"] = underlying
        pair = scanner.scan_best_pair(s, api, lot_df=(commodity_df if atype == "COMMODITY" else option_df))
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
            print("  >> no fresh entry this snapshot (flat; slope scores not aligned)")

    snap = meter.snapshot()["categories"]
    print("\nAPI calls used this run:", {k: v["day"]["used"] for k, v in snap.items()})
    print("DRY RUN COMPLETE — NO ORDERS PLACED.")


if __name__ == "__main__":
    main()
