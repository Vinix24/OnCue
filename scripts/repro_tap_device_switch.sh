#!/bin/bash
# Reproduction for audio-tap-stopt-met-leveren: does an output-device/route
# switch stop the AudioTee system-tap while it stays "attached", and does the
# bounded auto-reattach in monitor_tap_health() bring the signal back?
#
# SUPERVISED RUN ONLY. This script switches the Mac's audio OUTPUT device
# twice, which redirects whatever the operator is hearing. Do not run this
# during a real call, and only run it with the operator present -- it always
# restores the original output device on exit (including Ctrl-C), but a
# mid-call device flip is exactly the kind of disruption the underlying bug
# report is about.
#
#   bash scripts/repro_tap_device_switch.sh [target_output_device_name]
#
# With no argument, the script auto-picks the first available output device
# that is not the current default. Requires SwitchAudioSource (see
# docs/SETUP_EXPECTATIONS.md) and a built bin/audiotee (scripts/install.sh).
#
# Timeline: 20s on the original device (baseline, tap should carry signal),
# switch, 20s on the alternate device (this is where a route-triggered drop
# would show up), switch back, 15s to observe recovery. The system tap
# (tap_all AudioTee) and a real mic stream run for the whole 55s under the
# actual product monitor (monitor_tap_health), with the digital-silence
# threshold lowered so a drop shows up in this short window instead of
# needing the production 30s.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"
AUDIOTEE_BIN="$ROOT_DIR/bin/audiotee"
LOG_DIR="$ROOT_DIR/data/logs"
LOG_PATH="$LOG_DIR/repro_tap_device_switch.log"

BASELINE_SECONDS=20
SWITCHED_SECONDS=20
RECOVERY_SECONDS=15
TOTAL_SECONDS=$((BASELINE_SECONDS + SWITCHED_SECONDS + RECOVERY_SECONDS))

