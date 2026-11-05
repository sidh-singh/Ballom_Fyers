#!/usr/bin/env bash
# =============================================================================
#  install-service.sh  —  create background services for THIS instance so the bot
#  runs on its own: restarts if it crashes, and starts again after a reboot.
#  Safe to re-run. setup.sh calls this for you; you can also run it directly.
#
#  It creates two systemd services, named after the instance:
#     ballom-<INSTANCE>        -> the trading engine   (application.py)
#     ballom-<INSTANCE>-dash   -> the dashboard        (dashboard.py)
#  Different instances => different service names => they never clash.
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"

[ -x "$VENV/bin/python" ] || die "Python environment missing — run 'bash setup.sh' first."
command -v systemctl >/dev/null 2>&1 || die "systemd not found (no systemctl) — use ./start_app.sh instead."

USER_NAME="$(id -un)"
ENG_UNIT="ballom-$INSTANCE"
DASH_UNIT="ballom-$INSTANCE-dash"

say "Writing service $ENG_UNIT (engine) + $DASH_UNIT (dashboard), user=$USER_NAME"

$SUDO tee "/etc/systemd/system/$ENG_UNIT.service" >/dev/null <<EOF
[Unit]
Description=Ballom trading engine ($INSTANCE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO_DIR
Environment=INSTANCE=$INSTANCE
Environment=BALLOM_DIR=$BALLOM_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python -u $REPO_DIR/$ENGINE
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

$SUDO tee "/etc/systemd/system/$DASH_UNIT.service" >/dev/null <<EOF
[Unit]
Description=Ballom dashboard ($INSTANCE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO_DIR
Environment=INSTANCE=$INSTANCE
Environment=BALLOM_DIR=$BALLOM_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python -u $REPO_DIR/$DASHBOARD $PORT
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
# enable = start automatically on every boot (does NOT start it right now)
$SUDO systemctl enable "$ENG_UNIT" "$DASH_UNIT" >/dev/null 2>&1 || true
ok  "services installed and enabled on boot"
say "start now with:  $SUDO systemctl start $ENG_UNIT   (and  $DASH_UNIT for the dashboard)"
