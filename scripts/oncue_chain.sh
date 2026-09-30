#!/usr/bin/env bash
# One command to bring up the whole OnCue chain before a live call.
#
# Usage:
#   bash scripts/oncue_chain.sh check   # read-only preflight, exit 0 iff nothing required is FAIL
#   bash scripts/oncue_chain.sh up      # check, then start/register whatever is missing, then wait
#
# Repo root is resolved from this script's own location (never assume
# ~/Development/sales-copilot — this repo is cloned/worktreed in more than one
# place). Port/host come from the app's own config (sales_copilot.core.config
# .WebSocketConfig), never hardcoded, so a custom WS_HUB_PORT in .env is
# respected automatically.
#
# `up` never starts the backend as a child of this shell: it delegates to
# scripts/launcher/start-oncue.command via `open -a Terminal`, exactly like a
# human double-clicking it, because a backend parented by this script (or by
# tmux/launchd) has no app identity and the audio tap silently returns
# silence. `up` also never duplicates scripts/launcher/oncue-launch.sh's port
# or repo-resolution logic -- it only asks that script to do the launching.
#
# macOS bash 3.2 compatible: no mapfile, no associative arrays, no timeout(1).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT_CANON="$(cd "$REPO_ROOT" && pwd -P)"
PYTHON="$REPO_ROOT/.venv/bin/python"
START_COMMAND="$REPO_ROOT/scripts/launcher/start-oncue.command"

FAIL_COUNT=0
WARN_COUNT=0

# Populated by the individual _check_* functions, consumed by cmd_up() to
# decide what still needs doing. Plain globals, not an associative array:
# this has to run on bash 3.2.
STATUS_VENV=""
STATUS_HUB=""
STATUS_OWNER=""
STATUS_MCP=""
HUB_HOST="127.0.0.1"
HUB_PORT="8760"

_report() {
    local level="$1" label="$2" msg="$3" fix="${4:-}"
    case "$level" in
        OK)
            printf '[OK]   %-24s %s\n' "$label" "$msg"
            ;;
        WARN)
            printf '[WARN] %-24s %s\n' "$label" "$msg"
            WARN_COUNT=$((WARN_COUNT + 1))
            ;;
        FAIL)
            printf '[FAIL] %-24s %s\n' "$label" "$msg"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            ;;
        *)
            printf '[????] %-24s %s\n' "$label" "$msg"
            ;;
    esac
    if [[ -n "$fix" && "$level" != "OK" ]]; then
        printf '       Fix: %s\n' "$fix"
    fi
}

# Working directory of a PID, as reported by lsof (symlink-resolved) -- same
# technique scripts/launcher/oncue-launch.sh uses to establish ownership
# before it will touch a process on a port.
_pid_cwd() {
    lsof -a -p "$1" -d cwd -Fn 2>/dev/null | awk '/^n/{print substr($0,2); exit}'
}

_check_venv() {
    if [[ ! -x "$PYTHON" ]]; then
        STATUS_VENV="FAIL"
        _report FAIL "1. Repo + venv" ".venv/bin/python ontbreekt in $REPO_ROOT" \
            "cd $REPO_ROOT && ./scripts/setup.sh"
        return 0
    fi
    if "$PYTHON" -c "import sales_copilot" >/dev/null 2>&1; then
        STATUS_VENV="OK"
        _report OK "1. Repo + venv" ".venv/bin/python bestaat en importeert sales_copilot"
    else
        STATUS_VENV="FAIL"
        _report FAIL "1. Repo + venv" ".venv/bin/python bestaat maar kan sales_copilot niet importeren" \
            "cd $REPO_ROOT && ./scripts/setup.sh"
    fi
    return 0
}

