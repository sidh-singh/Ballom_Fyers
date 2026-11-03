"""
demo_cycle.py — forced ONE-SHOT demo trade cycle (fyers_demo).

Proves the open → fib-add → close logic AND API throttling end-to-end, WITHOUT a real
order and WITHOUT waiting for a live signal: real auth + real scan / quotes / history
(all metered) + SIMULATED buy/add/close on local disk, using the real LTP for P&L.

Run:  python demo_cycle.py        (Python 3.11)
"""

from __future__ import annotations

import json
import logging

from constants import (
    SYMBOLS_JSON, STEP_UP_BALANCE, UNIT_MAX_LIMIT, HEDGE_PER_UNIT,
    estimate_trade_charges, now_ist,
)
from fyers_auth import FyersAuth
from fyers_api import FyersAPI
from demo_broker import DemoFyersAPI
from position_tracker import PositionTracker
from api_meter import meter
import scanner

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")
FIB = [1, 2, 3, 5, 8, 13]


def main() -> None:
    print("=" * 68)
    print("  fyers_demo — FORCED DEMO CYCLE (real reads, simulated orders)  "
          + now_ist().strftime("%Y-%m-%d %H:%M IST"))
    print("=" * 68)

    config = json.load(open(SYMBOLS_JSON))
    sym_cfg = next((s for s in config["symbols"]
                    if s.get("enabled") and s.get("asset_type") == "INDEX"), None)
    if not sym_cfg:
        print("No enabled INDEX symbol in symbols.json")
        return

    auth = FyersAuth(read_only=False)
    print("Authenticating (real)…")
    auth.get_model()
    api = DemoFyersAPI(auth)
    tracker = PositionTracker()
    option_df = FyersAPI.download_option_data()

    bal = api.get_balance()
    step = sym_cfg.get("step_up_balance", STEP_UP_BALANCE)
    cap = sym_cfg.get("unit_max_limit", UNIT_MAX_LIMIT)
    hpu = sym_cfg.get("hedge_per_unit", HEDGE_PER_UNIT)
    units = max(1, min(int(bal // step) + 1, cap))
    print(f"Demo balance ₹{bal:,.0f} → units {units}  (target ≈ ₹{hpu * units} + charges)")

    sym_cfg["_resolved_underlying"] = sym_cfg["underlying"]
    pair = scanner.scan_best_pair(sym_cfg, api, lot_df=option_df)
    if not pair:
        print("Scan returned no pair.")
        return
    ce = pair["CE_Symbol"]
    lot = FyersAPI.get_lot_size(ce, option_df)
    qty = units * lot
    print(f"SCAN  CE {ce} ({pair['CE_Strike']}) ~₹{pair.get('CE_Premium')}  lot {lot} → entry_qty {qty}")

    # ── ENTRY (simulated) ──
    api.buy(ce, qty)
    fill = api.last_fill_price(ce)
    tracker.record_entry(ce, qty, fill, side=1)
    pos = api.get_position(ce)
    print(f"ENTRY netQty {pos['netQty']} avg ₹{pos['netAvg']} ltp ₹{pos['ltp']} → "
          f"cycle_pl ₹{tracker.cycle_pl(ce, pos['ltp'])}")

    # ── ADD (fib level 1) ──
    add_qty = FIB[1] * units * lot
    api.buy(ce, add_qty)
    tracker.record_add(ce, add_qty, api.last_fill_price(ce))
    pos = api.get_position(ce)
    mg = tracker.get_martingale_count(ce)
    print(f"ADD   +{add_qty} → netQty {pos['netQty']} avg ₹{pos['netAvg']} "
          f"mg {mg}  cycle_pl ₹{tracker.cycle_pl(ce, pos['ltp'])}")

    # ── CLOSE (simulated) ──
    net = api.get_position(ce)["netQty"]
    api.sell(ce, net)
    realized = tracker.record_close(ce, api.last_fill_price(ce))
    print(f"CLOSE sold {net} → realized ₹{realized}   demo balance now ₹{api.get_balance():,.2f}")
    print("  (post-market LTP is static, so realized ≈ 0; intraday it moves with the market)")

    snap = meter.snapshot()["categories"]
    print("\nAPI calls used this cycle:", {k: v["day"]["used"] for k, v in snap.items()})
    print("DEMO CYCLE COMPLETE — simulated orders only (no real trade).")


if __name__ == "__main__":
    main()
