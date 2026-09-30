#!/usr/bin/env bash
# Single source of truth for launching OnCue, in either mode. Both
# start-oncue.command and the "Start OnCue.app" AppleScript applet
# (scripts/launcher/build_app.sh) delegate here instead of keeping their own
# copy of the launch logic.
#
# Usage:
#   bash scripts/launcher/oncue-launch.sh dashboard   # full stack
#   bash scripts/launcher/oncue-launch.sh record      # recording only, port 8780
#
# Repo-root resolution lets this run from a copy of the app that lives outside
# the repo: $ONCUE_REPO overrides everything (and must be valid if set), then
# the path relative to this script, then a path remembered in
# ~/.oncue/repo-path, then an interactive folder picker that remembers its
# answer for next time.
#
# Debugging port resolution without starting anything or touching any
# process: set ONCUE_LAUNCH_DRY_RUN=1. The script prints MODE/REPO_ROOT/PORT/
# OTHER_PORT and exits before the port-ownership check.
set -euo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_PATH_FILE="$HOME/.oncue/repo-path"
RECORD_PORT=8780

MODE="${1:-}"
case "$MODE" in
    dashboard|record) ;;
    *)
        echo "Onbekende modus: '${MODE}'. Gebruik 'dashboard' of 'record'." >&2
        exit 1
        ;;
esac

_has_markers() {
    local dir="$1"
    [[ -f "$dir/src/sales_copilot/__main__.py" && -f "$dir/scripts/record_server.py" ]]
}

resolve_repo_root() {
    local candidate

    # An explicit override is authoritative: if it is set but invalid, fail
    # loudly instead of silently falling back to a different repo.
    if [[ -n "${ONCUE_REPO:-}" ]]; then
        candidate="${ONCUE_REPO%/}"
        if _has_markers "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
        echo "ONCUE_REPO ($candidate) is geen geldige OnCue-installatie (mist src/sales_copilot/__main__.py of scripts/record_server.py)." >&2
        return 1
    fi

    candidate="$(cd "$LAUNCHER_DIR/../.." && pwd)"
    if _has_markers "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
    fi

    if [[ -f "$REPO_PATH_FILE" ]]; then
        candidate="$(<"$REPO_PATH_FILE")"
        candidate="${candidate%/}"
        if _has_markers "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
        echo "Opgeslagen pad ($candidate, uit $REPO_PATH_FILE) is geen geldige OnCue-installatie meer." >&2
    fi

    candidate="$(osascript -e 'POSIX path of (choose folder with prompt "Kies de OnCue-projectmap")' 2>/dev/null)" || true
    candidate="${candidate%/}"
    if [[ -z "$candidate" ]]; then
        echo "Geen OnCue-map geselecteerd." >&2
        return 1
    fi
    if ! _has_markers "$candidate"; then
        echo "De gekozen map ($candidate) is geen geldige OnCue-installatie (mist src/sales_copilot/__main__.py of scripts/record_server.py)." >&2
        return 1
    fi
    mkdir -p "$(dirname "$REPO_PATH_FILE")"
    printf '%s\n' "$candidate" > "$REPO_PATH_FILE"
    printf '%s\n' "$candidate"
    return 0
}

REPO_ROOT="$(resolve_repo_root)" || exit 1

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "OnCue gevonden op $REPO_ROOT, maar de virtualenv ontbreekt. Run 'scripts/setup.sh' in die map en probeer opnieuw." >&2
    exit 1
fi

cd "$REPO_ROOT"

# Canonical (symlink-resolved) repo path, used to compare against process cwds
# reported by lsof — those are already resolved, so REPO_ROOT (a logical `cd
# && pwd` path) must be normalized the same way before comparing.
_canon_dir() {
    (cd "$1" 2>/dev/null && pwd -P) 2>/dev/null || true
}

REPO_ROOT_CANON="$(_canon_dir "$REPO_ROOT")"

