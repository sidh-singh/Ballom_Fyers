#!/usr/bin/env bash
# =============================================================================
#  scripts/common.sh  —  shared config + helpers.
#  Sourced by setup.sh / start_app.sh / start_dashboard.sh / install-service.sh.
#  (You never run this file directly.)
#
#  MULTI-BOT / MULTI-BROKER SAFE
#  -----------------------------
#  Everything below is namespaced by $INSTANCE, so you can run several bots on ONE
#  server (fyers live + fyers demo, or a future broker) without them touching each
#  other:
#     * data (state / token / cache) and the Python venv live in
#          ~/ballom-trading/<INSTANCE>/          (NOT inside the repo)
#     * each instance gets its own dashboard PORT and its own systemd service name.
#
#  INSTANCE defaults to the git branch with the "linux_" prefix removed
#  (linux_fyers_live -> fyers_live). Override ANY of these by exporting them first:
#     INSTANCE=myname   PORT=8051   BALLOM_DIR=/data/x   PYTHON=/path/to/python3.11
# =============================================================================
# NOTE: we do NOT 'set -e' here — each caller decides. This file only defines things.

# ---- where the repo lives (this file is <repo>/scripts/common.sh) ----
COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"

# ---- INSTANCE: which bot am I? ----
if [ -z "${INSTANCE:-}" ]; then
    if [ -f "$REPO_DIR/instance.conf" ]; then
        INSTANCE="$(tr -d '[:space:]' < "$REPO_DIR/instance.conf")"
    else
        _branch="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        _branch="${_branch#linux_}"
        INSTANCE="${_branch:-$(basename "$REPO_DIR")}"
    fi
fi

# ---- per-instance data root + venv (both OUTSIDE the repo) ----
TRADING_HOME="${TRADING_HOME:-$HOME/ballom-trading}"
INSTANCE_DIR="$TRADING_HOME/$INSTANCE"
export BALLOM_DIR="${BALLOM_DIR:-$INSTANCE_DIR}"     # constants.py reads this env var
VENV="${VENV:-$INSTANCE_DIR/venv}"

# ---- dashboard port (preset per instance so several bots never clash; override with PORT=) ----
# PORT REGISTRY - each instance has a fixed port so running bots never collide.
# >>> ADDING A NEW BROKER: give its branch the NEXT free port. Add a line in the case below,
#     in sequence (e.g.  zerodha_demo) _default_port=8056 ;;), then bump _NEXT_FREE_PORT by 1.
#     Full steps: LINUX_SETUP.md -> "Adding a NEW broker". Never reuse 8050-8055.
_NEXT_FREE_PORT=8056
case "$INSTANCE" in
    fyers_demo)         _default_port=8050 ;;
    fyers_live)         _default_port=8051 ;;
    fyers_netting_demo) _default_port=8052 ;;
    fyers_netting_live) _default_port=8053 ;;
    fyers_hedging_demo) _default_port=8054 ;;
    fyers_hedging_live) _default_port=8055 ;;
    # add new brokers here, each with the next port, kept in sequence
    *)                  _default_port="$_NEXT_FREE_PORT" ;;   # unregistered -> next free (register it)
esac
PORT="${PORT:-$_default_port}"

# ---- entrypoints (a future broker may override ENGINE) ----
ENGINE="${ENGINE:-application.py}"
DASHBOARD="${DASHBOARD:-dashboard.py}"

# ---- is this a live (real-order) instance? (name ends in 'live') ----
case "$INSTANCE" in
    *live) IS_LIVE=1 ;;
    *)     IS_LIVE=0 ;;
esac

# ---- simple coloured logging (ASCII, safe on any server) ----
say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m OK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- sudo helper (empty when already root) ----
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

# ---- make sure a user-local bin (where uv installs) is on PATH ----
if [ -d "$HOME/.local/bin" ]; then
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
fi

# ---- resolve a Python 3.11 interpreter: $PYTHON -> python3.11 -> uv ----
# fyers-apiv3's aiohttp pin has no 3.13 wheel, so 3.11 is required. Debian 12 has it
# via apt; Debian 13 (system python = 3.13) does not, so we fall back to uv's 3.11.
resolve_python() {
    if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
    if command -v python3.11 >/dev/null 2>&1; then echo "python3.11"; return 0; fi
    if command -v uv >/dev/null 2>&1; then
        uv python find 3.11 2>/dev/null && return 0
        uv python install 3.11 1>&2 || true
        uv python find 3.11 2>/dev/null && return 0
    fi
    return 1
}

# ---- ensure your config exists (created once from the tracked template) ----
# symbols.json is git-ignored, so `git pull` / updates never touch your edits. The first
# time any script runs, we copy symbols.example.json -> symbols.json if it's missing.
if [ ! -f "$REPO_DIR/symbols.json" ] && [ -f "$REPO_DIR/symbols.example.json" ]; then
    if cp "$REPO_DIR/symbols.example.json" "$REPO_DIR/symbols.json" 2>/dev/null; then
        warn "created symbols.json from the template — edit it to set your symbols & units."
    fi
fi
