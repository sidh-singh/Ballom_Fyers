#!/usr/bin/env bash
# =============================================================================
#  start_app.sh  —  run the trading ENGINE in the foreground (press Ctrl-C to stop).
#  Good for a quick test. For 24x7 running use the background service instead:
#       sudo systemctl start ballom-<instance>      (see setup.sh / LINUX_SETUP.md)
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ ! -x "$VENV/bin/python" ]; then
    die "Not set up yet for instance '$INSTANCE'. Run this first:  bash setup.sh"
fi

[ "$IS_LIVE" = 1 ] && warn "LIVE instance '$INSTANCE' — this can place REAL orders."
say "Engine starting  |  instance=$INSTANCE  |  data=$BALLOM_DIR"
exec "$VENV/bin/python" -u "$REPO_DIR/$ENGINE"