# Console scripts are declared once, in pyproject.toml's [project.scripts], and
# materialised as wrapper files in the venv's bin/ at install time only. `git
# pull` moves the declaration; nothing moves the wrappers. A command added after
# the last install is therefore simply absent -- no error, no log line, nothing
# to notice -- until someone reinstalls. The declared set is read out of
# pyproject.toml itself, never retyped here, the same rule checks 5, 6 and 7
# follow for their own sources of truth.
_check_entrypoints() {
    local pyproject="$REPO_ROOT/pyproject.toml"
    if [[ ! -f "$pyproject" ]]; then
        _report OK "1b. Entrypoints" "geen pyproject.toml naast deze installatie (verpakte build) -- niets te vergelijken"
        return 0
    fi
    if [[ "$STATUS_VENV" != "OK" ]]; then
        _report FAIL "1b. Entrypoints" "kan [project.scripts] niet vergelijken (venv ontbreekt, zie check 1)"
        return 0
    fi
    local declared
    if ! declared="$("$PYTHON" -c '
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    data = tomllib.load(fh)
for name in (data.get("project") or {}).get("scripts") or {}:
    print(name)
' "$pyproject" 2>/dev/null)"; then
        _report WARN "1b. Entrypoints" "kan [project.scripts] niet uit $pyproject lezen"
        return 0
    fi
    if [[ -z "$declared" ]]; then
        _report OK "1b. Entrypoints" "pyproject.toml declareert geen console-scripts"
        return 0
    fi
    local bindir name missing="" present=""
    bindir="$(dirname "$PYTHON")"
    while IFS= read -r name; do
        [[ -z "$name" ]] && continue
        if [[ -x "$bindir/$name" ]]; then
            present="${present}${present:+, }${name}"
        else
            missing="${missing}${missing:+, }${name}"
        fi
    done <<EOF
$declared
EOF
    if [[ -n "$missing" ]]; then
        _report FAIL "1b. Entrypoints" "gedeclareerd in pyproject.toml maar niet aanwezig in $bindir: $missing -- deze checkout is bijgewerkt zonder herinstallatie" \
            "cd $REPO_ROOT && bash scripts/install.sh   # of: .venv/bin/pip install -e \".[smart]\""
    else
        _report OK "1b. Entrypoints" "alle gedeclareerde commando's staan in $bindir ($present)"
    fi
    return 0
}

# Sets HUB_HOST/HUB_PORT from the app's own config (WS_HUB_HOST/WS_HUB_PORT
# via .env), falling back to the app's own defaults (127.0.0.1:8760) when the
# venv is unusable -- those are the same defaults sales_copilot.core.config
# .WebSocketConfig itself falls back to.
_resolve_hub_config() {
    if [[ "$STATUS_VENV" == "OK" ]]; then
        local out
        if out="$(cd "$REPO_ROOT" && "$PYTHON" -c '
from sales_copilot.core.config import load_env, WebSocketConfig
load_env()
c = WebSocketConfig.from_env()
print(f"{c.host}|{c.port}")
' 2>/dev/null)"; then
            HUB_HOST="${out%%|*}"
            HUB_PORT="${out##*|}"
        fi
    fi
    return 0
}

_check_hub() {
    if ! lsof -i ":$HUB_PORT" >/dev/null 2>&1; then
        STATUS_HUB="FAIL"
        _report FAIL "2. Hub" "niets luistert op poort $HUB_PORT ($HUB_HOST)" \
            "bash scripts/oncue_chain.sh up"
        return 0
    fi
    if curl -sf -m 3 "http://${HUB_HOST}:${HUB_PORT}/api/status" >/dev/null 2>&1; then
        STATUS_HUB="OK"
        _report OK "2. Hub" "luistert op poort $HUB_PORT en antwoordt op /api/status"
    else
        STATUS_HUB="FAIL"
        _report FAIL "2. Hub" "poort $HUB_PORT is bezet maar antwoordt niet op /api/status" \
            "lsof -i :$HUB_PORT   # zoek uit wie de poort vasthoudt, dan handmatig opruimen"
    fi
    return 0
}

_check_owner() {
    if [[ "$STATUS_HUB" != "OK" ]]; then
        _report WARN "3. Eigenaar van de hub" "overgeslagen (hub antwoordt niet, zie check 2)"
        return 0
    fi
    local pids foreign=0 same=0 found_pid=0 pid owner_cwd owner_canon
    pids="$(lsof -ti ":$HUB_PORT" 2>/dev/null || true)"
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        found_pid=1
        owner_cwd="$(_pid_cwd "$pid")"
        if [[ -z "$owner_cwd" ]]; then
            foreign=1
            _report FAIL "3. Eigenaar van de hub" "kan working directory van PID $pid niet vaststellen" \
                "lsof -a -p $pid -d cwd   # controleer handmatig wie dit proces is"
            continue
        fi
        owner_canon="$(cd "$owner_cwd" 2>/dev/null && pwd -P || true)"
        if [[ "$owner_canon" == "$REPO_ROOT_CANON" ]]; then
            same=1
        else
            foreign=1
            _report FAIL "3. Eigenaar van de hub" "PID $pid op poort $HUB_PORT draait vanuit een andere checkout ($owner_cwd), niet $REPO_ROOT" \
                "los dit handmatig op in die andere checkout (bijv. via Activity Monitor) -- dit script raakt processen van een andere checkout nooit aan"
        fi
    done <<EOF
$pids
EOF
    if [[ "$found_pid" -eq 0 ]]; then
        STATUS_OWNER="WARN"
        _report WARN "3. Eigenaar van de hub" "kon geen PID vinden op poort $HUB_PORT ondanks een geslaagde /api/status-call"
        return 0
    fi
    if [[ "$foreign" -eq 1 ]]; then
        STATUS_OWNER="FAIL"
    elif [[ "$same" -eq 1 ]]; then
        STATUS_OWNER="OK"
        _report OK "3. Eigenaar van de hub" "hub draait vanuit dit repo-root ($REPO_ROOT)"
    fi
    return 0
}

