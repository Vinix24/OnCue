#!/usr/bin/env bash
# OnCue — non-technical installer
# Idempotent: safe to run multiple times.
# Flags: --auto-yes  skip all interactive prompts (used by scripts/bootstrap.sh)
#        --prebuilt  the release route of scripts/bootstrap.sh already put the Python
#                    environment (.venv) and the prebuilt whisper.cpp and AudioTee
#                    programs in place: skip the command line tools, Homebrew, cmake,
#                    Python and compiler steps, verify those programs instead, then
#                    run the same model, settings and app steps as always.
#
# Normally started by scripts/bootstrap.sh (the one-line install). Running it
# directly in a checkout works too.
#
# Environment (all optional):
#   ONCUE_LOG_FILE       append all detail output here (bootstrap.sh sets it; run
#                        directly, a log is created in ONCUE_LOG_DIR)
#   ONCUE_LOG_DIR        log directory                  (default ~/Library/Logs/OnCue)
#   ONCUE_APPS_DIR       where OnCue.app is installed   (default ~/Applications)
#   ONCUE_CONFIG_DIR     where repo-path is written     (default ~/.oncue)
#   ONCUE_HEARTBEAT      seconds between "still working" lines (default 30)
#   ONCUE_MACOS_VERSION  override the detected macOS version (testing only)
#   ONCUE_MODEL_URL      speech model download URL (--prebuilt only; default the
#                        ggml-large-v3-turbo.bin file of ggerganov/whisper.cpp on Hugging Face)
#
# The --prebuilt path (prebuilt_* functions) must never call git, swift, cmake, make,
# cc or xcrun: without the command line tools those are shims that open Apple's
# install window. tests/test_install_release_route.py greps them.
set -euo pipefail

TOTAL_STEPS=9

# ── Colour helpers ──────────────────────────────────────────────────────────
if [[ -z "${ONCUE_COLOR:-}" ]]; then
    if [[ -t 1 ]]; then ONCUE_COLOR=1; else ONCUE_COLOR=0; fi
fi
_paint() {
    local code="$1" glyph="$2"
    shift 2
    if [[ "$ONCUE_COLOR" == 1 ]]; then
        printf '\033[%sm%s\033[0m %s\n' "$code" "$glyph" "$*"
    else
        printf '%s %s\n' "$glyph" "$*"
    fi
}
_blue()  { _paint 34 "▸" "$@"; }
_green() { _paint 32 "✓" "$@"; }
_warn()  { _paint 33 "⚠" "$@" >&2; }
_fail()  { _paint 31 "✗" "$@" >&2; exit 1; }
# LOG_FILE is set below; the timestamp line shows in the log how long each step took.
_step()  { printf '\n[%s/%s] %s\n' "$1" "$TOTAL_STEPS" "$2"; printf '[%s] step %s/%s\n' "$(date +%T)" "$1" "$TOTAL_STEPS" >> "${LOG_FILE:-/dev/null}"; }

# ── Parse flags ──────────────────────────────────────────────────────────────
AUTO_YES=false
PREBUILT=false
for _arg in "$@"; do
    [[ "$_arg" == "--auto-yes" ]] && AUTO_YES=true
    [[ "$_arg" == "--prebuilt" ]] && PREBUILT=true
done
# Step numbers of the two steps both routes share (settings, app).
STEP_SETTINGS=8
STEP_APP=9
if [[ "$PREBUILT" == true ]]; then
    TOTAL_STEPS=5
    STEP_SETTINGS=4
    STEP_APP=5
fi

