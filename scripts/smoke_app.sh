#!/bin/bash
# Smoke test of a built OnCue.app: copy it outside the repo, start it with an empty
# PYTHONPATH and no venv on PATH, wait for the notetaker server, check the start page,
# stop the app. Exit 0 = pass, non-zero = fail.
#
# Usage: bash scripts/smoke_app.sh <OnCue.app> [timeout_seconds]
#   SMOKE_TIMEOUT  seconds to wait for the server (default 90; the argument wins)
#   SMOKE_KEEP    set to 1 to keep the temp copy (app, app.log, home) and print its path
#   SMOKE_PORT     port for the notetaker (default 18780, not the real 8780)
#   SMOKE_STOP_TIMEOUT  seconds to wait after SIGTERM before SIGKILL (default 20)
#
# The embedded recording bar (Contents/Helpers/OnCue Bar.app) must start within the timeout and be
# gone after the app stops. A bundle without a bar is skipped unless SMOKE_REQUIRE_BAR=1.
#   SMOKE_REQUIRE_BAR  1 = fail when the bundle has no bar (use it for a real build)
#
# The bundled libraries: the interpreter in Contents/MacOS runs with -S (no site-packages of the machine) and
# -P (the working directory is not prepended to sys.path under -c) and a PYTHONPATH of only the bundle's
# lib/python3.12, python312.zip and lib-dynload, so a package missing from the bundle fails instead of coming
# from the build machine or the directory the script was started in. PortAudio loads through sounddevice and
# the packages that carry dylibs import. Their binaries
# must sit as real files in the bundle (not inside python312.zip) to be signed and notarized.
#   SMOKE_IMPORT_MODULES  space-separated modules to import (default below)
# A bundle without Contents/MacOS/python skips the check unless SMOKE_REQUIRE_BAR=1 (the same flag as the bar).
#
# The server answers /status (JSON with a phase) as its health signal; the hub's own
# routes are not started in notetaker mode. The start page is "/", the notetaker UI.

set -euo pipefail

