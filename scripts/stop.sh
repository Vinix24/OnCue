#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_PATH="$ROOT_DIR/data/runtime.pid"
PREV_OUTPUT_PATH="$ROOT_DIR/data/previous_output_device.txt"

usage() {
  cat <<EOF
Usage: bash scripts/stop.sh [--help]

Stops running OnCue process and restores previous output device when possible.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

stopped=false
if [[ -f "$PID_PATH" ]]; then
  pid="$(cat "$PID_PATH")"
  if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid" >/dev/null 2>&1 || true
    stopped=true
  fi
  rm -f "$PID_PATH"
fi

pkill -f "python -m sales_copilot" >/dev/null 2>&1 || true

if command -v SwitchAudioSource >/dev/null 2>&1 && [[ -f "$PREV_OUTPUT_PATH" ]]; then
  prev_output="$(cat "$PREV_OUTPUT_PATH")"
  if [[ -n "$prev_output" ]]; then
    SwitchAudioSource -s "$prev_output" -t output >/dev/null 2>&1 || true
    echo "Restored audio output: $prev_output"
  fi
  rm -f "$PREV_OUTPUT_PATH"
fi

if [[ "$stopped" == "true" ]]; then
  echo "OnCue stopped."
else
  echo "No tracked OnCue process was running (cleanup complete)."
fi
