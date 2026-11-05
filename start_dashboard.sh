#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# fyers_netting_live (Linux / Debian 12) — DASHBOARD (read-only, separate process).
# Usage: ./start_dashboard.sh [port]   (default 8050; binds to 127.0.0.1 only)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3.11}"
VENV=".venv"
PORT="${1:-8050}"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv with $PYTHON ..."
    "$PYTHON" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip setuptools wheel
    "$VENV/bin/pip" install -r requirements_fyers.txt
fi

echo "Dashboard -> http://127.0.0.1:$PORT"
exec "$VENV/bin/python" -u dashboard.py "$PORT"