# The dashboard port is application config, not launcher config: the app
# reads WS_HUB_PORT from .env (src/sales_copilot/core/config.py), so the
# launcher must resolve it the same way instead of assuming 8760. Only the
# single matching line is read — the file is never sourced, since it can
# contain secrets that must not end up in this script's (or the browser
# open's) environment.
resolve_dashboard_port() {
    local env_file="$REPO_ROOT/.env" line value
    if [[ -f "$env_file" ]]; then
        line="$(grep -E '^WS_HUB_PORT=' "$env_file" 2>/dev/null | tail -n1 || true)"
        if [[ -n "$line" ]]; then
            value="${line#WS_HUB_PORT=}"
            value="${value%%#*}"          # strip inline comment
            value="${value%\"}"; value="${value#\"}"
            value="${value%\'}"; value="${value#\'}"
            value="$(printf '%s' "$value" | tr -d '[:space:]')"
            if [[ "$value" =~ ^[0-9]+$ ]]; then
                printf '%s\n' "$value"
                return 0
            fi
        fi
    fi
    printf '8760\n'
}

DASHBOARD_PORT="$(resolve_dashboard_port)"

case "$MODE" in
    dashboard)
        PORT="$DASHBOARD_PORT"
        OTHER_MODE="record"
        OTHER_PORT="$RECORD_PORT"
        ;;
    record)
        PORT="$RECORD_PORT"
        OTHER_MODE="dashboard"
        OTHER_PORT="$DASHBOARD_PORT"
        ;;
esac

if [[ "${ONCUE_LAUNCH_DRY_RUN:-0}" == "1" ]]; then
    echo "MODE=$MODE"
    echo "REPO_ROOT=$REPO_ROOT"
    echo "PORT=$PORT"
    echo "OTHER_PORT=$OTHER_PORT"
    exit 0
fi

# Two servers on different ports can coexist. Only touch the port we are
# starting on; if the other mode is already running, leave it alone and just
# notify.
if lsof -i ":$OTHER_PORT" >/dev/null 2>&1; then
    osascript -e "display notification \"OnCue $OTHER_MODE draait al op poort $OTHER_PORT — dat laat ik staan.\" with title \"OnCue\"" >/dev/null 2>&1 || true
fi

# Working directory of a PID, as reported by lsof (already symlink-resolved).
_pid_cwd() {
    local pid="$1"
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2); exit}'
}

_pid_owns_port() {
    lsof -a -p "$1" -i ":$2" >/dev/null 2>&1
}

# Never kill by pattern-matching a command line: that matches any process on
# the machine whose args happen to contain the string, including a live
# session in a different clone of this repo. Establish ownership by PID via
# the port itself, and only ever kill PIDs discovered that way.
PORT_PIDS=()
while IFS= read -r _port_pid; do
    [[ -n "$_port_pid" ]] && PORT_PIDS+=("$_port_pid")
done < <(lsof -ti ":$PORT" 2>/dev/null || true)

if [[ "${#PORT_PIDS[@]}" -gt 0 ]]; then
    SAME_REPO_PIDS=()
    FOREIGN=0
    for pid in "${PORT_PIDS[@]}"; do
        owner_cwd="$(_pid_cwd "$pid")"
        if [[ -z "$owner_cwd" ]]; then
            FOREIGN=1
            echo "Kan de working directory van PID $pid (poort $PORT) niet vaststellen — ik raak dit proces niet aan." >&2
            continue
        fi
        if [[ "$(_canon_dir "$owner_cwd")" == "$REPO_ROOT_CANON" ]]; then
            SAME_REPO_PIDS+=("$pid")
        else
            FOREIGN=1
            echo "Poort $PORT is al in gebruik door PID $pid vanuit een andere map ($owner_cwd) — ik raak dit proces niet aan." >&2
        fi
    done

    if [[ "$FOREIGN" -eq 1 ]]; then
        echo "Los dit handmatig op (bijv. 'kill <pid>') en start OnCue opnieuw." >&2
        exit 1
    fi

    DIALOG_MSG="OnCue $MODE draait al op poort $PORT, vanuit deze map. Wil je die instantie herstarten?"
    CHOICE="$(osascript -e "display dialog \"$DIALOG_MSG\" buttons {\"Annuleer\", \"Herstarten\"} default button \"Annuleer\" with title \"OnCue\"" 2>/dev/null | sed -n 's/^button returned://p')" || true

    if [[ "$CHOICE" != "Herstarten" ]]; then
        exit 0
    fi

    for pid in "${SAME_REPO_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done

    FREED=0
    for _ in $(seq 1 10); do
        if ! lsof -i ":$PORT" >/dev/null 2>&1; then
            FREED=1
            break
        fi
        sleep 1
    done

    if [[ "$FREED" -ne 1 ]]; then
        echo "Poort $PORT is niet vrijgekomen na herstart-poging (PID(s): ${SAME_REPO_PIDS[*]}). Los dit handmatig op." >&2
        exit 1
    fi