# Wrapper: in auto-yes mode default Y without prompting
_confirm() {
    local prompt="$1"
    if [[ "$AUTO_YES" == true ]]; then
        _blue "$prompt [auto: Y]"
        return 0
    fi
    printf "%s [Y/n] " "$prompt"
    read -r _reply
    [[ -z "$_reply" || "$_reply" =~ ^[Yy] ]]
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# shellcheck source=scripts/oncue_preflight.sh
source "$ROOT_DIR/scripts/oncue_preflight.sh"

# ── Install log ──────────────────────────────────────────────────────────────
# Detail (compiler output, pip, curl progress) goes to the log, the terminal
# only gets one line per step. bootstrap.sh hands us its log; run directly we
# create our own and mirror our terminal output into it.
if [[ -n "${ONCUE_LOG_FILE:-}" ]]; then
    LOG_FILE="$ONCUE_LOG_FILE"
    mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
else
    _log_dir="${ONCUE_LOG_DIR:-$HOME/Library/Logs/OnCue}"
    mkdir -p "$_log_dir"
    LOG_FILE="$_log_dir/install-$(date +%Y%m%d-%H%M%S).log"
    : > "$LOG_FILE"
    export ONCUE_LOG_FILE="$LOG_FILE"
    exec > >(tee -a "$LOG_FILE") 2>&1
    _on_exit() {
        if [[ "$1" -ne 0 ]]; then
            printf '\nThe install did not finish. Log: %s\nSend this file when you ask for help.\n' "$LOG_FILE" >&2
        fi
    }
    trap '_on_exit $?' EXIT
fi

# Runs a command with all its output appended to the log and a "still working"
# line every ONCUE_HEARTBEAT seconds, so a long step never looks frozen.
# Returns the command's exit status.
_run_logged() {
    local beat="${ONCUE_HEARTBEAT:-30}" started="$SECONDS" next rc=0 pid
    printf '\n$ %s\n' "$*" >> "$LOG_FILE"
    "$@" >> "$LOG_FILE" 2>&1 &
    pid=$!
    next="$beat"
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if (( SECONDS - started >= next )); then
            printf '  ... still working (%s min %s s)\n' $(( (SECONDS - started) / 60 )) $(( (SECONDS - started) % 60 ))
            next=$(( next + beat ))
        fi
    done
    wait "$pid" || rc=$?
    return "$rc"
}

echo ""
echo "OnCue — installer"
echo "================================="
echo "Log: $LOG_FILE"

# Reasons for the status block at the end (empty = nothing went wrong).
TRANSCRIBER_REASON=""
AUDIO_REASON=""
APP_REASON=""

# ── 1. Platform guard ───────────────────────────────────────────────────────
_step 1 "Checking your Mac"
if [[ "$(uname)" != "Darwin" ]]; then
    _fail "This installer only supports macOS. Linux support is planned."
fi
if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
    _fail "OnCue needs a Mac with Apple Silicon (M1 or newer). This Mac has an Intel processor: the speech engine OnCue depends on (MLX) does not run on it."
fi
MACOS_VER="${ONCUE_MACOS_VERSION:-$(sw_vers -productVersion)}"
if ! oncue_macos_supported "$MACOS_VER"; then
    _fail "$(oncue_macos_message "$MACOS_VER")"
fi
_green "macOS $MACOS_VER on Apple Silicon"

# Paths and helpers both routes use (the status block checks them on disk).
_is_python_311() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null
}
VENV_DIR="$ROOT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
AUDIOTEE_BIN="$ROOT_DIR/bin/audiotee"
WHISPER_DIR="$ROOT_DIR/vendor/whisper.cpp"
WHISPER_CLI="$WHISPER_DIR/build/bin/whisper-cli"
WHISPER_SERVER="$WHISPER_DIR/build/bin/whisper-server"
WHISPER_MODEL_DIR="$WHISPER_DIR/models"
WHISPER_MODEL="$WHISPER_MODEL_DIR/ggml-large-v3-turbo.bin"

