#!/usr/bin/env sh
# Rootless launcher for the unshackle REST/CDM server (no systemd required).
#
# Usage:
#   ./start.sh                 # start in the background (defaults: 127.0.0.1:8786)
#   ./start.sh restart         # restart if already running
#   UNSHACKLE_HOST=0.0.0.0 UNSHACKLE_PORT=9000 ./start.sh   # custom bind
#
# Extra CLI flags for `unshackle serve` go in UNSHACKLE_SERVE_ARGS, e.g.:
#   UNSHACKLE_SERVE_ARGS="--api-only --no-widevine" ./start.sh
#
# The original package has no systemd units; this nohup + PID-file pair is the
# rootless equivalent. On hosts that provide them, `tmux`/`screen`/`pm2` work too:
#   tmux new -ds unshackle 'unshackle serve'
#   pm2 start "unshackle serve" --name unshackle

set -eu

HOST="${UNSHACKLE_HOST:-127.0.0.1}"
PORT="${UNSHACKLE_PORT:-8786}"
LOG_DIR="${UNSHACKLE_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/unshackle}"
PID_FILE="$LOG_DIR/serve.pid"
LOG_FILE="$LOG_DIR/serve.log"

mkdir -p "$LOG_DIR"

# Locate the unshackle entry point (works for `uv tool install` and `uv run` clones).
if command -v unshackle >/dev/null 2>&1; then
    CMD="unshackle"
elif command -v uv >/dev/null 2>&1; then
    CMD="uv run unshackle"
else
    echo "unshackle: command not found. Install it (see ROOTLESS_SETUP.md) or add it to your PATH." >&2
    exit 1
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    PID="$(cat "$PID_FILE")"
    if [ "${1:-}" = "restart" ]; then
        echo "Stopping running instance (pid $PID)..." >&2
        kill "$PID" 2>/dev/null || true
        i=0
        while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 30 ]; do
            sleep 1; i=$((i + 1))
        done
    else
        echo "unshackle serve is already running (pid $PID). Use '$0 restart' or ./stop.sh." >&2
        exit 1
    fi
fi

# shellcheck disable=SC2086
nohup $CMD serve -h "$HOST" -p "$PORT" $UNSHACKLE_SERVE_ARGS >>"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" >"$PID_FILE"
echo "Started unshackle serve (pid $PID) on http://$HOST:$PORT"
echo "Logs:  $LOG_FILE"
echo "Stop:  ./stop.sh"