_check_mcp() {
    local fix
    fix="claude mcp add oncue -s user -e MCP_BRIDGE_ENABLED=true -- /bin/sh -c \"cd $REPO_ROOT && exec .venv/bin/python -m sales_copilot.mcp_bridge\""
    if ! command -v claude >/dev/null 2>&1; then
        STATUS_MCP="FAIL"
        _report FAIL "4. MCP-registratie" "'claude' commando niet gevonden op PATH" "$fix"
        return 0
    fi
    if claude mcp get oncue >/dev/null 2>&1; then
        STATUS_MCP="OK"
        _report OK "4. MCP-registratie" "oncue geregistreerd bij claude mcp (user scope)"
    else
        STATUS_MCP="FAIL"
        _report FAIL "4. MCP-registratie" "oncue niet gevonden in claude mcp list" "$fix"
    fi
    return 0
}

# What this check does and does not cover. It reads MEETING_APP_CANDIDATES,
# which drives auto-start (core/autostart_monitor.py) -- NOT the audio tap. The
# audiotee path runs with tap_all=True and follows the whole system output mix,
# so it needs no meeting app at all. Saying otherwise trained the operator to
# read "no meeting app" as "no tap", which is the confusion the runtime
# tap-health heartbeat (audio/tap_health.py) exists to end. A pre-call check
# cannot answer "is audio flowing" anyway; that only becomes a fact once the
# call is running.
_check_meeting_apps() {
    if [[ "$STATUS_VENV" != "OK" ]]; then
        _report FAIL "5. Meeting-app" "kan MEETING_APP_CANDIDATES niet uitlezen (venv ontbreekt, zie check 1)"
        return 0
    fi
    local candidates running=0 found="" name all=""
    candidates="$("$PYTHON" -c "
from sales_copilot.audio.capture import MEETING_APP_CANDIDATES
for n in MEETING_APP_CANDIDATES:
    print(n)
" 2>/dev/null)" || candidates=""
    while IFS= read -r name; do
        [[ -z "$name" ]] && continue
        all="${all}${all:+, }${name}"
        if pgrep -x "$name" >/dev/null 2>&1; then
            running=$((running + 1))
            found="${found}${found:+, }${name}"
        fi
    done <<EOF
$candidates
EOF
    if [[ "$running" -eq 0 ]]; then
        _report WARN "5. Meeting-app" "geen kandidaat actief ($all) -- auto-start van het gesprek doet niets; de tap zelf volgt de hele systeemmix en heeft geen meeting-app nodig" \
            "open Google Meet, Microsoft Teams of Zoom voordat je het gesprek start"
    elif [[ "$running" -eq 1 ]]; then
        _report OK "5. Meeting-app" "precies een kandidaat actief: $found"
    else
        _report FAIL "5. Meeting-app" "meerdere kandidaten actief tegelijk ($found) -- auto-detectie kiest dan niets" \
            "sluit de app(s) die je niet gebruikt, of zet TARGET_PROCESS_NAME handmatig in .env"
    fi
    return 0
}

