# Ballom_Fyers
AlgoTrading Project

## 🐧 Run on a Linux server (Debian 12 / 13) — `linux_fyers_live`

This is the **Linux** branch. Trading logic is identical to the Windows branch; only the launchers
and the data path differ. **New to Linux? Follow [`LINUX_SETUP.md`](LINUX_SETUP.md)** — it explains
every command.

### One-command install

Log into your server over SSH, then paste this **one block**:

```bash
sudo apt update && sudo apt install -y git
git clone -b linux_fyers_live https://github.com/sidh-singh/Ballom_Fyers.git ~/ballom
cd ~/ballom
bash setup.sh
```

`setup.sh` installs **everything by itself** — `uv`, Python 3.11, all libraries, the data folder,
and a background service. When it finishes it prints the start/stop commands.

### Start / stop / watch

```bash
sudo systemctl start   ballom-fyers_live        # start the trading engine
sudo systemctl stop    ballom-fyers_live        # stop it
journalctl -u ballom-fyers_live -f              # watch live logs (Ctrl-C to quit)
sudo systemctl start   ballom-fyers_live-dash   # start the dashboard (port 8050)
```

> **`*_live` instances place real orders** once funded; **`*_demo` instances simulate** (no real
> orders). The engine is never auto-started — you start it with the command above when ready.

Prefer the foreground (Ctrl-C to stop)? `./start_app.sh` (engine) and `./start_dashboard.sh`
(dashboard).

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
| `start_app.sh` / `start_dashboard.sh` | run engine / dashboard in the foreground |
| `install-service.sh` | (re)install the background systemd services |
| `scripts/common.sh` | shared config — namespaces every bot by instance |
| `LINUX_SETUP.md` | full beginner guide |
