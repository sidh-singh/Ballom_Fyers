# Running on Linux (Debian 12 "Bookworm") — `linux_fyers_demo`

Linux port of **`fyers_demo`**. Trading logic is byte-identical to the Windows branch; only the
OS-specific bits changed:

- **`constants.py` → `BALLOM_DIR` is OS-aware.** `$BALLOM_DIR` if set, else `~/Ballom_FYR` on Linux
  (`C:/Ballom_FYR` on Windows). Token, cache and state all root off this one constant.
- **Launchers:** `start_app.bat` / `start_dashboard.bat` → **`start_app.sh` / `start_dashboard.sh`**
  (venv-based, Python 3.11).

## One-time setup
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
git clone <your-repo-url> ballom && cd ballom
git checkout linux_fyers_demo
# optional: pick where state/token/cache live (default ~/Ballom_FYR); the run user must own it
export BALLOM_DIR="$HOME/Ballom_FYR"
```

## Run
```bash
./start_app.sh              # engine — first run builds .venv and installs deps
./start_dashboard.sh 8050   # dashboard (separate terminal) -> http://127.0.0.1:8050
```
First run of either script creates `.venv/` and installs `requirements_fyers.txt` (which already
contains dash + plotly, so one venv serves both processes).

## Why Python 3.11 (not 3.13)
`fyers-apiv3==3.1.7` pins an `aiohttp` with no 3.13 wheel. **Debian 12 ships 3.11 natively** — no PPA
or pyenv needed.

## Unattended with systemd
`/etc/systemd/system/ballom-linux_fyers_demo.service` (edit `User=` and the two paths to match your box):
```ini
[Unit]
Description=Ballom Fyers engine (linux_fyers_demo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ballom
WorkingDirectory=/home/ballom/ballom
Environment=BALLOM_DIR=/home/ballom/Ballom_FYR
ExecStart=/home/ballom/ballom/.venv/bin/python -u application.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ballom-linux_fyers_demo
journalctl -u ballom-linux_fyers_demo -f
```
(Run the dashboard the same way with a second unit calling `dashboard.py`, or just launch it
on demand with `./start_dashboard.sh`.)

## Security notes (this is a money bot)
- Dedicated **non-root** user; `chmod 600` the token file under `$BALLOM_DIR`, lock down the repo dir
  (the TOTP secret/PIN live in `fyers_auth.py`).
- Dashboard binds to **127.0.0.1** — keep it local or behind an SSH tunnel; **never expose 8050**.
- Keep the box patched (`unattended-upgrades`) and firewalled.
- **Timezone:** the code is IST-aware (`now_ist()`), so a **UTC server is fine**; `tzdata` is pinned.
