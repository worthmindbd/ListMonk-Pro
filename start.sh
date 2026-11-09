#!/bin/bash
set -euo pipefail

echo "Starting ListMonk Dashboard..."

if [ ! -d "venv" ]; then
    echo "Error: virtual environment not found. Run 'python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt' first."
    exit 1
fi

# Port for the dev server (override with LISTMONK_PORT). Production ports come
# from each deployment's own .env / docker-compose; this only affects ./start.sh.
PORT="${LISTMONK_PORT:-8000}"
PORT_TRIES=10

# True when the PID is a previous instance of this app's uvicorn. Verify the
# listener is actually ours before stopping it: previously this script killed
# whatever happened to hold the port, which could be an unrelated service on
# a shared machine.
is_our_app() {
    local cmd
    cmd=$(ps -o command= -p "$1" 2>/dev/null || true)
    case "$cmd" in
        *uvicorn*app.main:app*|*"app.main:app"*) return 0 ;;
    esac
    return 1
}

# Pick a port: the configured one, then the next PORT_TRIES-1. A previous
# instance of this app is stopped; a port held by anything else (a browser's
# local service, another dev server) is never killed — the holder is reported
# and the next port is tried instead.
chosen=""
for (( try=0; try<PORT_TRIES; try++ )); do
    candidate=$(( PORT + try ))
    pids=$(lsof -t -i :"$candidate" 2>/dev/null || true)
    if [ -z "$pids" ]; then
        chosen=$candidate
        break
    fi

    ours=()
    foreign=()
    for pid in $pids; do
        if is_our_app "$pid"; then
            ours+=("$pid")
        else
            foreign+=("$pid")
        fi
    done

    if [ ${#ours[@]} -gt 0 ]; then
        echo "⚠️ Port $candidate is in use by a previous instance. Stopping it..."
        kill "${ours[@]}" 2>/dev/null || true
        sleep 1
        if lsof -i :"$candidate" > /dev/null 2>&1; then
            kill -9 "${ours[@]}" 2>/dev/null || true
            sleep 1
        fi
        echo "✅ Previous process stopped."
        chosen=$candidate
        break
    fi

    echo "⚠️ Port $candidate is held by a process that is not this app (left alone):"
    for pid in "${foreign[@]}"; do
        echo "  PID $pid: $(ps -o command= -p "$pid" 2>/dev/null || echo '?')"
    done
done

if [ -z "$chosen" ]; then
    echo "Error: no free port found in $PORT–$((PORT + PORT_TRIES - 1))."
    echo "Stop the holders yourself or pick another base port: LISTMONK_PORT=<port> ./start.sh"
    exit 1
fi

if [ "$chosen" -ne "$PORT" ]; then
    echo "➡️  Port $PORT was busy — starting on port $chosen instead."
    echo "    Open the dashboard at http://localhost:$chosen"
fi

# Activate virtual environment
source venv/bin/activate

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port "$chosen" --reload
