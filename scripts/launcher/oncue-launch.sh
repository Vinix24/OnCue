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
# Seams for tests; the defaults are the real values.
PID_DIR="${ONCUE_PID_DIR:-/tmp}"

# Messages come from the same catalog as the app (config/i18n/<language>.yaml, the
# "launcher:" section) in the language oncue-language.sh reports. Usage:
#   _msg <key> [name=value ...]    # fills the {name} placeholders
# MSG_ROOT is the install the language is read from; until the install is resolved
# it is $ONCUE_REPO when set, else the repo this script lives in.
MSG_ROOT=""
shopt -u patsub_replacement 2>/dev/null || true
_msg() {
    local key="$1" root lang catalog value="" pair name
    shift
    root="${MSG_ROOT:-${ONCUE_REPO:-$(cd "$LAUNCHER_DIR/../.." && pwd)}}"
    lang="$(bash "$LAUNCHER_DIR/oncue-language.sh" "$root")"
    for catalog in "$root/config/i18n/$lang.yaml" "$LAUNCHER_DIR/../../config/i18n/$lang.yaml" \
        "$root/config/i18n/en.yaml" "$LAUNCHER_DIR/../../config/i18n/en.yaml"; do
        [[ -f "$catalog" ]] || continue
        value="$(awk -v key="$key" '
            /^launcher:/ { in_section = 1; next }
            /^[^ #]/ { in_section = 0 }
            in_section && index($0, "  " key ": \"") == 1 {
                v = substr($0, length(key) + 6); sub(/"[ \t]*$/, "", v); print v; exit
            }' "$catalog")"
        [[ -n "$value" ]] && break
    done
    [[ -n "$value" ]] || value="$key"
    for pair in "$@"; do
        name="${pair%%=*}"
        value="${value//\{$name\}/${pair#*=}}"
    done
    printf '%s' "$value"
}

MODE="${1:-}"
case "$MODE" in
    dashboard|record) ;;
    *)
        echo "$(_msg unknown_mode mode="$MODE")" >&2
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
        echo "$(_msg invalid_install_env path="$candidate")" >&2
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
        echo "$(_msg invalid_saved_path path="$candidate" file="$REPO_PATH_FILE")" >&2
    fi

    candidate="$(osascript -e "POSIX path of (choose folder with prompt \"$(_msg folder_prompt)\")" 2>/dev/null)" || true
    candidate="${candidate%/}"
    if [[ -z "$candidate" ]]; then
        echo "$(_msg no_folder_selected)" >&2
        return 1
    fi
    if ! _has_markers "$candidate"; then
        echo "$(_msg invalid_install_chosen path="$candidate")" >&2
        return 1
    fi
    mkdir -p "$(dirname "$REPO_PATH_FILE")"
    printf '%s\n' "$candidate" > "$REPO_PATH_FILE"
    printf '%s\n' "$candidate"
    return 0
}

REPO_ROOT="$(resolve_repo_root)" || exit 1
MSG_ROOT="$REPO_ROOT"

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
    echo "$(_msg venv_missing path="$REPO_ROOT")" >&2
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
_env_value() {
    local key="$1" env_file="$REPO_ROOT/.env" line value
    if [[ -f "$env_file" ]]; then
        line="$(grep -E "^${key}=" "$env_file" 2>/dev/null | tail -n1 || true)"
        if [[ -n "$line" ]]; then
            value="${line#"${key}="}"
            value="${value%%#*}"          # strip inline comment
            value="${value%\"}"; value="${value#\"}"
            value="${value%\'}"; value="${value#\'}"
            printf '%s' "$value" | tr -d '[:space:]'
        fi
    fi
}

resolve_dashboard_port() {
    local value
    value="$(_env_value WS_HUB_PORT)"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
    else
        printf '8760\n'
    fi
}

DASHBOARD_PORT="$(resolve_dashboard_port)"

# Notetaker port: test seam first, then RECORDER_PORT from .env, then 8780.
resolve_record_port() {
    local value
    if [[ -n "${ONCUE_RECORD_PORT:-}" ]]; then
        printf '%s\n' "$ONCUE_RECORD_PORT"
        return
    fi
    value="$(_env_value RECORDER_PORT)"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
    else
        printf '8780\n'
    fi
}

RECORD_PORT="$(resolve_record_port)"

case "$MODE" in
    dashboard)
        PORT="$DASHBOARD_PORT"
        OTHER_ROUTE="notetaker"
        OTHER_LABEL="notulist"
        OWN_LABEL="copilot"
        OTHER_PORT="$RECORD_PORT"
        ;;
    record)
        PORT="$RECORD_PORT"
        OTHER_ROUTE="copilot"
        OTHER_LABEL="copilot"
        OWN_LABEL="notulist"
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

BAR_PID_FILE="$PID_DIR/oncue-launcher-bar.pid"
BAR_EXE="$REPO_ROOT/bin/OnCue Bar.app/Contents/MacOS/OnCueBar"

