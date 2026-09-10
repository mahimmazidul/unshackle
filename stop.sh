#!/usr/bin/env sh
# Stop the rootless unshackle serve instance started by start.sh (no systemd required).
#
# Usage:
#   ./stop.sh

set -eu

LOG_DIR="${UNSHACKLE_LOG_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/unshackle}"
PID_FILE="$LOG_DIR/serve.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No pid file at $PID_FILE - is unshackle serve running?" >&2
    exit 1
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    i=0
    while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 30 ]; do
        sleep 1; i=$((i + 1))
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "Process $PID did not stop, sending SIGKILL." >&2
        kill -9 "$PID" 2>/dev/null || true
    fi
    echo "Stopped unshackle serve (pid $PID)."
else
    echo "Pid $PID is not running; removing stale pid file."
fi

rm -f "$PID_FILE"
