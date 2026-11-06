#!/usr/bin/env bash
# =============================================================================
#  stop.sh  —  stop BOTH the dashboard and the trading engine, in one command.
#
#        ./stop.sh
#
#  Start them again with:  ./start.sh
#  (This stops them for now. They still come back on reboot because they are
#   "enabled". To stop that too:  sudo systemctl disable BALLOM-<name> and -dash.)
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"

ENG="ballom-$INSTANCE"
DASH="ballom-$INSTANCE-dash"

command -v systemctl >/dev/null 2>&1 \
    || die "no systemd here — if you used ./start_app.sh, stop it with Ctrl-C in its terminal."

say "Stopping dashboard + engine for '$INSTANCE'..."
$SUDO systemctl stop "$DASH" "$ENG" || true
for u in "$ENG" "$DASH"; do
    if $SUDO systemctl is-active --quiet "$u"; then
        warn "$u is still active — try again, or:  sudo systemctl stop $u"
    else
        ok "$u stopped"
    fi
done
say "Start again with:  ./start.sh"
