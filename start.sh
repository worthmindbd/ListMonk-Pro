#!/bin/bash
set -euo pipefail

echo "Starting ListMonk Dashboard..."

if [ ! -d "venv" ]; then
    echo "Error: virtual environment not found. Run 'python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt' first."
    exit 1
fi

# Check if port 8000 is already in use, kill it if so
if lsof -i :8000 > /dev/null 2>&1; then
    echo "⚠️ Port 8000 is already in use. Stopping existing process..."
    kill $(lsof -t -i :8000) 2>/dev/null
    sleep 1
    # Force kill if still running
    if lsof -i :8000 > /dev/null 2>&1; then
        kill -9 $(lsof -t -i :8000) 2>/dev/null
        sleep 1
    fi
    echo "✅ Previous process stopped."
fi

# Activate virtual environment
source venv/bin/activate

# Start the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
