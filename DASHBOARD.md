# Dashboard — access, theme & security

The dashboard is a **separate, read-only Dash web app** (`dashboard.py`) that renders the
JSON state files the engine writes. It never calls Fyers and never trades on its own; the
only actions that reach the engine are the password-gated **Force-close** and (on demo
branches) **Add demo balance**, which it performs by dropping a command file the
supervisor executes on its next tick. Run the engine and the dashboard as two processes.

---

## Access

```
python dashboard.py [port]            # default port 8050
# Windows : start_dashboard.bat [port]
# Linux   : ./start.sh                 (service unit — see LINUX_SETUP.md)
```

- **Local:** `http://127.0.0.1:<port>`
- **Network (phone / another PC):** the app binds **`0.0.0.0`**, so it is reachable at
  **`http://<server-ip>:<port>`** from any device on the same network. The startup log
  prints both URLs:
  `dashboard → http://0.0.0.0:<port>   (reachable at http://<server-ip>:<port>)`
- Each branch is normally run on **its own port** (8050, 8051, …) so several bots can run
  side by side — see **BRANCHES.md** for this deployment's port.

The UI is **responsive / mobile-friendly** — on a phone the panels stack, the charts resize to the screen and wide tables scroll, so there is no desktop-width zooming.

**Open the host firewall** for the port:

- Linux (ufw): `sudo ufw allow <port>/tcp`
- Windows: add an inbound rule allowing the port for Python in Windows Defender Firewall.

**iPhone / Android:** on the same Wi-Fi, browse to `http://<server-ip>:<port>`. If the
server is remote, **do not expose it raw** — use a **VPN, an SSH tunnel, or an
authenticated reverse proxy**. Example SSH tunnel from your laptop:

```
ssh -L 8050:localhost:8050 <user>@<server-ip>      # then open http://localhost:8050
```

---

## Light / dark theme (automatic, by the IST clock)

The dashboard themes itself to the time of day — **light from 06:00 to 18:00 IST, dark
otherwise** — with no toggle and no restart (identical behaviour to the `health_check`
dashboard). The window is two constants at the top of `dashboard.py`:

```python
DAY_START_HOUR, DAY_END_HOUR = 6, 18     # light between 06:00 and 18:00 IST
```

Change them to move the window (e.g. `7, 19`). The two palettes live side by side as
`THEME_DARK` / `THEME_LIGHT`; `_apply_theme()` runs at the top of every 5-second refresh
and recolours the whole UI live — KPI cards, per-symbol panels, Plotly charts (their paper
is transparent, so they inherit the themed card), the strategy log and the API-limit
panel — the moment the clock crosses 06:00 / 18:00.

---

## Market status badge (top strip)

Beside the engine-status dot (**● Engine RUNNING**) the top strip shows a pill telling you what
**today's market** is doing — so a quiet bot is never ambiguous between "markets are shut" and
"something is broken":

- **● Market open** (green) — a segment this bot trades is in its normal session right now.
- **○ Market closed** (grey) — a normal trading day, but outside market hours; the tooltip /
  sub-text reads *Pre-open — trading starts 09:15* or *Closed for the day — reopens 09:15*.
- **◆ Holiday** (blue) — a weekend or an NSE holiday; nothing trades today.
- **★ Unusual open** (amber) — the exchange is open on a day the calendar says is shut: a
  **Diwali Muhurat**, a weekend **Union-Budget** session, or any exchange-declared special session.

The state is **never hardcoded**. The engine reads the broker's own `market_status` each
supervisor tick and writes the verdict into `app_status.json` (the `market` field), so Muhurat /
Budget / any unscheduled session lights up as **Unusual open** automatically — no date tables to
maintain. If the live poll is briefly stale the pill falls back to the static NSE/MCX schedule and
its tooltip notes *from schedule (no live reading)*. On an older state file (no `market` field) the
badge simply renders nothing, so the dashboard stays backward-compatible.

---

## Per-symbol diagnostics — "why (not) trading"

So a quiet bot is never a mystery, each symbol's **ANALYSIS** card opens with a plain-English **"Why"**
line plus the raw entry-gate breakdown:

