# Ballom_Fyers
AlgoTrading Project

## 🐧 Running on Linux (Debian 12 "Bookworm")

This is the **`linux_fyers_demo`** branch — the Debian 12 Linux port. Trading logic is identical to the
Windows branch; only the state path and launchers differ. Full guide (systemd auto-start + security
hardening): see **[`LINUX_SETUP.md`](LINUX_SETUP.md)**.

**1. Install Python 3.11 + git** (Debian 12 ships 3.11 natively — no PPA needed):
```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git
```

**2. Clone the repo and check out this branch:**
```bash
git clone https://github.com/sidh-singh/Ballom_Fyers.git ballom && cd ballom
git checkout linux_fyers_demo
```

**3. (Optional) Choose where state/token/cache live** — defaults to `~/Ballom_FYR`; the run user must own it:
```bash
export BALLOM_DIR="$HOME/Ballom_FYR"      # or e.g. /opt/ballom_fyr
```

**4. Start the engine** — the first run auto-creates `.venv/` and installs all dependencies:
```bash
./start_app.sh
```

**5. Start the dashboard** in a second terminal (read-only; binds to 127.0.0.1 only):
```bash
./start_dashboard.sh 8050      # then open http://127.0.0.1:8050
```

> ⚠ **Use Python 3.11 — not 3.13.** `fyers-apiv3`'s `aiohttp` pin has no 3.13 wheel.
>
> **Run it 24×5** with `systemd` (auto-restart across the trading day + the ~00:10 daily reauth) —
> ready-to-edit unit file + security notes (`chmod 600` the token, keep the dashboard on localhost)
> are in [`LINUX_SETUP.md`](LINUX_SETUP.md).

---