# ── Prebuilt route (--prebuilt) ──────────────────────────────────────────────
# bootstrap.sh downloaded and checksum-verified the programs and built the venv.
# Here they are only verified; nothing is compiled and no tool of the command
# line tools is called.
prebuilt_verify_environment() {
    if [[ ! -x "$PYTHON" ]] || ! _is_python_311 "$PYTHON"; then
        _fail "The Python environment (.venv, Python 3.11) is missing. Run the one-line install command again."
    fi
    _green "Python environment found: .venv"
    if [[ -x "$WHISPER_CLI" && -x "$WHISPER_SERVER" ]] \
        && "$WHISPER_CLI" --help >/dev/null 2>&1 && "$WHISPER_SERVER" --help >/dev/null 2>&1; then
        _green "Transcriber programs verified: whisper-cli and whisper-server"
    else
        TRANSCRIBER_REASON="the prebuilt whisper.cpp programs are missing or do not run."
        _warn "$TRANSCRIBER_REASON"
    fi
    if [[ -x "$AUDIOTEE_BIN" ]] && "$AUDIOTEE_BIN" --help >/dev/null 2>&1; then
        _green "Audio capture program verified: bin/audiotee"
    else
        AUDIO_REASON="the prebuilt AudioTee program is missing or does not run."
        _warn "$AUDIO_REASON"
    fi
}

# Downloads the GGML speech model with curl. A download that stops halfway
# leaves a .part file that the next run resumes; the model file itself only
# appears once the download is complete.
prebuilt_fetch_model() {
    local url="${ONCUE_MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin}"
    if [[ -f "$WHISPER_MODEL" ]]; then
        _green "Speech model already present"
        return 0
    fi
    mkdir -p "$WHISPER_MODEL_DIR"
    _blue "Downloading the speech model (1.5 GB, progress goes to the log)..."
    if _run_logged curl -fsSL --retry 3 --connect-timeout 20 -C - -o "$WHISPER_MODEL.part" "$url" \
        && mv "$WHISPER_MODEL.part" "$WHISPER_MODEL"; then
        _green "Speech model downloaded"
    else
        TRANSCRIBER_REASON="the speech model download failed (see the log)."
        _warn "$TRANSCRIBER_REASON Run the installer again to resume it."
    fi
}

# ── 2-7. Build route (skipped with --prebuilt) ──────────────────────────────
if [[ "$PREBUILT" == true ]]; then
    _step 2 "Checking the downloaded programs"
    prebuilt_verify_environment
    _step 3 "Speech model (1.5 GB download, 1-10 minutes)"
    prebuilt_fetch_model
else
# ── 2. Command line tools ───────────────────────────────────────────────────
# `command -v git` is true on a Mac WITHOUT the tools (an Apple shim that pops up
# an install dialog), so check for the real toolchain.
_step 2 "Apple command line tools (git, swift)"
if ! oncue_clt_present; then
    _fail "Apple's command line tools are missing. Run this, click \"Install\" in the window that opens, wait until it finishes and then run the installer again:
  xcode-select --install"
fi
_green "Command line tools found: $(xcode-select -p)"

