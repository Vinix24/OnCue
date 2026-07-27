# Module 2 — Live Transcriber

Module 2 provides real-time transcription with speaker attribution using a local
whisper.cpp engine (whisper.cpp-direct by default) and publishes transcript events
over the WebSocket hub.

## Architecture Overview

Pipeline (runtime order):
- Dual audio capture: mic stream maps to `self`, system audio stream maps to `prospect`
- Each stream is buffered by an `AudioBufferer` (Silero VAD + RMS gate mark segment boundaries)
- Ready speech chunks are pushed onto a shared priority queue (`SharedInferenceQueue`)
- A single `InferenceWorker` owns one `TranscriptionBackend` and transcribes chunks sequentially
- Transcript payloads are normalized into `TranscriptEvent`s and published to `/ws/transcript`

Components and locations:
- Transcript event model + normalization: `src/sales_copilot/modules/transcriber/engine.py`
- Audio bufferer (shared-queue path): `src/sales_copilot/modules/transcriber/audio_bufferer.py`
- Shared inference queue: `src/sales_copilot/modules/transcriber/inference_queue.py`
- Inference worker (owns the single backend + hub connection): `src/sales_copilot/modules/transcriber/inference_worker.py`
- Direct whisper engine (legacy dual-engine path, `TRANSCRIBER_SHARED_QUEUE=false`): `src/sales_copilot/modules/transcriber/whisper_direct.py`
- Transcription backends: `src/sales_copilot/modules/transcriber/backends/` (`whisper_cpp_backend.py`, `mlx_backend.py`, `parakeet_backend.py`, `groq_backend.py`)
- Transcriber module runner: `src/sales_copilot/modules/transcriber/__main__.py`
- WebSocket hub: `src/sales_copilot/websocket/hub.py`

## Transcription Engine

The default engine is whisper.cpp-direct: the module captures audio itself, buffers it,
and runs a local `whisper.cpp` build on each speech chunk. There is no separate
transcription server to launch — inference is managed in-process. A persistent
`whisper-server` fast path (`WHISPER_CPP_SERVER_ENABLED=true`) keeps the model resident so
each chunk skips the model reload; it falls back automatically to the one-shot
`whisper-cli` binary if the server cannot start.

Selectable backends (via `WHISPER_BACKEND`):
- `whisper.cpp` (default, on-device) — built by `scripts/install.sh`
- `mlx-whisper` (experimental, Apple Silicon native fallback)
- `parakeet` (optional, Apple Silicon; needs `pip install '.[parakeet]'` + ffmpeg)
- `groq` (cloud, opt-in) — every audio chunk is uploaded to Groq as a WAV; never the
  default, not for sensitive calls; requires `GROQ_API_KEY`

Build the whisper.cpp backend and fetch the GGML model:

```bash
./scripts/install.sh
```

## Configuration (.env)

Transcriber:
- `WHISPER_BACKEND` (default `whisper.cpp`; `mlx-whisper` experimental fallback, `parakeet` optional, `groq` cloud opt-in)
- `WHISPER_MODEL` (default `large-v3-turbo`)
- `WHISPER_LANGUAGE` (default `nl`; follows `CALL_LANGUAGE` when unset)
- `WHISPER_CPP_BINARY` (default `./vendor/whisper.cpp/build/bin/whisper-cli`)
- `WHISPER_CPP_MODEL_PATH` (default `./vendor/whisper.cpp/models/ggml-large-v3-turbo.bin`)
- `WHISPER_CPP_SERVER_ENABLED` (default `true` — persistent model-resident fast path)
- `DIARIZATION_BACKEND` (default `sortformer`)
- `MIN_SPEAKERS` / `MAX_SPEAKERS` (default 2)
- `TRANSCRIBER_SHARED_QUEUE` (default `true` — single-backend priority-queue path; `false` = legacy dual-engine)

The buffering / latency knobs (`WHISPER_MAX_BUFFER_SECONDS`, `WHISPER_SILENCE_GAP_SECONDS`,
`WHISPER_MIN_SEGMENT_SECONDS`, `WHISPER_PRE_ROLL_MS`, and related) are documented inline in
`.env.example`.

WebSocket hub:
- `WS_HUB_HOST` (default `127.0.0.1`)
- `WS_HUB_PORT` (default 8760)

## Running Module 2

```bash
PYTHONPATH=src python -m sales_copilot.modules.transcriber
```

The module prints a startup banner, starts the WebSocket hub if needed, captures mic +
system audio, and pushes transcript events to `/ws/transcript`. No separate transcription
server needs to be started.

## WebSocket Schema

Channel: `/ws/transcript` — exact schema and the canonical `TranscriptEvent`
dataclass are normative in [MODULE2_CONTRACT.md](MODULE2_CONTRACT.md).

## Speaker Mapping

- Mic stream maps to `self`.
- System audio stream maps to `prospect`.
- In the BlackHole single-stream fallback (no separate mic stream), all speech maps to
  `SINGLE_STREAM_SPEAKER_DEFAULT` (default `prospect`).

## Hardware Requirements

- Apple Silicon recommended. `large-v3-turbo` needs ~1.6 GB RAM and ~5s warmup.
- CPU-only machines run whisper.cpp on CPU threads (`WHISPER_CPP_THREADS`); inference is slower.

## Limitations

- Speaker mapping is deterministic but not identity-aware; it uses audio-source (mic vs. system) alignment.
- Transcript events are not persisted (handled by Module 4).

## Certification Evidence (PR-25)

- CI gate: `bash scripts/ci.sh`
- Standalone run: `python -m sales_copilot.modules.transcriber`
- Unit tests: `tests/test_transcriber.py`
