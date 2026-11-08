#!/usr/bin/env bash
# install-service.sh — create systemd units for the monitor + dashboard, enable + (re)start.
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
command -v systemctl >/dev/null 2>&1 || { warn "no systemd"; exit 1; }
USER_NAME="$(id -un)"

$SUDO tee /etc/systemd/system/ballom-health.service >/dev/null <<UNIT
[Unit]
Description=Ballom server health monitor
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO_DIR
Environment=HEALTH_DIR=$HEALTH_DIR
ExecStart=$VENV/bin/python -u $REPO_DIR/$MONITOR
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

$SUDO tee /etc/systemd/system/ballom-health-dash.service >/dev/null <<UNIT
[Unit]
Description=Ballom server health dashboard
After=network-online.target ballom-health.service
Wants=network-online.target
[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$REPO_DIR
Environment=HEALTH_DIR=$HEALTH_DIR
Environment=PORT=$PORT
ExecStart=$VENV/bin/python -u $REPO_DIR/$DASHBOARD $PORT
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable ballom-health ballom-health-dash >/dev/null 2>&1 || true
ok "services installed (ballom-health, ballom-health-dash)"
