#!/bin/bash

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="${WEB_SERVER_TMP_DIR:-$SCRIPT_DIR/tmp}"
PID_FILE="${WEB_SERVER_PID_FILE:-$TMP_DIR/server.pid}"
LOCK_DIR="${WEB_SERVER_LOCK_DIR:-$TMP_DIR/server.lock}"
LOG_FILE="${WEB_SERVER_LOG_FILE:-$SCRIPT_DIR/server.log}"
HEALTH_URL="${WEB_SERVER_HEALTH_URL:-http://127.0.0.1:9222}"
CDP_PORT="${WEB_SERVER_CDP_PORT:-9223}"
START_TIMEOUT="${WEB_SERVER_START_TIMEOUT_SECONDS:-120}"
ACTION="start"
HEADLESS=false

if [[ $# -gt 0 && "$1" != --* ]]; then
    ACTION="$1"
    shift
fi
while [[ $# -gt 0 ]]; do
    case "$1" in
        --headless) HEADLESS=true ;;
        *) echo "Unknown parameter: $1" >&2; exit 2 ;;
    esac
    shift
done

mkdir -p "$TMP_DIR"

health_ready() {
    [[ "${WEB_SERVER_SKIP_HEALTH:-0}" == "1" ]] || curl --noproxy '*' -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1
}

owned_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid command
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    [[ "${WEB_SERVER_ALLOW_TEST_COMMAND:-0}" == "1" ]] && return 0
    command="$(ps -p "$pid" -o command= 2>/dev/null)"
    [[ "$command" == *"scripts/start-server.ts"* ]]
}

acquire_lock() {
    local attempts=0
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        attempts=$((attempts + 1))
        [[ $attempts -lt 100 ]] || { echo "Browser lifecycle lock timed out" >&2; return 1; }
        sleep 0.1
    done
    trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
}

start_server() {
    acquire_lock || return 1
    if owned_pid && health_ready; then
        echo "Browser server already running (PID $(cat "$PID_FILE"))"
        return 0
    fi
    if [[ "${WEB_SERVER_SKIP_HEALTH:-0}" != "1" ]] && health_ready; then
        echo "Browser server is healthy but owned by another launcher" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    if lsof -ti:"$CDP_PORT" >/dev/null 2>&1; then
        echo "CDP port $CDP_PORT is occupied by an unowned process; refusing to kill it" >&2
        return 1
    fi
    if [[ ! -d "$SCRIPT_DIR/node_modules" && -z "${WEB_SERVER_COMMAND:-}" ]]; then
        (cd "$SCRIPT_DIR" && npm install) || return 1
    fi
    local command="${WEB_SERVER_COMMAND:-exec npx tsx scripts/start-server.ts}"
    export HEADLESS="$HEADLESS"
    python3 "$SCRIPT_DIR/scripts/launch-server.py" "$SCRIPT_DIR" "$LOG_FILE" "$command" >"$PID_FILE" || return 1
    local waited=0
    while [[ $waited -lt $START_TIMEOUT ]]; do
        if owned_pid && health_ready; then
            echo "Browser server started (PID $(cat "$PID_FILE"))"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "Browser server failed to become ready" >&2
    return 1
}

stop_server() {
    acquire_lock || return 1
    if ! owned_pid; then
        echo "No owned browser server is running" >&2
        return 1
    fi
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid"
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 10 ]]; do
        sleep 1
        waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "Owned browser server did not stop cleanly (PID $pid)" >&2
        return 1
    fi
    rm -f "$PID_FILE"
    echo "Browser server stopped"
}

case "$ACTION" in
    start) start_server ;;
    status)
        if owned_pid && health_ready; then
            echo "Browser server is running (PID $(cat "$PID_FILE"))"
        else
            echo "Browser server is not running" >&2
            exit 1
        fi
        ;;
    stop) stop_server ;;
    restart)
        if owned_pid; then stop_server || exit 1; fi
        trap - EXIT
        rmdir "$LOCK_DIR" 2>/dev/null || true
        start_server
        ;;
    *) echo "Usage: $0 {start|status|stop|restart} [--headless]" >&2; exit 2 ;;
esac
