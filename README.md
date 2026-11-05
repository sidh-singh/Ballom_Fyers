# Ballom_Fyers
AlgoTrading Project

## 🐧 Running on Linux (Debian 12 "Bookworm")

This is the **`linux_fyers_live`** branch — the Debian 12 / 13 Linux port. Trading logic is identical to the
Windows branch; only the state path and launchers differ. Full guide (systemd auto-start + security
hardening): see **[`LINUX_SETUP.md`](LINUX_SETUP.md)**.

**1. Install Python 3.11 + git.** Your server is **Debian 13 (Trixie)**, whose system Python is 3.13 — and this bot needs **3.11** (`fyers-apiv3`'s `aiohttp` pin has no 3.13 wheel). Debian 13 has no `python3.11` package, so fetch a standalone one with [`uv`](https://docs.astral.sh/uv/):
```bash
sudo apt update && sudo apt install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh     # install uv (standalone Python manager)
source $HOME/.local/bin/env                          # put uv on PATH (or open a new shell)
uv python install 3.11                               # downloads a prebuilt CPython 3.11
```
> On **Debian 12** instead, 3.11 is native — `sudo apt install -y python3.11 python3.11-venv git` and skip uv. The launchers auto-detect whichever you have.

**2. Clone the repo and check out this branch:**
```bash
git clone https://github.com/sidh-singh/Ballom_Fyers.git ballom && cd ballom
git checkout linux_fyers_live
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

> ⚠ **Debian 13 (Trixie) ships Python 3.13 — but this bot needs 3.11** (`fyers-apiv3`'s `aiohttp` pin has no 3.13 wheel). The `uv` step above provides a standalone 3.11; the `.sh` launchers auto-detect it (`$PYTHON` → `python3.11` → uv).
>
> **Run it 24×5** with `systemd` (auto-restart across the trading day + the ~00:10 daily reauth) —
> ready-to-edit unit file + security notes (`chmod 600` the token, keep the dashboard on localhost)
> are in [`LINUX_SETUP.md`](LINUX_SETUP.md).

---