_check_detector_default() {
    if [[ "$STATUS_VENV" != "OK" ]]; then
        _report FAIL "6. Detector volgende call" "kan config/presets.yaml niet uitlezen (venv ontbreekt, zie check 1)"
        return 0
    fi
    local out name value
    out="$(cd "$REPO_ROOT" && "$PYTHON" -c "
import yaml
from sales_copilot.core.paths import resolve_app_path
data = yaml.safe_load(open(resolve_app_path('config/presets.yaml'))) or {}
presets = data.get('presets', data)
if isinstance(presets, list) and presets:
    first = presets[0]
    name = first.get('name', '<naamloos>')
elif isinstance(presets, dict) and presets:
    name, first = next(iter(presets.items()))
else:
    name, first = '<geen presets>', {}
value = (first.get('modules') or {}).get('pain_points')
print(f'{name}|{value}')
" 2>/dev/null)" || out="<onbekend>|<onbekend>"
    name="${out%%|*}"
    value="${out##*|}"
    if [[ "$value" == "True" ]]; then
        _report OK "6. Detector volgende call" "standaardpreset '$name' heeft pain_points: true (bron: config/presets.yaml)"
    else
        _report WARN "6. Detector volgende call" "standaardpreset '$name' heeft pain_points: $value -- een verse browser zonder opgeslagen keuze start dus zonder detectie (sinds #206 zichtbaar gemeld in het dashboard)" \
            "kies in de dashboard-setup preset 'full_demo' of 'pitch', of vink 'AI detectie' aan voor je op Start klikt"
    fi
    return 0
}

