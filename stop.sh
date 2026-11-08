#!/usr/bin/env bash
# stop.sh — stop BOTH the dashboard and the health monitor.
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
command -v systemctl >/dev/null 2>&1 || die "no systemd — Ctrl-C the foreground scripts"
say "Stopping dashboard + monitor..."
$SUDO systemctl stop ballom-health-dash ballom-health || true
ok "stopped.  Start again: ./start.sh"
