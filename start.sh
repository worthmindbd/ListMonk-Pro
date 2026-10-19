#!/bin/bash
set -euo pipefail

echo "Starting ListMonk Dashboard..."

if [ ! -d "venv" ]; then
    echo "Error: virtual environment not found. Run 'python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt' first."
    exit 1
fi

# Stop a previous instance of *this* app on port 8000. Verify the listener is
# actually our uvicorn process first: previously this killed whatever happened
# to hold the port, which could be an unrelated service on a shared machine.
stop_own_listener() {
    local pids
    pids=$(lsof -t -i :8000 2>/dev/null || true)
    [ -z "$pids" ] && return 0

    local ours=()
    for pid in $pids; do
        local cmd
        cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
        case "$cmd" in
            *uvicorn*app.main:app*|*"app.main:app"*) ours+=("$pid") ;;
        esac
    done

    if [ ${#ours[@]} -eq 0 ]; then
        echo "Error: port 8000 is held by a process that is not this app:"
        for pid in $pids; do
            echo "  PID $pid: $(ps -o command= -p "$pid" 2>/dev/null || echo '?')"
        done
        echo "Refusing to kill it. Stop it manually or use a different port."
        exit 1
    fi

    echo "⚠️ Port 8000 is in use by a previous instance. Stopping it..."
    kill "${ours[@]}" 2>/dev/null || true
    sleep 1
    if lsof -i :8000 > /dev/null 2>&1; then
        kill -9 "${ours[@]}" 2>/dev/null || true
        sleep 1
    fi
    echo "✅ Previous process stopped."
}

stop_own_listener

# Activate virtual environment
source venv/bin/activate

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
