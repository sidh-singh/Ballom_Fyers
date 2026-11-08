#!/usr/bin/env bash
# start_dashboard.sh — run the health DASHBOARD in the foreground (Ctrl-C to stop).
# Usage: ./start_dashboard.sh [port]      (default 9000)
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
PORT="${1:-$PORT}"
[ -x "$VENV/bin/python" ] || die "Not set up yet. Run first:  bash setup.sh"
say "Health dashboard starting  |  http://0.0.0.0:$PORT"
exec "$VENV/bin/python" -u "$REPO_DIR/$DASHBOARD" "$PORT"
