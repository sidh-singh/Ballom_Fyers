# LIVE_DRYRUN — first real-money validation of `fyers_live`

> ⚠️ **This bot places REAL orders with REAL money. Fyers has no paper trading.**
> The first validation IS a live trade. Do it with the **smallest possible size**,
> during market hours, at a desk where you can manually square off instantly.
> Nothing here is automated by Claude — you run each step.

---

## 0. One-time setup
- [ ] Machine has **Python 3.11** (`py -3.11 --version`). 3.13 will not install the deps.
- [ ] `start_app.bat` installs deps on first run; or `py -3.11 -m pip install -r requirements_fyers.txt`.
- [ ] Confirm the **API plan** in the Fyers dashboard is **Standard** (meter limits assume it).
- [ ] Credentials in `fyers_auth.py` are current (TOTP secret / PIN / app secret).

## 1. Shrink risk for the first run
- [ ] In `symbols.json`: only **NIFTY** `enabled: true`; GOLD/SILVER `false`.
- [ ] Set **`"unit_max_limit": 1`** on NIFTY → caps entry at **1 lot (65 qty)** regardless of
      balance, so the whole ladder/close cycle is validated at minimum size.
- [ ] Keep enough margin for ~1 buy option lot + a few fib adds, nothing more.
- [ ] `"brake": 0` (engine runs). Know that **`brake:1` pauses all workers** within ~30 s.

## 2. Launch & watch (two terminals)
- [ ] Terminal A: `start_app.bat` — watch the engine log.
- [ ] Terminal B: `start_dashboard.bat` → open http://127.0.0.1:8050.

**Confirm in order (engine log):**
- [ ] Startup auth OK + heartbeat started.
- [ ] `daily_maintenance` — CSV downloaded, holidays loaded.
- [ ] One `worker-NIFTY` thread started; inside 09:15–15:30 window.
- [ ] Dashboard **API-usage meter** increments as calls happen (DATA stays well under 5,000/day).

## 3. Watch ONE full cycle
- [ ] When index SHA+RSI align, a `SCAN` runs and an **ENTRY** fires: `BUY CE` (or `PE`) **65 qty**.
- [ ] Dashboard/log shows the leg; `position_tracker.json` has an `ENTRY` row.
- [ ] (If RSI dips) an **ADD** of fib qty (next: 130) — verify qty = `fib×units×lot`.
- [ ] When `effective_pl > ₹100×units + charges`, a **CLOSE** fires (sell to flat).
- [ ] After `netQty==0`: `CLOSE_BOOKED` logged, `booked_profit` snapshot taken, a **bar appears
      on the dashboard booking chart** (green if profit).
- [ ] Net booked ≈ **₹100 after taxes** for 1 unit (that's the whole point — confirm taxes didn't eat it).

## 4. Kill switches (practice BEFORE you need them)
- [ ] `brake:1` in `symbols.json` → workers pause (no new entries/adds); existing position is left as-is.
- [ ] `Ctrl+C` in Terminal A → clean shutdown (heartbeat stops, workers join).
- [ ] **Manual square-off in the Fyers app/web** is the ultimate stop — know where it is.
- [ ] The engine **adopts** any position you leave open on restart (`_recover_active`) and will
      try to manage/close it — so flatten manually if you don't want it managed.

## 5. Watch this run (most rough edges now hardened — TRADING_LOGIC.md §5–7)
- [ ] **Fill prices (KEY):** cost basis uses the tradebook fill price (fallback ltp).
      Confirm `last_fill_price` returns the real traded price on your account — if it
      silently falls back to ltp, booked P&L can be off by the bid/ask spread.
- [ ] **Overnight carry:** now a self-tracked cost basis preserved across midnight; still
      eyeball day-2 `cycle_pl` once against the Fyers positions screen.
- [ ] **Balance basis:** `BALANCE_SOURCE_TITLES` = Total → Available — confirm the tier
      uses the balance you intend.
- [ ] **API budget:** meter throttles; H1/H4/H6 RSI are cached per bar. Watch for real `-429`.

## 6. Stop criteria (abort the run if)
- any order rejects repeatedly; `netQty` doesn't match what the tracker thinks; effective_pl looks
  wrong vs the Fyers positions screen; or API usage climbs unexpectedly fast.

---
After a clean cycle at `unit_max_limit:1`, step the cap back up (`10`) and/or enable commodities only
once their symbols/timings are confirmed (ARCHITECTURE O4).