# The pid file only counts when it names a live process whose command is the
# recording bar. A dead or foreign pid (reused by something else) is ignored.
_stop_bar_from_pidfile() {
    local pid cmd
    [[ -f "$BAR_PID_FILE" ]] || return 0
    pid="$(tr -d '[:space:]' < "$BAR_PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$cmd" =~ /OnCueBar([[:space:]]|$) ]] || return 0
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.25
    done
}

# Working directory of a PID, as reported by lsof (already symlink-resolved).
_pid_cwd() {
    local pid="$1"
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2); exit}'
}

_pid_owns_port() {
    lsof -a -p "$1" -nP -iTCP:"$2" -sTCP:LISTEN >/dev/null 2>&1
}

# One route at a time (ADR-004 decision 7): both routes record the same call.
# The other route runs when its runtime file names a live pid whose port has a
# listener. Without such a file, a listener on the configured port counts.
_port_listening() {
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

_run_field() {
    grep -o "\"$2\"[[:space:]]*:[[:space:]]*[0-9]*" "$1" 2>/dev/null | head -n1 | grep -o '[0-9]*$' || true
}

OTHER_PID=""
OTHER_LISTEN_PORT=""

# A pid counts as this OnCue only when its working directory is this repo.
_pid_in_this_repo() {
    local cwd
    cwd="$(_pid_cwd "$1")"
    [[ -n "$cwd" && "$(_canon_dir "$cwd")" == "$REPO_ROOT_CANON" ]]
}

_find_other_route() {
    local run_file="$REPO_ROOT/data/run/$OTHER_ROUTE.json" pid port foreign=""
    if [[ -r "$run_file" ]]; then
        pid="$(_run_field "$run_file" pid)"
        port="$(_run_field "$run_file" port)"
        if [[ "$pid" =~ ^[0-9]+$ && "$port" =~ ^[0-9]+$ ]] \
            && kill -0 "$pid" 2>/dev/null && _pid_owns_port "$pid" "$port"; then
            if _pid_in_this_repo "$pid"; then
                OTHER_PID="$pid"
                OTHER_LISTEN_PORT="$port"
                return 0
            fi
            foreign="$port"
        fi
    fi
    pid="$(lsof -t -nP -iTCP:"$OTHER_PORT" -sTCP:LISTEN 2>/dev/null | head -n1 || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        if _pid_in_this_repo "$pid"; then
            OTHER_PID="$pid"
            OTHER_LISTEN_PORT="$OTHER_PORT"
            return 0
        fi
        foreign="$OTHER_PORT"
    fi
    if [[ -n "$foreign" ]]; then
        echo "$(_msg foreign_port port="$foreign")" >&2
    fi
    return 1
}

# The pid must be gone and the port free: a free port alone is not enough,
# the server closes its listener before the shutdown that ends a recording.
# The deadline runs on the clock (SECONDS), not on an iteration count.
_wait_route_gone() {
    local pid="$1" port="$2" limit="$3" deadline
    deadline=$((SECONDS + limit))
    while ((SECONDS < deadline)); do
        if ! kill -0 "$pid" 2>/dev/null && ! _port_listening "$port"; then
            return 0
        fi
        sleep 0.2
    done
    return 1
}

if _find_other_route; then
    SWITCH_MSG="$(_msg switch_question label="$OTHER_LABEL")"
    SWITCH_NO="$(_msg switch_no)"
    SWITCH_YES="$(_msg switch_yes)"
    if [[ "$OTHER_ROUTE" == "notetaker" ]]; then
        STATUS_BODY="$(curl -s -m 2 "http://127.0.0.1:${OTHER_LISTEN_PORT}/status" 2>/dev/null || true)"
        PHASE="$(printf '%s' "$STATUS_BODY" | sed -n 's/.*"phase"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
        if [[ -n "$PHASE" && "$PHASE" != "idle" ]]; then
            SWITCH_MSG="$SWITCH_MSG $(_msg switch_recording)"
        fi
    fi
    SWITCH_CHOICE="$(osascript -e "display dialog \"$SWITCH_MSG\" buttons {\"$SWITCH_NO\", \"$SWITCH_YES\"} default button \"$SWITCH_NO\" with title \"OnCue\"" 2>/dev/null | sed -n 's/^button returned://p')" || true
    if [[ "$SWITCH_CHOICE" != "$SWITCH_YES" ]]; then
        exit 0
    fi

    SWITCH_LIMIT="${ONCUE_SWITCH_TIMEOUT_S:-60}"
    [[ "$SWITCH_LIMIT" =~ ^[0-9]+$ ]] || SWITCH_LIMIT=60
    kill -TERM "$OTHER_PID" 2>/dev/null || true
    if ! _wait_route_gone "$OTHER_PID" "$OTHER_LISTEN_PORT" "$SWITCH_LIMIT"; then
        osascript -e "display notification \"$(_msg switch_notification label="$OWN_LABEL")\" with title \"OnCue\"" >/dev/null 2>&1 || true
        echo "$(_msg switch_timeout other="$OTHER_LABEL" pid="$OTHER_PID" port="$OTHER_LISTEN_PORT" limit="$SWITCH_LIMIT" label="$OWN_LABEL")" >&2
        exit 1
    fi
    if [[ "$OTHER_ROUTE" == "notetaker" ]]; then
        _stop_bar_from_pidfile
    fi
