# Ballom_Fyers
AlgoTrading Project

## 🐧 Run on a Linux server (Debian 12 / 13) — `linux_fyers_hedging_demo`

This is the **Linux** branch. Trading logic is identical to the Windows branch; only the launchers
and the data path differ. **New to Linux? Follow [`LINUX_SETUP.md`](LINUX_SETUP.md)** — it explains
every command.

### One-command install

Log into your server over SSH, then paste this **one block**:

```bash
sudo apt update && sudo apt install -y git
git clone -b linux_fyers_hedging_demo https://github.com/sidh-singh/Ballom_Fyers.git ~/ballom
cd ~/ballom
bash setup.sh
```

`setup.sh` installs **everything by itself** — `uv`, Python 3.11, all libraries, the data folder,
and a background service. When it finishes it prints the start/stop commands.

### Start / stop / watch

Easiest — **one command starts both** the engine and the dashboard (in the background):

```bash
./start.sh                                      # start BOTH engine + dashboard
./stop.sh                                       # stop both
journalctl -u ballom-fyers_hedging_demo -f              # watch the engine live (Ctrl-C to quit)
```

> **`*_live` instances place real orders** once funded; **`*_demo` instances simulate** (no real
> orders). On a `*_live` instance `./start.sh` asks you to type `yes` first (skip with `./start.sh -y`).

Want to control them separately? `sudo systemctl start/stop ballom-fyers_hedging_demo` (engine) and
`…-dash` (dashboard). Or run one in the foreground to watch it live: `./start_app.sh` /
`./start_dashboard.sh`.

### Viewing the dashboard
It binds to `127.0.0.1` only (not the internet). From your laptop, open an SSH tunnel then browse
`http://127.0.0.1:8050` — see [`LINUX_SETUP.md` §5](LINUX_SETUP.md).

### Running more than one bot (live + demo, or future brokers)
Every bot is an isolated **instance** (own data folder, venv, port, service) — they never clash.
See [`LINUX_SETUP.md` §7](LINUX_SETUP.md). Example: `PORT=8051 bash setup.sh` for a second one.

> ⚠ **Python 3.11 required — not 3.13.** Debian 13's system Python is 3.13; `setup.sh` fetches 3.11
> via `uv` automatically. (`fyers-apiv3`'s `aiohttp` pin has no 3.13 wheel.)

### Files
| File | Purpose |
|---|---|
| `setup.sh` | one-command installer (run this first) |
| **`start.sh` / `stop.sh`** | start / stop BOTH engine + dashboard together (everyday commands) |
| `start_app.sh` / `start_dashboard.sh` | (advanced) run engine / dashboard in the foreground |
| `install-service.sh` | (re)install the background systemd services |
| `scripts/common.sh` | shared config — namespaces every bot by instance |
| `LINUX_SETUP.md` | full beginner guide |
