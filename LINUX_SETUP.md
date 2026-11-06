# Running this bot on a Linux server (Debian 12 / 13) — `linux_fyers_netting_live`

Written for someone **new to Linux**. Every command is explained. If you can open a
terminal (the black screen) on your server and type, you can do this.

> **TL;DR** — copy-paste **one block** ([§2](#2-the-one-command-install)) and the bot installs
> itself. Everything after that is "how to start it, watch it, and run more than one."

---

## 1. What you need

- A **Debian 12 or 13** server you can log into over **SSH** (you bought this already).
- Your server login: **`ssh root@80.150.138.80`** (user `root`, IP `80.150.138.80`, SSH port `22` — the default).
  Run that on your own laptop first — you should land at a prompt like `root@...:~#`.

That `~$` prompt is where every command below is typed.

---

## 2. The one-command install

Copy-paste this **whole block** at the prompt and press Enter. It installs git, downloads the
bot, and then runs `setup.sh`, which installs **everything else by itself** (uv, Python 3.11, all
libraries, the data folder, and a background service):

```bash
sudo apt update && sudo apt install -y git
git clone -b linux_fyers_netting_live https://github.com/sidh-singh/Ballom_Fyers.git ~/ballom
cd ~/ballom
bash setup.sh
```

That's it. The script prints each step (`Step 1/6 ...`) so you can see progress. When it finishes
it shows you the start/stop commands. **Nothing else to install by hand.**

> If a command ever says `sudo: command not found`, you are already the **root** user — just
> remove the word `sudo` from that command and run it again.

---

## 3. What those commands did (so you understand your server)

| Command | What it does |
|---|---|
| `sudo apt update` | refreshes the list of installable software (`sudo` = "do this as administrator") |
| `sudo apt install -y git` | installs **git**, the tool that downloads code (`-y` = "yes, don't ask") |
| `git clone -b linux_fyers_netting_live …` | downloads this branch of the bot into a folder `~/ballom` (`~` = your home folder) |
| `cd ~/ballom` | "change directory" — moves you **into** that folder |
| `bash setup.sh` | runs the installer script |

Inside `setup.sh`, the 6 steps are: system packages → **uv** (fetches Python) → **Python 3.11** →
your data folder → the Python libraries → the **background service**. Python 3.11 is required
because the Fyers library doesn't work on Debian 13's built-in Python 3.13.

---

## 4. Start, stop, and watch the bot

The bot runs as **services** — programs Linux keeps running in the background (they restart if they
crash and come back after a reboot). The engine and the dashboard are **two separate services**.

**The easy way — one command starts BOTH:**
```bash
./start.sh        # starts the engine AND the dashboard (in the background)
./stop.sh         # stops both
```
`./start.sh` does **not** block your terminal, and both keep running after you log out. On a `_live`
instance it asks you to type `yes` first (skip with `./start.sh -y`).

**Watch what the engine is doing, live:**
```bash
journalctl -u ballom-fyers_netting_live -f
```
(`-f` = "follow". Press **Ctrl-C** to stop watching — this does **not** stop the bot.)

**Control each service separately** (only if you ever need to):
```bash
sudo systemctl start   ballom-fyers_netting_live        # engine only
sudo systemctl stop    ballom-fyers_netting_live
sudo systemctl status  ballom-fyers_netting_live        # running? (green = good; press q to exit)
sudo systemctl start   ballom-fyers_netting_live-dash   # dashboard only
```

### LIVE vs DEMO — important
- Instances ending in **`_live`** place **REAL orders** with real money once started and funded.
  The installer never auto-starts the engine — **you** start it (the command above), and only when
  you're ready.
- Instances ending in **`_demo`** are a **simulation** — no real orders. Safe to start and leave
  running to watch how it behaves.

> First time on live? Start it **after** market hours once and watch `journalctl` to confirm it
> logs in and fetches data cleanly, before a trading day.

---

## 5. View the dashboard (from your own laptop)

The dashboard runs on the server at `http://127.0.0.1:8053`. `127.0.0.1` means "this machine
only" — for safety it is **not** open to the internet. To see it from your laptop, make an **SSH
tunnel**: run this **on your laptop** (not the server), using your normal login:

```bash
ssh -L 8053:127.0.0.1:8053 root@80.150.138.80
```

Leave that window open, then first start the dashboard service **on the server**:
```bash
sudo systemctl start ballom-fyers_netting_live-dash
```
Now open **`http://127.0.0.1:8053`** in your laptop's browser. The tunnel forwards it securely.

---

## 6. Where your data lives

Everything this bot writes is under **one folder**, separate from the code:

```
~/ballom-trading/fyers_netting_live/
    state/              signal / account / api-usage JSON (what the dashboard reads)
    cache/              downloaded symbol lists
    fyers_token.json    your login token (keep it private!)
    venv/               this bot's private Python + libraries
```

Lock the token down once: `chmod 600 ~/ballom-trading/fyers_netting_live/fyers_token.json`.

**Your config — `symbols.json` (in the repo folder, `~/ballom`):** this is the one file you edit to
choose symbols, units and per-symbol settings. On first run it's created automatically from the
shipped template `symbols.example.json`. It is **git-ignored**, so a `git pull` / update can **never
overwrite your edits** — your settings are safe forever. (Updated defaults, if any, arrive in
`symbols.example.json`; diff it against yours when you like.)

---

## 7. Run MORE THAN ONE bot (live + demo, or future brokers)

This is built to be safe. Each bot is an **instance** with its own data folder, its own Python
env, its own dashboard port, and its own service name — so they **never interfere**.

**Every branch is preset to its own dashboard port (8050–8055), so they never clash** — you
don't set anything. `./start.sh` prints the exact port for the branch you're on. The order is
demo→live per family: fyers 8050/8051, netting 8052/8053, hedging 8054/8055.

**Example — run the demo alongside this one:**
```bash
git clone -b linux_fyers_demo https://github.com/sidh-singh/Ballom_Fyers.git ~/ballom-demo
cd ~/ballom-demo && bash setup.sh            # auto-uses its own port + data dir + service
./start.sh                                   # no PORT= needed
```
Each instance gets its own `~/ballom-trading/<instance>/` folder and `ballom-<instance>` service, so nothing overlaps.

**Future brokers in this same repo:** the instance name drives everything. A new broker just runs
with its own `INSTANCE` (and, if its entry file differs, `ENGINE`):
```bash
INSTANCE=zerodha_live ENGINE=zerodha_app.py PORT=8052 bash setup.sh
```
It gets `~/ballom-trading/zerodha_live/`, port 8052, and service `ballom-zerodha_live` — fully
isolated from Fyers. **Nothing you add later can clash with what's already running.**

The knobs you can set before any command (all optional — sensible defaults otherwise):

| Variable | Default | Meaning |
|---|---|---|
| `INSTANCE` | the branch name (minus `linux_`) | the bot's unique name — drives data dir + service |
| `PORT` | preset per branch (8050–8055) | dashboard port; override to change it |
| `BALLOM_DIR` | `~/ballom-trading/<INSTANCE>` | where data/token/cache live |
| `ENGINE` | `application.py` | the engine entry file (future brokers may differ) |
| `PYTHON` | auto | force a specific Python 3.11 path |

---

## 8. Update the bot later

`./start.sh` now **pulls the latest code automatically** every time (and refreshes the libraries if
a change needs it). So to apply an update, just restart:

```bash
./stop.sh && ./start.sh
```

Notes:
- The auto-pull is **fast-forward only** and never overwrites your edits. If you've changed a tracked
  file on the server (e.g. `symbols.json`), the pull simply **skips with a warning** and starts with
  your current code — your config is safe.
- Skip the auto-pull for one run with `NO_PULL=1 ./start.sh`.
- Fully manual alternative: `cd ~/ballom && git pull && bash setup.sh`.

## 9. If something goes wrong

| Symptom | Fix |
|---|---|
| `setup.sh` can't find Python 3.11 | re-run `bash setup.sh` — it installs it via `uv`. Or: `uv python install 3.11`. |
| `sudo: command not found` | you're root already — drop `sudo` from the command. |
| service won't start | `journalctl -u ballom-fyers_netting_live -n 50` shows the last 50 log lines with the error. |
| `bad interpreter` on a `.sh` | the file got Windows line-endings; run `sed -i 's/\r$//' *.sh scripts/*.sh`. |
| dashboard won't load | make sure the SSH tunnel ([§5](#5-view-the-dashboard-from-your-own-laptop)) is open and the `-dash` service is started. |

## 10. Security (it's a money bot)

- Use a **dedicated non-root user** if you can; `chmod 600` the token file.
- The dashboard stays on `127.0.0.1` — reach it **only** through the SSH tunnel. Never open the
  dashboard port (8050–8055) to the internet.
- Keep the server patched: `sudo apt update && sudo apt upgrade -y` occasionally.
- The TOTP secret / PIN live in `fyers_auth.py`, so keep the repo folder private.

---

### Why Python 3.11 (not 3.13)
`fyers-apiv3==3.1.7` pins `aiohttp==3.8.4`, which has **no wheel for Python 3.13** and won't build
there. Debian 13 (Trixie) ships Python 3.13 and has **no `python3.11` apt package**, so `setup.sh`
fetches a standalone 3.11 with **`uv`**. Debian 12 (Bookworm) has 3.11 natively. The scripts
auto-detect whichever is present (`$PYTHON` → `python3.11` → uv).
