#!/usr/bin/env bash
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Kill server processen
pkill -f "python -m sales_copilot" 2>/dev/null && echo "Server gestopt" || echo "Geen server actief"

# Kill afplay als die nog draait (van mp3-tests)
pkill -f "afplay" 2>/dev/null && echo "afplay gestopt" || true

# Cleanup pid file
rm -f /tmp/sales-copilot-server.pid

# Notificatie
osascript -e 'display notification "Server en audio-processen gestopt." with title "OnCue Stopped" sound name "Pop"'

echo ""
echo "OnCue is gestopt."
echo "Druk Enter om dit venster te sluiten..."
read
