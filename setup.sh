#!/usr/bin/env bash
# =============================================================================
#  setup.sh — one command to set up the health-check app on a fresh Debian/Ubuntu.
#        bash setup.sh
#  Installs python venv + dash/plotly, then a background service for the monitor
#  and the dashboard (auto-restart + start on boot). Safe to re-run.
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"

say "Ballom health-check setup"
say "  data dir  : $HEALTH_DIR"
say "  dashboard : http://0.0.0.0:$PORT  (once started)"

say "Step 1/4  system packages (python3-venv, iputils-ping)..."
$SUDO apt-get update -y
$SUDO apt-get install -y python3-venv python3-pip iputils-ping ca-certificates
ok "system packages ready"

PY="$(resolve_python)" || die "no python3 found"
ok "using Python: $PY"

say "Step 2/4  data folder: $HEALTH_DIR"
mkdir -p "$HEALTH_DIR/state/health"

if [ -x "$VENV/bin/python" ]; then
    ok "venv exists — refreshing libraries"
else
    say "Step 3/4  creating venv..."
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"
ok "libraries installed"

say "Step 4/4  installing background services..."
if bash "$REPO_DIR/install-service.sh"; then
    ok "services installed"
else
    warn "service install skipped (no systemd?) — run in foreground instead"
fi

cat <<MSG

  DONE.
    Start both (background) :  ./start.sh
    Stop both              :  ./stop.sh
    Foreground (test)      :  ./start_monitor.sh   and   ./start_dashboard.sh
    Dashboard              :  http://<server-ip>:$PORT     (or SSH-tunnel 127.0.0.1:$PORT)
    Watch monitor logs     :  journalctl -u ballom-health -f
MSG