- **Why** — e.g. *"re-entry cooldown"*, *"⚠ no tradeable pair — scan empty"*, *"wait — index H1 flat"*,
  *"wait — candidate leg H1 not advancing"*, *"wait — candidate leg M15 not advancing"*,
  *"BUY/CE conditions met — entering"*, or *"holding CE — managing exit"*.
- **Gate flags** — `index H1 UP/DOWN/FLAT · leg H1 ✓/– · leg M15 ✓/– · pair ✓/scan-empty ·
  entry-allowed/cooldown` — so you can see exactly which condition is blocking an entry. (`strategy.py`
  exposes `idx_dir`/`h1_ok`/`m15_ok`; the worker forwards them; `dashboard._diag_block` renders them.)

Above that, the **RSI + MACD slopes** block shows, per timeframe, green ▲ / red ▼ / grey – for **both**
the RSI and the MACD pivot-slope, as three rows — **Index**, **CE**, **PE** — across **M5 / M15 / H1 /
H4** (`DISPLAY_TFS`, a display-only set kept separate from the decision timeframes, so changing a
decision TF never blanks the readout). The **pair** block shows the chosen CE/PE strikes, premiums and
expiry (or *scanning…* while flat with no pair yet).

---

## Security — the Force-close password

Force-close flattens a **real open position** (simulated on demo), and the dashboard is
network-reachable, so the button is **password-protected**. Type the password in the
controls-row box before pressing a symbol's **⏹ Force close**. A wrong or empty password is
rejected — `⛔ wrong password — force-close … NOT sent` — and nothing is queued.

**The password is never stored in the repository as plaintext** — this repo is public, so
only its **SHA-256 hash** is compiled into `dashboard.py` (the `_FC_HASH` value). Keep the
plaintext out of every committed file.

### Resetting / changing the password

If the password is compromised (or you just want to change it), use **any one** of the
following and then **restart the dashboard process** (the hash is read once at startup):

**1 — Plaintext env var (easiest).** Set `FORCE_CLOSE_PASSWORD`; it is hashed at startup.

- Linux (systemd): `sudo systemctl edit <your-dashboard-service>` and add, under
  `[Service]`:
  `Environment=FORCE_CLOSE_PASSWORD=YourNewPassword` — then
  `sudo systemctl restart <your-dashboard-service>`.
- Linux (shell): `export FORCE_CLOSE_PASSWORD='YourNewPassword'` before `./start.sh`.
- Windows: `set "FORCE_CLOSE_PASSWORD=YourNewPassword"` before `start_dashboard.bat`.

**2 — Pre-computed hash env var.** Keeps the plaintext out of the environment. Compute the
hash once and set `FORCE_CLOSE_PASSWORD_SHA256` to it:

```
python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "YourNewPassword"
```

**3 — Change the built-in default.** Replace the hash literal in `dashboard.py`
(`_FC_HASH = … "<hash>"`) with the value printed by the command above, then commit / push /
pull. (Env vars 1–2 override this, so they are the quickest reset without a code change.)

**Precedence:** `FORCE_CLOSE_PASSWORD` → `FORCE_CLOSE_PASSWORD_SHA256` → built-in default
hash.

> Never commit the plaintext password. Share it out of band; the repo keeps only the hash.

---

## Controls & history

- **⏹ Force close** (per symbol, today's view): queues a `force_close` command → the
  symbol's worker flattens its open basket on the next tick (a real sell on **live**,
  simulated on **demo**; books P&L). Password-gated (above). To also *pause* re-entry, use
  the brake.
- **＋ Add demo balance** (demo branches only): an amount box + button tops up the
  simulated balance.
- **View date** (top-right): defaults to **Today** and always re-anchors to today on a page
  load / refresh / restore — a tab left open overnight never opens on a stale day, and if it is
  still showing the auto "today" when the clock crosses midnight it rolls forward on its own. Pick
  a past date (today + the previous 6 days) to replay that day's price chart, trades, booked P&L
  and strategy log; an explicit pick is kept until you reload the page. History is kept for ~1 week.
