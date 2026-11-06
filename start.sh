#!/usr/bin/env bash
# =============================================================================
#  start.sh  —  start BOTH the trading engine AND the dashboard, in one command.
#
#        ./start.sh
#
#  They run in the background (as services), don't block your terminal, and keep
#  running after you log out. Stop them both with:  ./stop.sh
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"

ENG="ballom-$INSTANCE"
DASH="ballom-$INSTANCE-dash"

command -v systemctl >/dev/null 2>&1 \
    || die "no systemd here — use ./start_app.sh and ./start_dashboard.sh (foreground) instead."
[ -f "/etc/systemd/system/$ENG.service" ] \
    || die "services not installed yet — run this first:  bash setup.sh"

# LIVE safety: confirm before starting a real-order engine (skip with: ./start.sh -y  or  YES=1)
if [ "$IS_LIVE" = 1 ] && [ "${1:-}" != "-y" ] && [ "${YES:-}" != "1" ]; then
    warn "'$INSTANCE' is a LIVE instance — the engine can place REAL orders once funded."
    printf "   Type 'yes' to start the LIVE engine + dashboard: "
    read -r reply || reply=""
    [ "$reply" = "yes" ] || die "cancelled — nothing started."
fi

say "Starting engine + dashboard for '$INSTANCE'..."
$SUDO systemctl start "$ENG" "$DASH"
sleep 1
for u in "$ENG" "$DASH"; do
    if $SUDO systemctl is-active --quiet "$u"; then
        ok "$u is running"
    else
        warn "$u did NOT start — check:  journalctl -u $u -n 30 --no-pager"
    fi
done

echo
say "Dashboard : http://127.0.0.1:$PORT   (open the SSH tunnel from your laptop — LINUX_SETUP.md section 5)"
say "Live logs : journalctl -u $ENG -f      (Ctrl-C stops watching, not the bot)"
say "Stop both : ./stop.sh"