usage() {
  cat <<EOF
Usage: bash scripts/repro_tap_device_switch.sh [target_output_device_name]

Supervised reproduction for the tap-stops-delivering bug: plays a test file,
taps the system output, switches the output device mid-recording, switches
back, and reports whether the tap went to digital silence and whether the
product's auto-reattach recovered it. Always restores the original output
device on exit. Never run this unattended or during a real call.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v SwitchAudioSource >/dev/null 2>&1; then
  echo "SwitchAudioSource not installed (see docs/SETUP_EXPECTATIONS.md); cannot switch devices." >&2
  exit 1
fi
if [[ ! -x "$VENV_PY" ]]; then
  echo "No .venv at $ROOT_DIR/.venv; run scripts/install.sh first." >&2
  exit 1
fi
if [[ ! -x "$AUDIOTEE_BIN" ]]; then
  echo "bin/audiotee not built; run scripts/install.sh first." >&2
  exit 1
fi

ORIG_OUTPUT="$(SwitchAudioSource -c -t output)"
if [[ -z "$ORIG_OUTPUT" ]]; then
  echo "Could not read the current output device via SwitchAudioSource -c." >&2
  exit 1
fi

TARGET_OUTPUT="${1:-}"
if [[ -z "$TARGET_OUTPUT" ]]; then
  TARGET_OUTPUT="$(SwitchAudioSource -a -t output | grep -Fvx "$ORIG_OUTPUT" | head -n 1)"
fi
if [[ -z "$TARGET_OUTPUT" ]]; then
  echo "No alternate output device found to switch to. Pass one explicitly: $0 \"<device name>\"" >&2
  exit 1
fi

TEST_AUDIO_FILE="/System/Library/Sounds/Ping.aiff"
if [[ ! -f "$TEST_AUDIO_FILE" ]]; then
  echo "Test audio file not found: $TEST_AUDIO_FILE" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

PLAYBACK_PID=""
MONITOR_PID=""

cleanup() {
  # Always restore the original output device, even on error or Ctrl-C -- a
  # script that leaves the operator's audio on the wrong device is worse than
  # the bug it is trying to reproduce.
  if [[ -n "$PLAYBACK_PID" ]] && kill -0 "$PLAYBACK_PID" >/dev/null 2>&1; then
    kill "$PLAYBACK_PID" >/dev/null 2>&1 || true
    wait "$PLAYBACK_PID" 2>/dev/null || true
  fi
  if [[ -n "$MONITOR_PID" ]] && kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
    wait "$MONITOR_PID" 2>/dev/null || true
  fi
  SwitchAudioSource -s "$ORIG_OUTPUT" -t output >/dev/null 2>&1 || true
  echo "Restored audio output: $ORIG_OUTPUT"
}
trap cleanup EXIT

echo "Current output device : $ORIG_OUTPUT"
echo "Switching to           : $TARGET_OUTPUT (at ${BASELINE_SECONDS}s)"
echo "Test audio file        : $TEST_AUDIO_FILE"
echo "Log                    : $LOG_PATH"
echo "Total run time         : ${TOTAL_SECONDS}s"
echo

# Loop the test file for the whole run so there is always something for the
# system tap to carry, on whichever output device is currently selected.
(
  end=$((SECONDS + TOTAL_SECONDS + 2))
  while (( SECONDS < end )); do
    afplay "$TEST_AUDIO_FILE" || true
  done
) &
PLAYBACK_PID=$!

# The monitor under test: the real product code (AudioTeeStream tap_all=True
# for the system side, MicStream as the "peer" that keeps carrying signal,
# monitor_tap_health() for the bounded auto-reattach). The digital-silence
# threshold is lowered from the 30s production default so a real drop shows
# up inside this script's 20s switched window instead of needing much longer.
AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS=5 PYTHONPATH="$ROOT_DIR/src" "$VENV_PY" - "$TOTAL_SECONDS" > "$LOG_PATH" 2>&1 <<'PYEOF' &
import asyncio
import sys
import time

from sales_copilot.audio.capture import AudioConfig, AudioTeeStream, MicStream
from sales_copilot.audio.tap_health import monitor_tap_health

TOTAL_SECONDS = float(sys.argv[1])


async def main() -> None:
    config = AudioConfig()
    system = AudioTeeStream(config, tap_all=True)
    mic = MicStream(config)

    reattach_attempts = 0
    prospect_went_zero = False
    prospect_recovered_after_zero = False

    async def broadcast_warning(payload: dict) -> None:
        print(f"[warning][t={time.monotonic():.1f}] {payload}", flush=True)

    async def broadcast_marker(payload: dict) -> None:
        nonlocal reattach_attempts
        text = payload.get("text", "")
        if "aangehaakt" in text:
            reattach_attempts += 1
        print(f"[marker][t={time.monotonic():.1f}] {text}", flush=True)

    async def broadcast_status(payload: dict) -> None:
        nonlocal prospect_went_zero, prospect_recovered_after_zero
        print(f"[status][t={time.monotonic():.1f}] {payload}", flush=True)
        if payload.get("stream") != "prospect":
            return
        if payload.get("dashboard_state") != "attached_signal":
            prospect_went_zero = True
        elif prospect_went_zero:
            prospect_recovered_after_zero = True

    mic.start()
    system.start()
    try:
        stop_event = asyncio.Event()
        monitor_task = asyncio.create_task(
            monitor_tap_health(
                [(system, "prospect"), (mic, "self")],
                broadcast_warning,
                stop_event,
                interval_seconds=2.0,
                no_frames_warn_seconds=5.0,
                silent_warn_seconds=15.0,
                broadcast_marker=broadcast_marker,
                broadcast_status=broadcast_status,
            )
        )
        await asyncio.sleep(TOTAL_SECONDS)
        stop_event.set()
        await monitor_task
    finally:
        system.stop()
        mic.stop()

    print("=== VERDICT ===", flush=True)
    print(f"Tap gaf nullen na de wissel: {'JA' if prospect_went_zero else 'NEE'}", flush=True)
    print(f"Reattach-pogingen gezien: {reattach_attempts}", flush=True)
    print(
        f"Signaal hersteld na reattach/terug-wissel: "
        f"{'JA' if prospect_recovered_after_zero else 'NEE'}",
        flush=True,
    )


asyncio.run(main())
PYEOF
MONITOR_PID=$!

sleep "$BASELINE_SECONDS"
echo "[$(date '+%H:%M:%S')] Switching output: $ORIG_OUTPUT -> $TARGET_OUTPUT"
SwitchAudioSource -s "$TARGET_OUTPUT" -t output

sleep "$SWITCHED_SECONDS"
echo "[$(date '+%H:%M:%S')] Switching output back: $TARGET_OUTPUT -> $ORIG_OUTPUT"
SwitchAudioSource -s "$ORIG_OUTPUT" -t output

sleep "$RECOVERY_SECONDS"

wait "$MONITOR_PID"
MONITOR_PID=""
kill "$PLAYBACK_PID" >/dev/null 2>&1 || true
wait "$PLAYBACK_PID" 2>/dev/null || true
PLAYBACK_PID=""

echo
echo "--- verdict (from $LOG_PATH) ---"
awk '/=== VERDICT ===/{found=1} found' "$LOG_PATH"