# ── 3. Homebrew ─────────────────────────────────────────────────────────────
# Homebrew provides Python 3.11 and cmake. When it is missing, offer to run the
# official installer (the same one scripts/bootstrap.sh uses).
_step 3 "Homebrew"
export HOMEBREW_NO_ENV_HINTS=1
_brew_install_url="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
if ! command -v brew &>/dev/null; then
    if [[ -x /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi
if ! command -v brew &>/dev/null; then
    if _confirm "Homebrew not found. Install it now (official Homebrew installer)?"; then
        _blue "Installing Homebrew — 5-10 minutes, asks for your macOS password (typing stays invisible)..."
        if [[ -t 0 ]]; then
            /bin/bash -c "$(curl -fsSL "$_brew_install_url")" \
                || _warn "Homebrew installer exited non-zero — verifying below."
        else
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL "$_brew_install_url")" \
                || _warn "Homebrew installer exited non-zero — verifying below."
        fi
        if [[ -x /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        command -v brew &>/dev/null || _fail "Homebrew install did not complete. Install it from https://brew.sh, then re-run this installer."
    else
        _fail "Homebrew is required. Install it from https://brew.sh, then re-run this installer."
    fi
fi
_green "Homebrew found: $(brew --version | head -1)"

# ── 4. Python 3.11 and cmake ────────────────────────────────────────────────
# Only the Python CI tests (3.11, see .github/workflows/ci.yml) is accepted.
_step 4 "Python 3.11 and cmake"
PYTHON_BIN=""
if command -v python3.11 &>/dev/null && _is_python_311 "$(command -v python3.11)"; then
    PYTHON_BIN="$(command -v python3.11)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
    _blue "Python 3.11 not found — installing it with Homebrew (about 1 minute)..."
    _run_logged brew install python@3.11 || _fail "Could not install Python 3.11 with Homebrew. See the log: $LOG_FILE"
    _prefix="$(brew --prefix python@3.11)"
    if [[ -x "$_prefix/bin/python3.11" ]] && _is_python_311 "$_prefix/bin/python3.11"; then
        PYTHON_BIN="$_prefix/bin/python3.11"
    else
        _fail "Python 3.11 was installed but could not be found at $_prefix/bin/python3.11."
    fi
fi
PY_VER=$("$PYTHON_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
_green "Python $PY_VER found at $PYTHON_BIN"

# cmake builds the whisper.cpp transcriber. Without it transcription would
# silently fall back to the experimental mlx-whisper backend.
if command -v cmake &>/dev/null; then
    _green "cmake found: $(cmake --version | head -1)"
else
    _blue "cmake not found — installing it with Homebrew (about 1 minute)..."
    _run_logged brew install cmake || _fail "Could not install cmake with Homebrew. See the log: $LOG_FILE"
    _green "cmake installed: $(cmake --version | head -1)"
fi

# ── 5. Virtual environment + Python dependencies ────────────────────────────
_step 5 "Python packages (about 1.6 GB, 3-8 minutes)"
if [[ -x "$VENV_DIR/bin/python" ]] && ! _is_python_311 "$VENV_DIR/bin/python"; then
    _blue "Existing .venv does not use Python 3.11 — recreating it..."
    "$PYTHON_BIN" -m venv --clear "$VENV_DIR"
elif [[ ! -d "$VENV_DIR" ]]; then
    _blue "Creating virtual environment at .venv..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    _green "Virtual environment already exists at .venv"
fi

# constraints.txt pins every package to the version uv.lock (and so CI) tests,
# so a new major of a dependency cannot break a fresh install. No uv needed here.
_blue "Installing OnCue's Python packages (output goes to the log)..."
_run_logged "$PYTHON" -m pip install --upgrade pip || _fail "pip upgrade failed. See the log: $LOG_FILE"
_run_logged "$PYTHON" -m pip install -e ".[smart]" -c "$ROOT_DIR/constraints.txt" \
    || _fail "Installing the Python packages failed. Check your internet connection and run the installer again. Log: $LOG_FILE"
_green "Python packages installed"

# ── 6. AudioTee (OSS default whole-system tap) ───────────────────────────────
# AudioTee taps whole-system audio (Teams/Zoom/Meet, no manual routing). The
# Swift source is vendored at vendor/audiotee/ (MIT, upstream:
# github.com/makeusabrew/audiotee); build it locally into bin/audiotee. Only
# the telephony call-tap (iPhone-relay/FaceTime) stays Sales Pro.
_step 6 "Audio capture: AudioTee (about 1 minute)"
AUDIOTEE_SRC="$ROOT_DIR/vendor/audiotee"
if [[ -x "$AUDIOTEE_BIN" ]]; then
    _green "AudioTee binary already built: bin/audiotee"
elif [[ ! -f "$AUDIOTEE_SRC/Package.swift" ]]; then
    AUDIO_REASON="AudioTee source (vendor/audiotee) is missing from this download."
    _warn "$AUDIO_REASON"
else
    _blue "Building AudioTee (swift build -c release)..."
    if _run_logged swift build -c release --package-path "$AUDIOTEE_SRC" \
        && mkdir -p "$ROOT_DIR/bin" \
        && cp "$AUDIOTEE_SRC/.build/release/audiotee" "$AUDIOTEE_BIN" \
        && chmod +x "$AUDIOTEE_BIN"; then
        _green "AudioTee binary built: bin/audiotee"
    else
        AUDIO_REASON="the AudioTee build failed (see the log)."
        _warn "AudioTee build failed. Re-run it with: swift build -c release --package-path vendor/audiotee"
    fi
fi

# ── 7. whisper.cpp backend (binary + GGML model) ─────────────────────────────
# whisper.cpp is the default transcription backend. Its binary must be compiled
# from the vendored source and paired with a GGML model. Without it the
# transcriber degrades to the experimental mlx-whisper backend at runtime.
_step 7 "Transcriber: whisper.cpp build (about 3 minutes) and speech model (1.5 GB download, 1-10 minutes)"
# A private checkout carries whisper.cpp as an uninitialised submodule; the
# public snapshot already ships the source, so this is a no-op there.
if [[ ! -f "$WHISPER_DIR/CMakeLists.txt" ]] && [[ -f "$ROOT_DIR/.gitmodules" ]]; then
    _run_logged git -C "$ROOT_DIR" submodule update --init --recursive vendor/whisper.cpp || true
fi

if [[ -x "$WHISPER_CLI" && -x "$WHISPER_SERVER" && -f "$WHISPER_MODEL" ]]; then
    _green "whisper.cpp binary and model already present"
elif [[ ! -f "$WHISPER_DIR/CMakeLists.txt" ]]; then
    TRANSCRIBER_REASON="the whisper.cpp source (vendor/whisper.cpp) is missing."
    _warn "$TRANSCRIBER_REASON Transcription falls back to the experimental mlx-whisper backend."
else
    _blue "Building whisper.cpp and downloading the speech model (compiler output and download progress go to the log)..."
    if _run_logged bash "$ROOT_DIR/scripts/install_whisper_cpp.sh"; then
        _green "whisper.cpp built and model downloaded"
    else
        TRANSCRIBER_REASON="the whisper.cpp build or the model download failed (see the log)."
        _warn "whisper.cpp step failed. Re-run it with: bash scripts/install_whisper_cpp.sh
  Transcription falls back to the experimental mlx-whisper backend until built."
    fi
fi

# mlx-whisper fallback weights. The MLX model is cached separately (Hugging
# Face cache) so a runtime fallback to the experimental mlx-whisper backend stays
# usable when whisper.cpp could not be built. Skipped once a GGML model exists.
if ! ls "$WHISPER_MODEL_DIR"/ggml-*.bin &>/dev/null 2>&1; then
    _blue "Fetching mlx-whisper fallback weights (large-v3-turbo)..."
    _run_logged "$PYTHON" scripts/install_whisper_model.py || \
        _warn "mlx-whisper weight download skipped — run '.venv/bin/python scripts/install_whisper_model.py' manually."
fi
fi  # end of the build route (PREBUILT != true)

# ── 8. .env configuration + data directories ─────────────────────────────────
_step "$STEP_SETTINGS" "Settings and data folders"
ENV_FILE="$ROOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ROOT_DIR/.env.example" "$ENV_FILE"
    _green ".env created from .env.example"
else
    _green ".env already present (left untouched)"
fi
mkdir -p "$ROOT_DIR/data/sessions"
_green "data/ directories ready"

# ── 9. The OnCue app ─────────────────────────────────────────────────────────
# Build the launcher app, copy it to ~/Applications (no admin rights needed) and
# remember where this checkout lives so the copy can find it.
_step "$STEP_APP" "OnCue app (about 10 seconds)"
APPS_DIR="${ONCUE_APPS_DIR:-$HOME/Applications}"
CONFIG_DIR="${ONCUE_CONFIG_DIR:-$HOME/.oncue}"
APP_SRC="$ROOT_DIR/scripts/launcher/Start OnCue.app"
APP_DEST="$APPS_DIR/OnCue.app"
if _run_logged bash "$ROOT_DIR/scripts/launcher/build_app.sh" && [[ -d "$APP_SRC" ]]; then
    mkdir -p "$APPS_DIR" "$CONFIG_DIR"
    # ditto overwrites in place, so a re-run replaces the app without deleting anything.
    if _run_logged ditto "$APP_SRC" "$APP_DEST" && _run_logged codesign --verify --strict "$APP_DEST"; then
        printf '%s' "$ROOT_DIR" > "$CONFIG_DIR/repo-path.tmp"
        mv "$CONFIG_DIR/repo-path.tmp" "$CONFIG_DIR/repo-path"
        _green "OnCue app installed: $APP_DEST"
    else
        APP_REASON="copying the app to $APPS_DIR failed (see the log)."
        _warn "Could not install the app. Open the one in $APP_SRC instead."
    fi
else
    APP_REASON="building the app failed (see the log)."
    _warn "App build failed. Re-run it with: bash scripts/launcher/build_app.sh"
fi

# ── Status block ─────────────────────────────────────────────────────────────
NOT_OK=0
_status_line() {
    # $1 label, $2 reason ("" = OK), $3 fix
    if [[ -z "$2" ]]; then
        printf '  %-14s OK\n' "$1"
    else
        NOT_OK=$((NOT_OK + 1))
        printf '  %-14s NOT OK: %s\n%-17sFix: %s\n' "$1" "$2" "" "$3"
    fi
}

# Verify against the disk, not against what the steps believed happened.
if [[ -z "$TRANSCRIBER_REASON" ]]; then
    if [[ ! -x "$WHISPER_CLI" || ! -x "$WHISPER_SERVER" ]]; then
        TRANSCRIBER_REASON="the whisper.cpp program is not built."
    elif [[ ! -f "$WHISPER_MODEL" ]]; then
        TRANSCRIBER_REASON="the speech model is not downloaded."
    fi
fi
if [[ -z "$AUDIO_REASON" && ! -x "$AUDIOTEE_BIN" ]]; then
    AUDIO_REASON="AudioTee is not built."
fi
if [[ -z "$APP_REASON" && ! -f "$CONFIG_DIR/repo-path" ]]; then
    APP_REASON="the app does not know where OnCue lives."
fi

echo ""
echo "================================="
echo "Status"
if [[ "$PREBUILT" == true ]]; then
    _status_line "Transcriber" "$TRANSCRIBER_REASON" "run the one-line install command again (it resumes and needs an internet connection)"
    _status_line "Audio capture" "$AUDIO_REASON" "run the one-line install command again"
    _status_line "App" "$APP_REASON" "run the one-line install command again"
else
    _status_line "Transcriber" "$TRANSCRIBER_REASON" "bash scripts/install_whisper_cpp.sh (needs cmake and an internet connection)"
    _status_line "Audio capture" "$AUDIO_REASON" "swift build -c release --package-path vendor/audiotee, then re-run scripts/install.sh"
    _status_line "App" "$APP_REASON" "bash scripts/install.sh"
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
if [[ "$NOT_OK" -eq 0 ]]; then
    _green "OnCue is installed."
else
    _warn "OnCue is installed, but $NOT_OK item(s) above need attention."
fi
echo ""
echo "  Open OnCue from Launchpad, or double-click ~/Applications/OnCue.app"
echo "  The first time, macOS asks for permission to use the microphone and to"
echo "  record audio. Click Allow."
echo ""
echo "  An AI provider for summaries is optional. See INSTALL.md."
echo "  Problems? Send this file: $LOG_FILE"
echo ""
