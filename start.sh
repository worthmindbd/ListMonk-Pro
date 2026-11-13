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

# PIDs LISTENING on the port. Only listeners count: a plain `lsof -i :PORT`
# also matches clients connected to it (e.g. a browser tab still open on the
# old dashboard), which made the script think the port was taken by the browser.
listeners() {
    lsof -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

# Always run on the same port: stop whatever is listening there first
# (a previous instance, its --reload parent, or anything else).
pids=$(listeners "$PORT")
if [ -n "$pids" ]; then
    echo "⚠️ Port $PORT is in use. Stopping:"
    for pid in $pids; do
        echo "  PID $pid: $(ps -o command= -p "$pid" 2>/dev/null | cut -c1-120 || echo '?')"
    done
    kill $pids 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        [ -z "$(listeners "$PORT")" ] && break
        sleep 1
    done
    pids=$(listeners "$PORT")
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
        sleep 1
    fi
    if [ -n "$(listeners "$PORT")" ]; then
        echo "Error: could not free port $PORT (try: sudo lsof -iTCP:$PORT -sTCP:LISTEN)."
        exit 1
    fi
    echo "✅ Port $PORT freed."
fi

# Activate virtual environment
source venv/bin/activate

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
