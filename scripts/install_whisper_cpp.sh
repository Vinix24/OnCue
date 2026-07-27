#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="$ROOT_DIR/vendor/whisper.cpp"
BUILD_DIR="$VENDOR_DIR/build"
BIN_PATH="$BUILD_DIR/bin/whisper-cli"
SERVER_PATH="$BUILD_DIR/bin/whisper-server"
MODEL_PATH="$VENDOR_DIR/models/ggml-large-v3-turbo.bin"

mkdir -p "$ROOT_DIR/vendor"

# Ensure pinned vendor source is present from submodule config.
git -C "$ROOT_DIR" submodule update --init --recursive vendor/whisper.cpp

cmake -S "$VENDOR_DIR" -B "$BUILD_DIR" -DWHISPER_METAL=ON
# Build both the one-shot CLI and the persistent server. The server keeps the
# model resident so live transcription skips the ~1.6GB per-chunk reload.
cmake --build "$BUILD_DIR" --config Release --target whisper-cli whisper-server

bash "$VENDOR_DIR/models/download-ggml-model.sh" large-v3-turbo
"$BIN_PATH" --help >/dev/null
"$SERVER_PATH" --help >/dev/null

if [ ! -f "$SERVER_PATH" ]; then
  echo "ERROR: expected whisper-server not found at $SERVER_PATH" >&2
  exit 1
fi

if [ ! -f "$MODEL_PATH" ]; then
  echo "ERROR: expected model not found at $MODEL_PATH" >&2
  exit 1
fi

echo "whisper.cpp installed successfully:"
echo "  Binary: $BIN_PATH"
echo "  Server: $SERVER_PATH"
echo "  Model:  $MODEL_PATH"
