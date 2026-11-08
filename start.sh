#!/usr/bin/env bash
# start.sh — start BOTH the health monitor and the dashboard (background services).
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
command -v systemctl >/dev/null 2>&1 || die "no systemd — use ./start_monitor.sh and ./start_dashboard.sh"
[ -f /etc/systemd/system/ballom-health.service ] || die "not installed — run:  bash setup.sh"
say "Starting health monitor + dashboard..."
$SUDO systemctl start ballom-health ballom-health-dash
sleep 1
for u in ballom-health ballom-health-dash; do
    if $SUDO systemctl is-active --quiet "$u"; then ok "$u running"; else warn "$u failed — journalctl -u $u -n 30"; fi
done
say "Dashboard: http://<server-ip>:$PORT   (stop both: ./stop.sh)"
