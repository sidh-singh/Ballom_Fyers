#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# fyers_hedging_demo (Linux / Debian 12) — ENGINE process.  DEMO — simulated, no real orders
# One process runs ALL enabled symbols (per-symbol worker threads inside the engine).
#
# First run creates .venv and installs requirements_fyers.txt.
# Needs Python 3.11:  sudo apt install -y python3.11 python3.11-venv
# State/token/cache dir defaults to ~/Ballom_FYR — override: export BALLOM_DIR=/opt/ballom_fyr
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.11}"
VENV=".venv"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv with $PYTHON ..."
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip setuptools wheel
    "$VENV/bin/pip" install -r requirements_fyers.txt
fi

echo "============================================================"
echo "  STARTING fyers_hedging_demo ENGINE  (DEMO — simulated)"
echo "============================================================"
exec "$VENV/bin/python" -u application.py
