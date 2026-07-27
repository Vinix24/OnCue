#!/bin/bash
set -euo pipefail

echo "=== OnCue — Setup ==="
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
echo "Python version: $PYTHON_VERSION"
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required.")
PY

# Check macOS and Apple Silicon
if [[ "$(uname)" != "Darwin" ]]; then
    echo "WARNING: This project is designed for macOS. Some features may not work."
fi

if [[ "$(uname -m)" != "arm64" ]]; then
    echo "WARNING: Apple Silicon (arm64) recommended for MLX-Whisper backend."
fi

# Create virtual environment
echo ""
echo "--- Creating virtual environment ---"
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
echo ""
echo "--- Installing dependencies ---"
pip install -e ".[dev]"

# AudioTee whole-system tap is the OSS default (Teams/Zoom/Meet, no manual
# routing). The Swift source is vendored at vendor/audiotee/ (MIT, upstream:
# github.com/makeusabrew/audiotee) and built locally into bin/audiotee. Only
# the telephony call-tap (iPhone-relay/FaceTime) stays Sales Pro.
echo ""
echo "--- AudioTee ---"
AUDIOTEE_BIN="bin/audiotee"
if [ -x "$AUDIOTEE_BIN" ]; then
    echo "AudioTee binary already built: $AUDIOTEE_BIN"
elif [[ "$(uname)" != "Darwin" ]]; then
    echo "AudioTee is macOS-only. Set AUDIO_CAPTURE_METHOD=blackhole (WASAPI on Windows) in .env."
elif ! command -v swift >/dev/null 2>&1; then
    echo "WARNING: Swift toolchain not found — install Xcode command line tools:"
    echo "         xcode-select --install"
    echo "         Then re-run setup, or build manually:"
    echo "         swift build -c release --package-path vendor/audiotee"
    echo "         Falling back to AUDIO_CAPTURE_METHOD=blackhole in .env until built."
else
    echo "Building AudioTee (swift build -c release) — first run takes a minute..."
    if swift build -c release --package-path vendor/audiotee; then
        mkdir -p bin
        cp vendor/audiotee/.build/release/audiotee "$AUDIOTEE_BIN"
        chmod +x "$AUDIOTEE_BIN"
        echo "AudioTee binary built: $AUDIOTEE_BIN"
        echo "One-time macOS permission: the first run prompts for microphone /"
        echo "audio-recording access (System Settings > Privacy & Security) — required"
        echo "for the Core Audio process tap."
    else
        echo "WARNING: AudioTee build failed — falling back to AUDIO_CAPTURE_METHOD=blackhole."
        echo "         Re-run manually: swift build -c release --package-path vendor/audiotee"
    fi
fi

# whisper.cpp is the default transcription backend; compile its binary + model.
echo ""
echo "--- whisper.cpp backend ---"
WHISPER_CLI="vendor/whisper.cpp/build/bin/whisper-cli"
if [ -x "$WHISPER_CLI" ]; then
    echo "whisper.cpp binary already built: $WHISPER_CLI"
elif [ -f "vendor/whisper.cpp/CMakeLists.txt" ] && command -v cmake >/dev/null 2>&1; then
    echo "Building whisper.cpp binary (cmake) + GGML model — first run takes a few minutes..."
    if bash scripts/install_whisper_cpp.sh >/dev/null; then
        echo "whisper.cpp binary built: $WHISPER_CLI"
    else
        echo "WARNING: whisper.cpp build failed — transcription falls back to mlx-whisper."
        echo "         Re-run manually: bash scripts/install_whisper_cpp.sh"
    fi
else
    echo "WARNING: cmake or whisper.cpp submodule missing — transcription falls back to mlx-whisper."
    echo "         Init submodule: git submodule update --init --recursive (and: brew install cmake)"
fi

# Create .env if not exists
if [ ! -f .env ]; then
    echo ""
    echo "--- Creating .env from .env.example ---"
    cp .env.example .env
    echo "Created .env — edit it to add your API keys."
fi

# Enable the version-controlled git hooks (gitleaks pre-commit + local CI pre-push)
if [ -d .githooks ]; then
    git config core.hooksPath .githooks
    chmod +x .githooks/* 2>/dev/null || true
    echo "--- Git hooks enabled (.githooks: pre-commit gitleaks, pre-push CI gate) ---"
fi

# Create data directories
mkdir -p data/sessions

# Initialize SQLite database
echo ""
echo "--- Initializing local database ---"
python scripts/seed_cases.py

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API key (GEMINI_API_KEY or GROQ_API_KEY)"
echo "  2. Test audio: python scripts/test_audio.py"
echo "  3. Run: python -m sales_copilot"
