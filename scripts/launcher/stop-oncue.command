#!/usr/bin/env bash
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_ROOT"

# Same defaults as oncue-launch.sh; the environment overrides are test seams.
RECORD_PORT="${ONCUE_RECORD_PORT:-8780}"
PID_DIR="${ONCUE_PID_DIR:-/tmp}"
RECORD_PID_FILE="$PID_DIR/oncue-launcher-record.pid"
BAR_PID_FILE="$PID_DIR/oncue-launcher-bar.pid"

_ask() {
    local answer=""
    printf '%s ' "$1"
    read -r answer || true
    [[ "$answer" == "j" || "$answer" == "J" ]]
}

# A pid from a pid file or /status only counts when it is alive and its
# command line contains the given marker.
_pid_matches() {
    local pid="$1" marker="$2" cmd
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$cmd" == *"$marker"* ]]
}

_pidfile_value() {
    [[ -f "$1" ]] && tr -d '[:space:]' < "$1"
}

# The notetaker route: ask before anything is signalled.
STATUS_JSON="$(curl -s --max-time 2 "http://127.0.0.1:${RECORD_PORT}/status" 2>/dev/null || true)"
PHASE=""
STATUS_PID=""
ELAPSED=""
if [[ -n "$STATUS_JSON" ]]; then
    # The project venv only: a system python3 can open the command line tools
    # installer on a Mac that has none. No venv or a failed parse = no answer.
    VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
    PARSED=""
    [[ -x "$VENV_PYTHON" ]] && PARSED="$(printf '%s' "$STATUS_JSON" | "$VENV_PYTHON" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("phase") or "-", d.get("pid") or "-", int(d.get("elapsed_s") or 0))
except Exception:
    pass
' 2>/dev/null || true)"
    read -r PHASE STATUS_PID ELAPSED <<< "$PARSED"
    [[ "$PHASE" == "-" ]] && PHASE=""
    [[ "$STATUS_PID" == "-" ]] && STATUS_PID=""
fi

case "$PHASE" in
    recording)
        printf 'Er loopt een opname (%02d:%02d).\n' $((ELAPSED / 60)) $((ELAPSED % 60))
        _ask "Opname afronden en OnCue stoppen? [j/N]" || { echo "Niets gestopt."; exit 0; }
        ;;
    transcribing)
        echo "De transcriptie loopt door bij de volgende start."
        ;;
    idle|ready|failed) ;;
    *)
        # Only a live record server makes an unknown status worth a question.
        SERVER_PID="$(_pidfile_value "$RECORD_PID_FILE" || true)"
        if ! _pid_matches "$SERVER_PID" "scripts/record_server.py" && [[ -n "$STATUS_PID" ]]; then
            SERVER_PID="$STATUS_PID"
        fi
        if _pid_matches "$SERVER_PID" "scripts/record_server.py"; then
            echo "De status van de opname is onbekend."
            _ask "Toch stoppen? [j/N]" || { echo "Niets gestopt."; exit 0; }
        fi
        ;;
esac

# Kill server processen
pkill -f "python -m sales_copilot" 2>/dev/null && echo "Server gestopt" || echo "Geen server actief"

# Kill afplay als die nog draait (van mp3-tests)
pkill -f "afplay" 2>/dev/null && echo "afplay gestopt" || true

# Cleanup pid file
rm -f /tmp/sales-copilot-server.pid

# Notetaker server: SIGTERM only, then wait for it to finish the recording.
SERVER_PID="$(_pidfile_value "$RECORD_PID_FILE" || true)"
if ! _pid_matches "$SERVER_PID" "scripts/record_server.py" && [[ -n "$STATUS_PID" ]]; then
    SERVER_PID="$STATUS_PID"
fi
if _pid_matches "$SERVER_PID" "scripts/record_server.py"; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    echo "De opname wordt afgerond (maximaal 60 seconden)..."
    for _ in $(seq 1 60); do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "De server draait na 60 seconden nog. Ik laat hem staan."
    else
        echo "Opname-server gestopt"
        rm -f "$RECORD_PID_FILE"
    fi
else
    kill -0 "$SERVER_PID" 2>/dev/null || rm -f "$RECORD_PID_FILE"
    echo "Geen opname-server actief"
fi

# Recording bar
BAR_PID="$(_pidfile_value "$BAR_PID_FILE" || true)"
if _pid_matches "$BAR_PID" "/OnCueBar"; then
    kill -TERM "$BAR_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$BAR_PID" 2>/dev/null || break
        sleep 0.25
    done
    kill -0 "$BAR_PID" 2>/dev/null || { echo "Opnamebalk gestopt"; rm -f "$BAR_PID_FILE"; }
elif ! kill -0 "$BAR_PID" 2>/dev/null; then
    rm -f "$BAR_PID_FILE"
fi

# Notificatie
osascript -e 'display notification "Server en audio-processen gestopt." with title "OnCue Stopped" sound name "Pop"'

echo ""
echo "OnCue is gestopt."
echo "Druk Enter om dit venster te sluiten..."
read -r || true
