#!/usr/bin/env bash
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Verifieer venv
if [ ! -x ".venv/bin/python" ]; then
  osascript -e 'display alert "OnCue" message "Venv niet gevonden in .venv/. Run scripts/setup.sh eerst."'
  exit 1
fi

# Verifieer Vertex credentials
if [ ! -f "$HOME/.config/gcloud-keys/sales-copilot-vertex.json" ]; then
  osascript -e 'display notification "Vertex service account JSON niet gevonden. Server probeert .env config." with title "OnCue"'
fi

# Stop oude instance als die nog draait
pkill -f "python -m sales_copilot" 2>/dev/null && sleep 1 || true

# The launcher manages its own browser tabs below, so tell the server not to
# open one itself (main() opens the front door by default for direct runs).
export SALES_COPILOT_NO_BROWSER=1

# Start server in background, log naar data/logs/launcher-server.log
mkdir -p data/logs
nohup .venv/bin/python -m sales_copilot > data/logs/launcher-server.log 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > /tmp/sales-copilot-server.pid

# Wacht max 30s op server ready (port 8760)
for i in $(seq 1 30); do
  if lsof -i :8760 > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Verifieer port 8760 actief
if ! lsof -i :8760 > /dev/null 2>&1; then
  osascript -e 'display alert "OnCue" message "Server failed to start. Check data/logs/launcher-server.log."'
  exit 1
fi

# Decide the front door: the first-run wizard until the app is configured
# (provider key + license present in .env), otherwise the dashboard. One rule,
# resolved from .env by the same helper main() uses.
FRONT_PATH="$(.venv/bin/python -c 'from sales_copilot.wizard.front_door import first_run_path; print(first_run_path())' 2>/dev/null || echo /dashboard/wizard/)"

# Open browser tabs
open "http://localhost:8760${FRONT_PATH}"

# The presentation is a second (screen-shared) screen — only relevant once the
# app is configured and you are heading into a call, not during first-run setup.
if [ "$FRONT_PATH" = "/dashboard" ]; then
  sleep 1
  open "http://localhost:8760/presentation"
  osascript -e 'display notification "Dashboard + Presentation open. Klaar voor call." with title "OnCue Ready" sound name "Glass"'
else
  osascript -e 'display notification "First-run wizard geopend. Vul je sleutels in om te starten." with title "OnCue Setup" sound name "Glass"'
fi

echo ""
echo "============================================="
echo "OnCue is gestart."
echo "Server PID: $SERVER_PID"
echo "Front door: http://localhost:8760${FRONT_PATH}"
echo "Dashboard: http://localhost:8760/dashboard"
echo "Presentation: http://localhost:8760/presentation"
echo "Stop via: rode Stop-knop rechtsboven in het dashboard"
echo "============================================="
echo ""
echo "Venster sluit automatisch na 30 seconden (of druk Enter)..."
read -t 30
