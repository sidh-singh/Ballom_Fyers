#!/usr/bin/env bash
# =============================================================================
#  scripts/common.sh  —  shared config + helpers for the health-check app.
#  Sourced by setup.sh / start*.sh / install-service.sh. You never run it directly.
#
#  Data (status history) + the Python venv live OUTSIDE the repo:
#     ~/ballom-healthcheck/            (override with HEALTH_DIR=)
#  Override anything by exporting first:  PORT=9000  HEALTH_DIR=/data/x  PYTHON=/path/python3
# =============================================================================

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"

INSTANCE="health"
export HEALTH_DIR="${HEALTH_DIR:-$HOME/ballom-healthcheck}"   # constants.py reads this env var
VENV="${VENV:-$HEALTH_DIR/venv}"
PORT="${PORT:-9000}"                                          # separate from fyers 8050-8056

MONITOR="${MONITOR:-monitor.py}"
DASHBOARD="${DASHBOARD:-dashboard.py}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ---- resolve a Python 3 interpreter (any 3.9+; no 3.11 pin for this app) ----
resolve_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
    for p in python3 python3.12 python3.11 python3.10 python; do
        command -v "$p" >/dev/null 2>&1 && { echo "$p"; return 0; }
    done
    return 1
}
