#!/usr/bin/env bash
# start_monitor.sh — run the health MONITOR in the foreground (Ctrl-C to stop).
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
[ -x "$VENV/bin/python" ] || die "Not set up yet. Run first:  bash setup.sh"
say "Health monitor starting  |  data=$HEALTH_DIR"
exec "$VENV/bin/python" -u "$REPO_DIR/$MONITOR"
