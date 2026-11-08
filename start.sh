#!/usr/bin/env bash
# start.sh — start BOTH the health monitor and the dashboard (background services).
set -euo pipefail
source "$(dirname "$0")/scripts/common.sh"
command -v systemctl >/dev/null 2>&1 || die "no systemd — use ./start_monitor.sh and ./start_dashboard.sh"
[ -f /etc/systemd/system/ballom-health.service ] || die "not installed — run:  bash setup.sh"

# ── auto-update: pull the latest code before starting (best-effort; never blocks) ──
# Fast-forward only + no credential prompt, so it can't hang or make merge messes. If you have
# local edits (e.g. ip_list.txt) it just skips with a warning and starts the CURRENT code.
# Skip the pull entirely with:  NO_PULL=1 ./start.sh
if [ "${NO_PULL:-}" != "1" ] && git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    say "Checking for updates (git pull)..."
    _req_before="$( (sha1sum "$REPO_DIR/requirements.txt" 2>/dev/null) || true )"
    if GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" pull --ff-only --quiet; then
        ok "code is up to date"
    else
        warn "update skipped (local edits or no fast-forward) — starting with the CURRENT code."
    fi
    _req_after="$( (sha1sum "$REPO_DIR/requirements.txt" 2>/dev/null) || true )"
    if [ "$_req_before" != "$_req_after" ] && [ -x "$VENV/bin/python" ]; then
        say "dependencies changed — refreshing libraries..."
        "$VENV/bin/python" -m pip install -q -r "$REPO_DIR/requirements.txt" \
            || warn "library refresh failed — run 'bash setup.sh' if it won't start."
    fi
fi

say "Starting health monitor + dashboard..."
$SUDO systemctl start ballom-health ballom-health-dash
sleep 1
for u in ballom-health ballom-health-dash; do
    if $SUDO systemctl is-active --quiet "$u"; then ok "$u running"; else warn "$u failed — journalctl -u $u -n 30"; fi
done
say "Dashboard: http://<server-ip>:$PORT   (stop both: ./stop.sh)"
