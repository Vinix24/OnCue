#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_PATH="$ROOT_DIR/data/logs/runtime.log"
PID_PATH="$ROOT_DIR/data/runtime.pid"
PREV_OUTPUT_PATH="$ROOT_DIR/data/previous_output_device.txt"
TARGET_OUTPUT="OnCue Output"

usage() {
  cat <<EOF
Usage: bash scripts/start.sh [--help]

Starts OnCue in background, switches output device to "$TARGET_OUTPUT",
opens dashboard + presentation in the default browser, and tails recent logs.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

mkdir -p "$ROOT_DIR/data/logs"

"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/bust_cache.py"

if command -v SwitchAudioSource >/dev/null 2>&1; then
  prev_output="$(SwitchAudioSource -c -t output || true)"
  if [[ -n "$prev_output" ]]; then
    echo "$prev_output" > "$PREV_OUTPUT_PATH"
  fi

  if SwitchAudioSource -a | grep -Fqx "$TARGET_OUTPUT"; then
    SwitchAudioSource -s "$TARGET_OUTPUT" -t output
    echo "Switched output to: $TARGET_OUTPUT"
  else
    echo "Warning: $TARGET_OUTPUT not found; leaving current output unchanged."
  fi
else
  echo "Warning: SwitchAudioSource not installed; skipping output switch."
fi

"$ROOT_DIR/.venv/bin/python" -m sales_copilot > "$LOG_PATH" 2>&1 &
pid=$!
echo "$pid" > "$PID_PATH"

echo "OnCue started with PID: $pid"
sleep 5

echo "--- Last log lines ---"
tail -n 40 "$LOG_PATH" || true

open "$ROOT_DIR/dashboard/index.html"
open "$ROOT_DIR/presentation/index.html"

echo "Run scripts/stop.sh to stop."
