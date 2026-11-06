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

# ── auto-update: pull the latest code before starting (best-effort; never blocks) ──
# Fast-forward only, and no credential prompt — so it can't hang or create merge messes.
# If you have edited a tracked file (e.g. symbols.json) it just skips with a warning and
# starts with your current code. Skip the pull entirely with:  NO_PULL=1 ./start.sh
if [ "${NO_PULL:-}" != "1" ] && git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    say "Checking for updates (git pull)..."
    _req_before="$( (sha1sum "$REPO_DIR/requirements_fyers.txt" 2>/dev/null) || true )"
    if GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" pull --ff-only --quiet; then
        ok "code is up to date"
    else
        warn "update skipped (local edits or no fast-forward) — starting with the CURRENT code."
        warn "  if you edited symbols.json on the server, that's expected and fine."
    fi
    _req_after="$( (sha1sum "$REPO_DIR/requirements_fyers.txt" 2>/dev/null) || true )"
    if [ "$_req_before" != "$_req_after" ] && [ -x "$VENV/bin/python" ]; then
        say "dependencies changed — refreshing libraries..."
        "$VENV/bin/python" -m pip install -q -r "$REPO_DIR/requirements_fyers.txt" \
            || warn "library refresh failed — run 'bash setup.sh' if the engine won't start."
    fi
fi

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
