#!/usr/bin/env bash
# =============================================================================
#  start_dashboard.sh  —  run the DASHBOARD in the foreground (Ctrl-C to stop).
#  Usage: ./start_dashboard.sh [port]      (default is this instance's PORT)
#  The dashboard is read-only and binds to 127.0.0.1 only. To view it from your
#  own laptop, open an SSH tunnel (see LINUX_SETUP.md -> "View the dashboard").
#  For 24x7 running use:  sudo systemctl start ballom-<instance>-dash
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

PORT="${1:-$PORT}"

if [ ! -x "$VENV/bin/python" ]; then
    die "Not set up yet for instance '$INSTANCE'. Run this first:  bash setup.sh"
fi

say "Dashboard starting  |  instance=$INSTANCE  |  http://127.0.0.1:$PORT"
exec "$VENV/bin/python" -u "$REPO_DIR/$DASHBOARD" "$PORT"