# Resolve a log file name from the module that actually writes it, so this
# check can never vouch for a file the app does not use. Same pattern as
# check 5, which reads MEETING_APP_CANDIDATES out of capture.py rather than
# retyping the list. Echoes nothing when the venv or the module is absent;
# callers treat an empty answer as "cannot determine", never as FAIL.
_log_name_from_source() {
    local module="$1" attr="$2"
    [[ "$STATUS_VENV" == "OK" ]] || return 0
    (cd "$REPO_ROOT" && "$PYTHON" -c "
import importlib, sys
try:
    print(getattr(importlib.import_module('$module'), '$attr'))
except Exception:
    sys.exit(1)
" 2>/dev/null) || true
}

_check_logs() {
    local logs_dir app_log_name app_log
    # LOG_DIR wins over the default, exactly as core/logging.py resolves it.
    logs_dir="$(cd "$REPO_ROOT" && "$PYTHON" -c "
import os
from sales_copilot.core.paths import resolve_app_path
print(os.environ.get('LOG_DIR') or resolve_app_path('data/logs'))
" 2>/dev/null)" || logs_dir=""
    [[ -n "$logs_dir" ]] || logs_dir="$REPO_ROOT/data/logs"

    app_log_name="$(_log_name_from_source sales_copilot.core.logging LOG_FILE_NAME)"
    [[ -n "$app_log_name" ]] || app_log_name="copilot.log"
    app_log="$logs_dir/$app_log_name"

    if [[ -f "$app_log" ]]; then
        if [[ -w "$app_log" ]]; then
            _report OK "7a. App-log" "bestaat en is schrijfbaar ($app_log)"
        else
            _report FAIL "7a. App-log" "bestaat maar is niet schrijfbaar ($app_log)" "chmod u+w \"$app_log\""
        fi
    elif [[ -d "$logs_dir" ]]; then
        if [[ -w "$logs_dir" ]]; then
            _report OK "7a. App-log" "bestaat nog niet, maar $logs_dir is schrijfbaar (ontstaat bij eerste start)"
        else
            _report FAIL "7a. App-log" "$logs_dir bestaat maar is niet schrijfbaar" "chmod u+w \"$logs_dir\""
        fi
    elif [[ -w "$REPO_ROOT/data" ]] 2>/dev/null; then
        _report OK "7a. App-log" "$logs_dir bestaat nog niet, wordt aangemaakt bij eerste start"
    elif [[ -w "$REPO_ROOT" ]]; then
        _report OK "7a. App-log" "data/ bestaat nog niet, wordt aangemaakt bij eerste start"
    else
        _report FAIL "7a. App-log" "kan $logs_dir niet aanmaken (geen schrijfrechten in $REPO_ROOT)" \
            "chmod u+w \"$REPO_ROOT\""
    fi

    # The bridge writes its own rotating log since PR #202. Before that the
    # only trace was Claude Code's stdio capture directory, which is what this
    # check used to look at; that directory belongs to the host, not to us, so
    # it is now a secondary line and never decides the verdict.
    # src/sales_copilot/mcp_bridge/ is excluded from the public export, so an
    # OSS checkout has no such module: say so calmly instead of failing.
    local mcp_log_name mcp_log
    mcp_log_name="$(_log_name_from_source sales_copilot.mcp_bridge.server _LOG_FILE_NAME)"
    if [[ -z "$mcp_log_name" ]]; then
        _report OK "7b. MCP-bridge-log" "MCP-brug niet aanwezig in deze checkout (Pro-only) -- geen actie nodig"
    else
        mcp_log="$logs_dir/$mcp_log_name"
        if [[ -f "$mcp_log" ]]; then
            if [[ -w "$mcp_log" ]]; then
                _report OK "7b. MCP-bridge-log" "aanwezig en schrijfbaar ($mcp_log)"
            else
                _report FAIL "7b. MCP-bridge-log" "aanwezig maar niet schrijfbaar ($mcp_log)" "chmod u+w \"$mcp_log\""
            fi
        else
            _report OK "7b. MCP-bridge-log" "nog niet aangemaakt (verschijnt na de eerste MCP-sessie) -- geen actie nodig"
        fi
    fi

    local slug host_log_dir
    slug="${REPO_ROOT_CANON//\//-}"
    host_log_dir="$HOME/Library/Caches/claude-cli-nodejs/${slug}/mcp-logs-oncue"
    if [[ -d "$host_log_dir" ]]; then
        printf '       (host-stdio-log: %s)\n' "$host_log_dir"
    fi
    return 0
}

run_checks() {
    printf 'OnCue chain check -- %s\n' "$REPO_ROOT"
    printf -- '------------------------------------------------------------\n'
    _check_venv
    _check_entrypoints
    _resolve_hub_config
    _check_hub
    _check_owner
    _check_mcp
    _check_meeting_apps
    _check_detector_default
    _check_logs
    printf -- '------------------------------------------------------------\n'
    if [[ "$FAIL_COUNT" -gt 0 ]]; then
        printf 'Resultaat: %d FAIL, %d WARN\n' "$FAIL_COUNT" "$WARN_COUNT"
    else
        printf 'Resultaat: alles OK (%d WARN)\n' "$WARN_COUNT"
    fi
    return 0
}

cmd_check() {
    run_checks
    if [[ "$FAIL_COUNT" -gt 0 ]]; then
        return 1
    fi
    return 0
}

cmd_up() {
    run_checks
    echo
    echo "Aanvullen wat ontbreekt..."

    if [[ "$STATUS_VENV" != "OK" ]]; then
        echo "Kan niet doorgaan: venv ontbreekt of is kapot. Run eerst: cd $REPO_ROOT && ./scripts/setup.sh" >&2
        exit 1
    fi

    if [[ "$STATUS_OWNER" == "FAIL" ]]; then
        echo "Kan niet doorgaan: poort $HUB_PORT is bezet door een andere checkout. Los dat eerst handmatig op (zie check 3 hierboven)." >&2
        exit 1
    fi

    if [[ "$STATUS_HUB" == "OK" ]]; then
        echo "Hub draait al vanuit dit repo-root (poort $HUB_PORT) -- niet opnieuw gestart."
    else
        echo "Backend starten in een nieuw Terminal-venster..."
        open -a Terminal "$START_COMMAND"
    fi

    if [[ "$STATUS_MCP" == "OK" ]]; then
        echo "MCP-server 'oncue' is al geregistreerd -- niet opnieuw."
    else
        echo "MCP-server 'oncue' registreren..."
        claude mcp add oncue -s user -e MCP_BRIDGE_ENABLED=true -- /bin/sh -c "cd $REPO_ROOT && exec .venv/bin/python -m sales_copilot.mcp_bridge"
    fi

    echo "Wachten tot de hub antwoordt op /api/status (max 60s)..."
    local waited=0 ready=0
    while [[ "$waited" -lt 60 ]]; do
        if curl -sf -m 2 "http://${HUB_HOST}:${HUB_PORT}/api/status" >/dev/null 2>&1; then
            ready=1
            break
        fi
        printf '.'
        sleep 2
        waited=$((waited + 2))
    done
    echo

    if [[ "$ready" -ne 1 ]]; then
        echo "Timeout: de hub antwoordt nog niet na ${waited}s. Controleer het app-log (zie check 7a)" >&2
        exit 1
    fi

    echo
    echo "Checks opnieuw uitvoeren na opstarten..."
    FAIL_COUNT=0
    WARN_COUNT=0
    run_checks

    echo
    echo "=== Klaar ==="
    if [[ "$FAIL_COUNT" -eq 0 ]]; then
        echo "Alles staat klaar. Het enige dat je zelf nog moet doen: start het gesprek in de dashboard."
        return 0
    fi
    echo "Nog niet alles is groen (zie FAIL hierboven) -- los dat op voor je belt."
    return 1
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        check)
            cmd_check
            ;;
        up)
            cmd_up
            ;;
        *)
            echo "Gebruik: $0 {check|up}" >&2
            exit 2
            ;;
    esac
}

main "$@"