APP_SRC="${1:?usage: smoke_app.sh <OnCue.app> [timeout_seconds]}"
TIMEOUT="${2:-${SMOKE_TIMEOUT:-90}}"
PORT="${SMOKE_PORT:-18780}"
[[ -d "$APP_SRC/Contents/MacOS" ]] || { echo "FAIL: $APP_SRC is not an app bundle" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck source=scripts/safe_tmp_rm.sh
source "$REPO_ROOT/scripts/safe_tmp_rm.sh"
WORK="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/oncue-smoke.XXXXXX")" && pwd -P)"
PID=""
# SIGTERM first, SIGKILL when the app is still alive after SMOKE_STOP_TIMEOUT seconds, so an app that
# ignores SIGTERM cannot stop the EXIT handler from removing the temp folder.
stop_app() {
    [[ -n "$PID" ]] || return 0
    kill -TERM "$PID" 2>/dev/null || true
    local waited=0 limit="${SMOKE_STOP_TIMEOUT:-20}"
    while kill -0 "$PID" 2>/dev/null && ((waited < limit * 10)); do
        sleep 0.1
        waited=$((waited + 1))
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "WARNING: app ignored SIGTERM for ${limit}s; sending SIGKILL" >&2
        kill -KILL "$PID" 2>/dev/null || true
    fi
    wait "$PID" 2>/dev/null || true
}
# On exit: stop the app, then remove the temp copy unless SMOKE_KEEP=1. The failure branches
# below print the tail of app.log before they exit, so the log is shown before it is removed.
on_exit() {
    local status=$?
    trap - EXIT
    stop_app
    if [[ "${SMOKE_KEEP:-0}" == "1" ]]; then
        echo "Smoke temp folder kept: $WORK" >&2
    else
        safe_tmp_rm "$WORK" || echo "WARNING: smoke temp folder $WORK was not removed" >&2
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "$WORK" in "$REPO_ROOT"/*) echo "FAIL: temp dir is inside the repo" >&2; exit 1 ;; esac
mkdir -p "$WORK/home"
cp -a "$APP_SRC" "$WORK/OnCue.app"


# Import check. The module name that fails is in the message; sounddevice must also load PortAudio.
BUNDLE_PY="$WORK/OnCue.app/Contents/MacOS/python"
BUNDLE_LIB="$WORK/OnCue.app/Contents/Resources/lib"
BUNDLE_PYPATH="$BUNDLE_LIB/python3.12:$BUNDLE_LIB/python312.zip:$BUNDLE_LIB/python3.12/lib-dynload"
IMPORT_NOTE="imports: no bundle interpreter (skipped)"
if [[ -x "$BUNDLE_PY" ]]; then
    IMPORT_MODULES="${SMOKE_IMPORT_MODULES:-sounddevice PIL mlx.core llvmlite.binding google._upb._message}"
    for mod in $IMPORT_MODULES; do
        code="import $mod"
        [[ "$mod" == "sounddevice" ]] && code="import sounddevice; sounddevice.get_portaudio_version()"
        if ! env -i HOME="$WORK/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" PYTHONPATH="$BUNDLE_PYPATH" \
            "$BUNDLE_PY" -S -P -c "$code" >"$WORK/import.log" 2>&1; then
            echo "FAIL: the bundled python cannot import $mod" >&2
            tail -n 15 "$WORK/import.log" >&2
            exit 1
        fi
    done
    IMPORT_NOTE="imports ok: $IMPORT_MODULES"
elif [[ "${SMOKE_REQUIRE_BAR:-0}" == "1" ]]; then
    echo "FAIL: the bundle has no interpreter at $BUNDLE_PY" >&2
    exit 1
fi

listeners() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true; }
if [[ -n "$(listeners)" ]]; then
    echo "FAIL: port $PORT is already in use" >&2
    exit 1
fi

# True when $1 is the app process $PID or one of its descendants.
is_app_process() {
    local candidate="$1"
    [[ "$candidate" == "$PID" ]] && return 0
    # Walk up from the listener: its ancestors must include $PID.
    local cur="$candidate" parent
    while [[ -n "$cur" && "$cur" != "1" && "$cur" != "0" ]]; do
        [[ "$cur" == "$PID" ]] && return 0
        parent="$(ps -o ppid= -p "$cur" 2>/dev/null | tr -d ' ')"
        cur="$parent"
    done
    return 1
}

EXE="$(ls "$WORK/OnCue.app/Contents/MacOS/"* | head -n 1)"
echo "Starting $EXE (port $PORT, HOME $WORK/home)"
env -i HOME="$WORK/home" PATH="/usr/bin:/bin:/usr/sbin:/sbin" PYTHONPATH="" \
    ONCUE_APP_MODE=record RECORDER_PORT="$PORT" SALES_COPILOT_NO_BROWSER=1 \
    "$EXE" >"$WORK/app.log" 2>&1 &
PID=$!

deadline=$((SECONDS + TIMEOUT))
until curl -fsS -m 2 "http://127.0.0.1:$PORT/status" >"$WORK/status.json" 2>/dev/null; do
    kill -0 "$PID" 2>/dev/null || { echo "FAIL: app exited early" >&2; tail -n 30 "$WORK/app.log" >&2; exit 1; }
    ((SECONDS < deadline)) || { echo "FAIL: no /status within ${TIMEOUT}s" >&2; tail -n 30 "$WORK/app.log" >&2; exit 1; }
    sleep 1
done
kill -0 "$PID" 2>/dev/null || { echo "FAIL: app exited before /status was checked" >&2; tail -n 30 "$WORK/app.log" >&2; exit 1; }
owned=0
for lp in $(listeners); do
    if is_app_process "$lp"; then owned=1; else echo "FAIL: port $PORT is held by pid $lp, not the launched app (pid $PID)" >&2; exit 1; fi
done
((owned)) || { echo "FAIL: nothing owned by the launched app listens on port $PORT" >&2; exit 1; }
grep -q '"phase"' "$WORK/status.json" || { echo "FAIL: /status has no phase: $(cat "$WORK/status.json")" >&2; exit 1; }

curl -fsS -m 5 "http://127.0.0.1:$PORT/" >"$WORK/start.html" || { echo "FAIL: start page not served" >&2; exit 1; }
grep -qi 'recorder' "$WORK/start.html" || { echo "FAIL: start page does not offer the notetaker" >&2; exit 1; }

kill -0 "$PID" 2>/dev/null || { echo "FAIL: app exited during the checks" >&2; tail -n 30 "$WORK/app.log" >&2; exit 1; }

# The recording bar: starts within the timeout while the app runs, is gone once the app stops.
BAR_EXE="$WORK/OnCue.app/Contents/Helpers/OnCue Bar.app/Contents/MacOS/OnCueBar"
bar_pids() { pgrep -f "$BAR_EXE" 2>/dev/null || true; }
BAR_NOTE="bar: not in the bundle (skipped)"
if [[ -x "$BAR_EXE" ]]; then
    until [[ -n "$(bar_pids)" ]]; do
        ((SECONDS < deadline)) || { echo "FAIL: the recording bar did not start within ${TIMEOUT}s" >&2; tail -n 30 "$WORK/app.log" >&2; exit 1; }
        sleep 1
    done
    BAR_NOTE="bar: started (pid $(bar_pids | head -n 1))"
    kill -TERM "$PID" 2>/dev/null || true
    stop_deadline=$((SECONDS + 20))
    while [[ -n "$(bar_pids)" ]]; do
        if ((SECONDS >= stop_deadline)); then
            echo "FAIL: the recording bar is still running 20s after the app was stopped" >&2
            bar_pids | xargs kill 2>/dev/null || true
            exit 1
        fi
        sleep 1
    done
    BAR_NOTE="$BAR_NOTE, gone after the app stopped"
elif [[ "${SMOKE_REQUIRE_BAR:-0}" == "1" ]]; then
    echo "FAIL: the bundle has no recording bar at $BAR_EXE" >&2
    exit 1
fi
echo "PASS: $(cat "$WORK/status.json") ; start page $(wc -c <"$WORK/start.html") bytes ; $BAR_NOTE ; $IMPORT_NOTE ; app copy ran outside the repo"
