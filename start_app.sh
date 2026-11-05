#!/usr/bin/env bash
# --------------------------------------------------------------------------------
# fyers_hedging_live (Linux / Debian 12 & 13) -- ENGINE process.  LIVE — places REAL orders when funded
# One process runs ALL enabled symbols (per-symbol worker threads inside the engine).
#
# First run creates .venv and installs requirements_fyers.txt.
# Needs Python 3.11 (fyers-apiv3's aiohttp pin has no 3.13 wheel):
#   Debian 12:  sudo apt install -y python3.11 python3.11-venv
#   Debian 13:  curl -LsSf https://astral.sh/uv/install.sh | sh  &&  uv python install 3.11
# State/token/cache dir defaults to ~/Ballom_FYR -- override: export BALLOM_DIR=/opt/ballom_fyr
# --------------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

resolve_python() {
    # Prints a Python 3.11 interpreter on stdout; returns 1 if none found. Order:
    # explicit $PYTHON, then python3.11 on PATH (Debian 12), then a uv-managed
    # standalone 3.11 (Debian 13+ has no python3.11 apt package).
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
    if command -v python3.11 >/dev/null 2>&1; then echo "python3.11"; return 0; fi
    if command -v uv >/dev/null 2>&1; then
        uv python find 3.11 2>/dev/null && return 0
        echo ">> no python3.11 found - fetching a standalone one via uv ..." >&2
        uv python install 3.11 1>&2 || true
        uv python find 3.11 2>/dev/null && return 0
    fi
    return 1
}
if [ ! -x "$VENV/bin/python" ]; then
    PY="$(resolve_python)" || {
        echo "ERROR: Python 3.11 is required (fyers-apiv3's aiohttp pin has no 3.13 wheel)." >&2
        echo "  Debian 12:  sudo apt install -y python3.11 python3.11-venv" >&2
        echo "  Debian 13:  curl -LsSf https://astral.sh/uv/install.sh | sh  &&  uv python install 3.11" >&2
        echo "  or: export PYTHON=/path/to/python3.11 and re-run." >&2
        exit 1
    }
    echo "Creating virtualenv with: $PY"
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip setuptools wheel
    "$VENV/bin/pip" install -r requirements_fyers.txt
fi

echo "============================================================"
echo "  STARTING fyers_hedging_live ENGINE  (LIVE — real orders when funded)"
echo "============================================================"
exec "$VENV/bin/python" -u application.py
