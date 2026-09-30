# Module 1 — Talk Time Coaching

Module 1 provides local talk-time coaching using real-time audio capture, VAD, a tracker, and WebSocket publishing. It is designed to run entirely on localhost and integrate with the dashboard UI over WebSocket channels.

## Architecture Overview

Pipeline (runtime order):
- Audio capture (mic + optional system audio). System-audio capture is
  platform-specific behind the shared `AudioStream` protocol: `AudioTeeStream`
  (macOS, default) or `WasapiLoopbackStream` (Windows) —
  see `capture.py :: normalize_capture_method()`.
- VAD processing per stream
- Talk-time tracker (rolling + cumulative state, monologue detection)
- WebSocket publisher to hub channels

Components and locations:
- AudioCapture: `src/sales_copilot/audio/capture.py`
- VADProcessor: `src/sales_copilot/modules/talk_time/vad.py`
- TalkTimeTracker: `src/sales_copilot/modules/talk_time/tracker.py`
- TalkTimePublisher: `src/sales_copilot/modules/talk_time/publisher.py`
- WebSocketHub: `src/sales_copilot/websocket/hub.py`

## Configuration (.env)

Talk-time and thresholds:
- `ROLLING_WINDOW_SECONDS` (default 120)
- `MONOLOGUE_WARNING_SECONDS` (default 76)
- `DISCOVERY_TARGET_SELF` (default 0.35)
- `PITCH_TARGET_SELF` (default 0.65)
- `CLOSING_TARGET_SELF` (default 0.45)
- `RATIO_AMBER_THRESHOLD` (default 0.05)
- `RATIO_RED_THRESHOLD` (default 0.10)
- `COACHING_UPDATE_INTERVAL_MS` (default 5000)
- `PERCENTAGE_UPDATE_INTERVAL_MS` (default 15000)

Audio capture:
- `AUDIO_CAPTURE_METHOD` (`audiotee` default on macOS, `wasapi` default on Windows; `blackhole`, `mic`, and `replay` also available. `normalize_capture_method()` corrects a mismatched value for the current platform rather than raising.)
- `AUDIO_SAMPLE_RATE` (default 16000)
- `AUDIO_CHANNELS` (default 1)
- `TARGET_PROCESS_NAME` (macOS legacy per-process path only; unused by the default `audiotee` path, which taps the whole system output — `tap_all`, with no meeting-app detection. Not used on Windows — `WasapiLoopbackStream` captures the whole default render endpoint, no target process needed.)
- `AUDIOTEE_BINARY_PATH` (macOS only, default `./bin/audiotee`)

WebSocket hub:
- `WS_HUB_HOST` (default `127.0.0.1`)
- `WS_HUB_PORT` (default 8760)

## Running Module 1

From repo root (macOS/Linux shell):

```bash
PYTHONPATH=src python -m sales_copilot.modules.talk_time
```

On Windows (PowerShell), set `PYTHONPATH` as a separate step since the
`VAR=value cmd` shell syntax above is not valid there:

```powershell
$env:PYTHONPATH = "src"
python -m sales_copilot.modules.talk_time
```

Startup banner prints configuration, then starts the WebSocket hub and publisher. The module reads mic audio and optionally system audio (AudioTee on macOS, WASAPI loopback on Windows) and emits talk-time and coaching updates.

## Data Models & WebSocket Schemas

Normative spec (exact field types, Protocol interfaces, and JSON schemas for
`/ws/talk-time`, `/ws/coaching`, `/ws/phase`): [MODULE1_CONTRACT.md](MODULE1_CONTRACT.md).

## Limitations
- Requires a valid audio input device for `mic` capture.
- `audiotee` (macOS) requires a running target process name and the AudioTee binary — only for the legacy per-process path; the default `tap_all` path needs neither.
- `wasapi` (Windows) requires a default audio output/render device and the `soundcard` package (`pip install .[windows]`); it cannot target or exclude a specific process, so there is no Windows equivalent to the macOS telephony-tap or per-process exclusion.
- WebSocket hub binds to localhost only; no remote access by design.
- Deprecated warnings from `websockets`/`uvicorn` are expected until upstream updates.

## Certification Evidence (PR-18)
- CI gate: `bash scripts/ci.sh`
- Standalone run: `python -m sales_copilot.modules.talk_time`
- End-to-end tests: `tests/test_module1_e2e.py`
