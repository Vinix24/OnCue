#!/usr/bin/env bash
# OnCue — non-technical installer
# Idempotent: safe to run multiple times.
# Flags: --auto-yes  skip all interactive prompts (used by Setup .app)
set -euo pipefail

# ── Colour helpers ──────────────────────────────────────────────────────────
_blue()  { printf "\033[34m▸\033[0m %s\n" "$*"; }
_green() { printf "\033[32m✓\033[0m %s\n" "$*"; }
_warn()  { printf "\033[33m⚠\033[0m %s\n" "$*" >&2; }
_fail()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ── Parse flags ──────────────────────────────────────────────────────────────
AUTO_YES=false
for _arg in "$@"; do
    [[ "$_arg" == "--auto-yes" ]] && AUTO_YES=true
done

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

echo ""
echo "OnCue — installer"
echo "================================="
echo ""

# ── 1. Platform guard ───────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    _fail "This installer only supports macOS. Linux support is planned."
fi

if [[ "$(uname -m)" != "arm64" ]]; then
    _warn "Apple Silicon (M1/M2/M3) recommended. Intel Macs may have limited MLX support."
fi

MACOS_VER=$(sw_vers -productVersion)
MACOS_MAJOR=$(echo "$MACOS_VER" | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 13 ]]; then
    _fail "macOS 13 (Ventura) or newer required. You have: $MACOS_VER"
fi
_green "macOS $MACOS_VER — OK"

