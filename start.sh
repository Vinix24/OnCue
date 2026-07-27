#!/bin/bash
# OnCue — Quick Start
# Starts copilot backend.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "No .venv found. Run: python3 -m venv .venv && pip install -e ."
    exit 1
fi

echo "Checking audio devices..."
python scripts/verify_audio.py
if [ $? -ne 0 ]; then
    echo "WARNING: Audio issues detected. Continuing in fallback mode."
fi

# Kill any leftover processes on our ports
for PORT in 8760 8761; do
    lsof -ti:$PORT 2>/dev/null | xargs kill -9 2>/dev/null
done
sleep 1

# Read config from .env (grep + defaults)
ENGINE=$(grep -m1 '^TRANSCRIPTION_ENGINE=' .env 2>/dev/null | cut -d= -f2)
ENGINE=${ENGINE:-direct}
echo "TRANSCRIPTION_ENGINE=$ENGINE"

# Open dashboard in Chrome via the server
(sleep 3 && open -a "Google Chrome" "http://localhost:8760/dashboard/") &

echo ""
echo "Starting OnCue..."
echo "  Dashboard: http://localhost:8760/dashboard/"
echo "  Transcription engine: $ENGINE"
echo "Press Ctrl+C to stop everything."
echo ""

# Cleanup function
cleanup() {
    echo "Stopping..."
    echo "Stopped."
}
trap cleanup EXIT INT TERM

# Start copilot (foreground)
python -m sales_copilot
