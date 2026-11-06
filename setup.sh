#!/usr/bin/env bash
# =============================================================================
#  setup.sh  —  ONE command to set up this bot on a fresh Debian 12 / 13 server.
#
#  Run it once (safe to run again any time):
#        bash setup.sh
#
#  It does EVERYTHING for you:
#     1. installs system packages (git, curl, ...)
#     2. installs "uv" (a tiny tool that fetches the right Python)
#     3. installs Python 3.11  (this bot needs 3.11; Debian 13's own Python is 3.13)
#     4. creates this bot's own data folder + Python environment
#     5. downloads every Python library the bot needs
#     6. installs a background service so the bot auto-restarts and survives reboots
#
#  When it finishes it prints exactly how to START and STOP the bot.
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"

echo
say "Ballom setup starting"
say "  instance : $INSTANCE"
say "  data dir : $BALLOM_DIR"
say "  venv     : $VENV"
say "  dashboard: http://127.0.0.1:$PORT  (once started)"
[ "$IS_LIVE" = 1 ] && warn "This is a LIVE instance — once started it can place REAL orders."
echo

# -- 1. system packages ------------------------------------------------------
say "Step 1/6  Installing system packages (git, curl, ca-certificates)..."
$SUDO apt-get update -y
$SUDO apt-get install -y git curl ca-certificates
ok  "system packages ready"

# -- 2. uv -------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
    ok "uv already installed ($(uv --version 2>/dev/null))"
else
    say "Step 2/6  Installing uv (Python version manager)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    [ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
    PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv install failed — see the messages above"
    ok "uv installed ($(uv --version 2>/dev/null))"
fi

# -- 3. Python 3.11 ----------------------------------------------------------
say "Step 3/6  Ensuring Python 3.11 is available..."
PY="$(resolve_python)" || die "could not obtain Python 3.11 (tried python3.11 and uv)"
ok  "using Python: $PY"

# -- 4. data folder ----------------------------------------------------------
say "Step 4/6  Creating data folder: $BALLOM_DIR"
mkdir -p "$BALLOM_DIR/state" "$BALLOM_DIR/cache"
ok  "data folder ready"

# -- 5. venv + libraries -----------------------------------------------------
if [ -x "$VENV/bin/python" ]; then
    ok "Python environment already exists — refreshing libraries..."
else
    say "Step 5/6  Creating Python environment (this can take a few minutes)..."
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
"$VENV/bin/python" -m pip install -r "$REPO_DIR/requirements_fyers.txt"
ok  "all libraries installed"

# -- 6. background service ---------------------------------------------------
say "Step 6/6  Installing the background service (auto-restart + start on boot)..."
if bash "$REPO_DIR/install-service.sh"; then
    ok "background service installed"
else
    warn "service install skipped (no systemd?) — you can still run it in the foreground"
fi

# -- done --------------------------------------------------------------------
echo
say "============================================================"
say " DONE — '$INSTANCE' is installed and ready."
say "============================================================"
cat <<EOF

  WHERE THINGS LIVE
     data + token + logs : $BALLOM_DIR
     dashboard web page   : http://127.0.0.1:$PORT   (after you start it)

  EASIEST — start / stop BOTH the engine AND the dashboard together:
     ./start.sh         # start both (background) — does NOT block your terminal
     ./stop.sh          # stop both

  WATCH / CHECK:
     journalctl -u ballom-$INSTANCE -f               # watch engine logs (Ctrl-C quits)
     $SUDO systemctl status ballom-$INSTANCE         # is the engine running?

  Control each one separately if you want:
     $SUDO systemctl start/stop ballom-$INSTANCE          # engine only
     $SUDO systemctl start/stop ballom-$INSTANCE-dash     # dashboard only

  Or run in the FOREGROUND to watch it live (Ctrl-C stops it):
     ./start_app.sh          # engine
     ./start_dashboard.sh    # dashboard (in a second terminal)

  Full step-by-step guide:  LINUX_SETUP.md
EOF
[ "$IS_LIVE" = 1 ] && warn "LIVE: the engine was NOT auto-started. Start it yourself when you are ready."
echo