fi

mkdir -p data/logs
LOG_FILE="data/logs/launcher-${MODE}.log"
PID_FILE="/tmp/oncue-launcher-${MODE}.pid"

if [[ "$MODE" == "dashboard" ]]; then
    # The launcher manages its own browser tabs below, so tell the server not
    # to open one itself (main() opens the front door by default for direct
    # runs).
    export SALES_COPILOT_NO_BROWSER=1
    nohup .venv/bin/python -m sales_copilot > "$LOG_FILE" 2>&1 &
else
    nohup .venv/bin/python scripts/record_server.py --port "$RECORD_PORT" > "$LOG_FILE" 2>&1 &
fi
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# Readiness means our own server owns the port — not merely that something
# does. A stale/foreign listener on $PORT would otherwise make this loop
# report success for a server that never started (see launcher findings).
_server_owns_port() {
    local port="$1"

    if _pid_owns_port "$SERVER_PID" "$port"; then
        return 0
    fi

    # uvicorn runs single-process here (no `workers=`), but check direct
    # children too in case that ever changes.
    local child
    for child in $(pgrep -P "$SERVER_PID" 2>/dev/null || true); do
        if _pid_owns_port "$child" "$port"; then
            return 0
        fi
    done

    # Second-best signal if the PID/port intersection above is ever
    # inconclusive: does whoever holds the port share our repo's cwd?
    local owner_pid owner_cwd
    for owner_pid in $(lsof -ti ":$port" 2>/dev/null || true); do
        owner_cwd="$(_pid_cwd "$owner_pid")"
        if [[ -n "$owner_cwd" && "$(_canon_dir "$owner_cwd")" == "$REPO_ROOT_CANON" ]]; then
            return 0
        fi
    done

    return 1
}

READY=0
for _ in $(seq 1 30); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "OnCue $MODE server is direct gestopt. Log: $REPO_ROOT/$LOG_FILE" >&2
        exit 1
    fi
    if _server_owns_port "$PORT"; then
        READY=1
        break
    fi
    sleep 1
done

if [[ "$READY" -ne 1 ]]; then
    echo "OnCue $MODE server startup timed out. Log: $REPO_ROOT/$LOG_FILE" >&2
    exit 1
fi

if [[ "$MODE" == "dashboard" ]]; then
    # Front door: the first-run wizard until the app is configured (provider
    # key + license present in .env), otherwise the dashboard. Same rule
    # main() uses, resolved once here so both launch paths agree.
    FRONT_PATH="$(.venv/bin/python -c 'from sales_copilot.wizard.front_door import first_run_path; print(first_run_path())' 2>/dev/null || echo /dashboard/wizard/)"
    open "http://localhost:${PORT}${FRONT_PATH}"
    if [[ "$FRONT_PATH" == "/dashboard" ]]; then
        sleep 1
        open "http://localhost:${PORT}/presentation"
    fi
else
    open "http://127.0.0.1:${PORT}"
fi

echo "OnCue $MODE gestart (pid $SERVER_PID, poort $PORT). Log: $REPO_ROOT/$LOG_FILE"