# ── 2. Homebrew ─────────────────────────────────────────────────────────────
# Homebrew provides Python, cmake, ffmpeg and the optional BlackHole cask. When
# it is missing, offer to run the official installer (the same one
# scripts/first-run.sh uses) instead of dead-ending — the non-technical Setup
# .app drives this script with --auto-yes and can't act on a "do it yourself"
# message.
_brew_install_url="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
if ! command -v brew &>/dev/null; then
    if _confirm "Homebrew not found. Install it now (official Homebrew installer)?"; then
        _blue "Installing Homebrew — this can take 5-10 minutes and may ask for your macOS password..."
        # NONINTERACTIVE=1 skips the installer's "press RETURN" prompt so the
        # Setup .app (--auto-yes) doesn't hang; an attended run behaves normally.
        if [[ "$AUTO_YES" == true ]]; then
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL "$_brew_install_url")" \
                || _warn "Homebrew installer exited non-zero — verifying below."
        else
            /bin/bash -c "$(curl -fsSL "$_brew_install_url")" \
                || _warn "Homebrew installer exited non-zero — verifying below."
        fi
        # Put brew on PATH for the rest of this run (Apple Silicon + Intel paths).
        if [[ -x /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        command -v brew &>/dev/null || _fail "Homebrew install did not complete. Install it manually, then re-run this installer:
  /bin/bash -c \"\$(curl -fsSL $_brew_install_url)\""
    else
        _fail "Homebrew is required. Install it, then re-run this installer:
  /bin/bash -c \"\$(curl -fsSL $_brew_install_url)\""
    fi
fi
_green "Homebrew found: $(brew --version | head -1)"

# ── 3. Python 3.11+ ─────────────────────────────────────────────────────────
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    _blue "Python 3.11+ not found — installing via Homebrew..."
    brew install python@3.11
    PYTHON_BIN="$(brew --prefix)/bin/python3.11"
fi

PY_VER=$("$PYTHON_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
_green "Python $PY_VER found at $PYTHON_BIN"

# ── 4. git ───────────────────────────────────────────────────────────────────
if ! command -v git &>/dev/null; then
    _fail "git not found. Install Xcode command line tools:
  xcode-select --install"
fi
_green "git $(git --version | awk '{print $3}')"

# ── 5. Virtual environment ───────────────────────────────────────────────────
VENV_DIR="$ROOT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    _blue "Creating virtual environment at .venv..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    _green "Virtual environment created"
else
    _green "Virtual environment already exists at .venv"
fi

PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

# ── 6. Install Python dependencies ──────────────────────────────────────────
_blue "Installing Python dependencies (pip install -e .[smart])..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -e ".[smart]"
_green "Python dependencies installed"

# ── 7. AudioTee (OSS default whole-system tap) ───────────────────────────────
# AudioTee taps whole-system audio (Teams/Zoom/Meet, no manual routing). The
# Swift source is vendored at vendor/audiotee/ (MIT, upstream:
# github.com/makeusabrew/audiotee); build it locally into bin/audiotee. Only
# the telephony call-tap (iPhone-relay/FaceTime) stays Sales Pro.
AUDIOTEE_BIN="$ROOT_DIR/bin/audiotee"
AUDIOTEE_SRC="$ROOT_DIR/vendor/audiotee"
if [[ -x "$AUDIOTEE_BIN" ]]; then
    _green "AudioTee binary already built: bin/audiotee"
elif [[ ! -f "$AUDIOTEE_SRC/Package.swift" ]]; then
    _warn "AudioTee source not found at vendor/audiotee — video-call capture continues via BlackHole."
elif ! command -v swift &>/dev/null; then
    _warn "Swift toolchain not found — install Xcode command line tools:
  xcode-select --install
  Then re-run this installer, or build manually:
  swift build -c release --package-path vendor/audiotee
  Video-call capture continues via BlackHole until built."
else
    _blue "Building AudioTee (swift build -c release) — first run takes a minute..."
    if swift build -c release --package-path "$AUDIOTEE_SRC"; then
        mkdir -p "$ROOT_DIR/bin"
        cp "$AUDIOTEE_SRC/.build/release/audiotee" "$AUDIOTEE_BIN"
        chmod +x "$AUDIOTEE_BIN"
        _green "AudioTee binary built: bin/audiotee"
        _blue "One-time macOS permission: the first run prompts for microphone /"
        _blue "audio-recording access (System Settings > Privacy & Security) — required"
        _blue "for the Core Audio process tap."
    else
        _warn "AudioTee build failed — video-call capture continues via BlackHole.
  Re-run manually: swift build -c release --package-path vendor/audiotee"
    fi
fi

# ── 8. whisper.cpp backend (binary + GGML model) ─────────────────────────────
# whisper.cpp is the default transcription backend. Its binary must be compiled
# from the vendored submodule and paired with a GGML model. Without it the
# transcriber degrades to the experimental mlx-whisper backend at runtime.
WHISPER_DIR="$ROOT_DIR/vendor/whisper.cpp"
WHISPER_CLI="$WHISPER_DIR/build/bin/whisper-cli"
WHISPER_MODEL_DIR="$WHISPER_DIR/models"

# 8a. Build the whisper.cpp binary + fetch its GGML model (idempotent).
# Delegates to the blessed build script (same cmake build + download-ggml-model.sh)
# so there is a single source of truth for the whisper.cpp toolchain.
if [[ -x "$WHISPER_CLI" ]]; then
    _green "whisper.cpp binary already built: vendor/whisper.cpp/build/bin/whisper-cli"
elif [[ ! -f "$WHISPER_DIR/CMakeLists.txt" ]]; then
    _warn "whisper.cpp submodule not initialised (vendor/whisper.cpp/CMakeLists.txt missing).
  Run: git submodule update --init --recursive
  Transcription will fall back to the experimental mlx-whisper backend until built."
elif ! command -v cmake &>/dev/null; then
    _warn "cmake not found — cannot build the whisper.cpp binary.
  Install cmake (brew install cmake), then: bash scripts/install_whisper_cpp.sh
  Transcription will fall back to the experimental mlx-whisper backend until built."
else
    _blue "Building whisper.cpp binary (cmake) + GGML model — first run takes a few minutes..."
    if bash "$ROOT_DIR/scripts/install_whisper_cpp.sh" >/dev/null; then
        _green "whisper.cpp binary built: vendor/whisper.cpp/build/bin/whisper-cli"
    else
        _warn "whisper.cpp build failed.
  Re-run manually: bash scripts/install_whisper_cpp.sh
  Transcription will fall back to the experimental mlx-whisper backend until built."
    fi
fi

# 8b. mlx-whisper fallback weights. The MLX model is cached separately (Hugging
# Face cache) so a runtime fallback to the experimental mlx-whisper backend stays
# usable when whisper.cpp could not be built. Skipped once a GGML model exists.
if [[ -d "$WHISPER_MODEL_DIR" ]] && ls "$WHISPER_MODEL_DIR"/ggml-*.bin &>/dev/null 2>&1; then
    _green "whisper.cpp GGML model present"
else
    _blue "Fetching mlx-whisper fallback weights (large-v3-turbo)..."
    "$PYTHON" scripts/install_whisper_model.py 2>/dev/null || \
        _warn "mlx-whisper weight download skipped — run 'python scripts/install_whisper_model.py' manually."
fi

# ── 9. .env configuration ────────────────────────────────────────────────────
ENV_FILE="$ROOT_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    _blue "Creating .env from .env.example..."
    cp "$ROOT_DIR/.env.example" "$ENV_FILE"
    _green ".env created — edit it to add your API key before first run"
else
    _green ".env already present"
fi

# ── 10. Local data directories ───────────────────────────────────────────────
mkdir -p "$ROOT_DIR/data/sessions"
_green "data/ directories ready"

# ── 11. Done — print next steps ─────────────────────────────────────────────
echo ""
echo "================================="
_green "Installation complete!"
echo ""
echo "Next steps:"
echo ""
echo "  1. Set your LLM API key in .env:"
echo "       nano .env"
echo "     Gemini (free tier): https://aistudio.google.com/app/apikey"
echo "     Groq   (free tier): https://console.groq.com"
echo ""
echo "  2. (Optional) BlackHole audio routing — only for the manual blackhole"
echo "     fallback; AudioTee whole-system capture is the default:"
echo "     brew install --cask blackhole-2ch (see docs/BETA_TESTER_GUIDE.md)"
echo ""
echo "  3. Start the copilot:"
echo "       .venv/bin/sales-copilot start"
echo "     Or double-click: scripts/launcher/Start OnCue.app"
echo ""
echo "  4. Open your browser at:"
echo "       Dashboard  → dashboard/index.html"
echo "       Slides     → presentation/index.html (share this screen with prospect)"
echo ""
echo "  Full guide: docs/BETA_TESTER_GUIDE.md"
echo "  Problems?   docs/TROUBLESHOOTING.md"
echo ""