fi

# Never kill by pattern-matching a command line: that matches any process on
# the machine whose args happen to contain the string, including a live
# session in a different clone of this repo. Establish ownership by PID via
# the port itself, and only ever kill PIDs discovered that way.
PORT_PIDS=()
while IFS= read -r _port_pid; do
    [[ -n "$_port_pid" ]] && PORT_PIDS+=("$_port_pid")
done < <(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)

if [[ "${#PORT_PIDS[@]}" -gt 0 ]]; then
    SAME_REPO_PIDS=()
    FOREIGN=0
    for pid in "${PORT_PIDS[@]}"; do
        owner_cwd="$(_pid_cwd "$pid")"
        if [[ -z "$owner_cwd" ]]; then
            FOREIGN=1
            echo "$(_msg cwd_unknown pid="$pid" port="$PORT")" >&2
            continue
        fi
        if [[ "$(_canon_dir "$owner_cwd")" == "$REPO_ROOT_CANON" ]]; then
            SAME_REPO_PIDS+=("$pid")
        else
            FOREIGN=1
            echo "$(_msg port_other_folder port="$PORT" pid="$pid" cwd="$owner_cwd")" >&2
        fi
    done

    if [[ "$FOREIGN" -eq 1 ]]; then
        echo "$(_msg resolve_manually)" >&2
        exit 1
    fi

    DIALOG_MSG="$(_msg restart_question mode="$MODE" port="$PORT")"
    RESTART_CANCEL="$(_msg restart_cancel)"
    RESTART_YES="$(_msg restart_yes)"
    CHOICE="$(osascript -e "display dialog \"$DIALOG_MSG\" buttons {\"$RESTART_CANCEL\", \"$RESTART_YES\"} default button \"$RESTART_CANCEL\" with title \"OnCue\"" 2>/dev/null | sed -n 's/^button returned://p')" || true

    if [[ "$CHOICE" != "$RESTART_YES" ]]; then
        exit 0
    fi

    for pid in "${SAME_REPO_PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    if [[ "$MODE" == "record" ]]; then
        _stop_bar_from_pidfile
    fi

    FREED=0
    for _ in $(seq 1 10); do
        if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            FREED=1
            break
        fi
        sleep 1
    done

    if [[ "$FREED" -ne 1 ]]; then
        echo "$(_msg port_not_freed port="$PORT" pids="${SAME_REPO_PIDS[*]}")" >&2
        exit 1
    fi
fi

mkdir -p data/logs
LOG_FILE="data/logs/launcher-${MODE}.log"
PID_FILE="$PID_DIR/oncue-launcher-${MODE}.pid"

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
    for owner_pid in $(lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
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
        echo "$(_msg server_stopped mode="$MODE" log="$REPO_ROOT/$LOG_FILE")" >&2
        exit 1
    fi
    if _server_owns_port "$PORT"; then
        READY=1
        break
    fi
    sleep 1
done

if [[ "$READY" -ne 1 ]]; then
    echo "$(_msg server_timeout mode="$MODE" log="$REPO_ROOT/$LOG_FILE")" >&2
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

    # The recording bar is a separate process. Without it OnCue still works,
    # so nothing below may stop the launcher.
    BAR_LOG="data/logs/launcher-bar.log"
    BAR_MARKER="data/.bar-missing-notified"
    BAR_HOTKEY="$(_env_value BAR_HOTKEY)"
    BAR_HOTKEY="${BAR_HOTKEY:-ctrl+option+r}"
    _stop_bar_from_pidfile
    if [[ -x "$BAR_EXE" ]]; then
        nohup "$BAR_EXE" --port "$RECORD_PORT" --hotkey "$BAR_HOTKEY" >> "$BAR_LOG" 2>&1 &
        echo "$!" > "$BAR_PID_FILE"
        rm -f "$BAR_MARKER"
    else
        printf '%s opnamebalk ontbreekt of is niet uitvoerbaar: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$BAR_EXE" >> "$BAR_LOG"
        if [[ ! -e "$BAR_MARKER" ]]; then
            osascript -e "display notification \"$(_msg bar_missing_notification)\" with title \"OnCue\"" >/dev/null 2>&1 || true
            : > "$BAR_MARKER" || true
        fi
    fi
fi

echo "$(_msg started mode="$MODE" pid="$SERVER_PID" port="$PORT" log="$REPO_ROOT/$LOG_FILE")"
