# Module 2 — Live Transcriber Architecture Contract (Source of Truth)

Module 2 provides real-time transcription with diarization and streams structured transcript events over the WebSocket hub. This contract defines boundaries, interfaces, schemas, and integration points for the live transcriber.

## Component Boundaries + File Locations

- Transcript event model + normalization — `src/sales_copilot/modules/transcriber/engine.py`
- Audio bufferer (VAD + buffering, shared-queue path) — `src/sales_copilot/modules/transcriber/audio_bufferer.py`
- Shared inference queue — `src/sales_copilot/modules/transcriber/inference_queue.py`
- Inference worker (owns the single backend + hub connection) — `src/sales_copilot/modules/transcriber/inference_worker.py`
- Direct whisper engine (legacy dual-engine path) — `src/sales_copilot/modules/transcriber/whisper_direct.py`
- Transcription backends — `src/sales_copilot/modules/transcriber/backends/` (`whisper_cpp_backend.py`, `mlx_backend.py`, `parakeet_backend.py`, `groq_backend.py`)
- WebSocket hub — `src/sales_copilot/websocket/hub.py`
- Audio capture (shared) — `src/sales_copilot/audio/capture.py`

## Interfaces + Responsibilities

### Canonical transcript event

`engine.py` owns the canonical `TranscriptEvent` and the payload-to-event normalization.
Every backend, regardless of implementation, ends up producing this event before it is
published to `/ws/transcript`:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

Speaker = Literal["self", "prospect"]

@dataclass(frozen=True)
class TranscriptEvent:
    type: Literal["transcript"]
    text: str
    speaker: Speaker
    start_ms: int
    end_ms: int
    is_final: bool
```

### Transcription backend Protocol

Backends implement the `TranscriptionBackend` Protocol in
`src/sales_copilot/modules/transcriber/backends/base.py` (`start`, `warmup`, `transcribe`,
`stop`). The default is whisper.cpp-direct; `mlx-whisper`, `parakeet`, and `groq` are
selectable via `WHISPER_BACKEND`.

Responsibilities:
- `AudioBufferer` runs the VAD + RMS gate and pushes complete speech chunks onto the shared queue; it does not hold a backend reference.
- `InferenceWorker` owns the single `TranscriptionBackend` instance, drains the queue, applies the hallucination filter, and publishes `TranscriptEvent`s to `/ws/transcript`.
- `engine.py` normalizes backend output into the canonical `TranscriptEvent` (timestamps in milliseconds) and maps the audio source (mic vs. system) to `self` / `prospect`.

## Backend Configuration Passthrough

The transcriber passes these settings (from `TranscriberConfig`) to the selected backend:
- `WHISPER_BACKEND`
- `WHISPER_MODEL`
- `WHISPER_LANGUAGE`
- `WHISPER_CPP_BINARY` / `WHISPER_CPP_MODEL_PATH`
- `DIARIZATION_BACKEND`
- `MIN_SPEAKERS`
- `MAX_SPEAKERS`

Diarization labels come from the diarization backend (`sortformer`) combined with the
dual-stream (mic vs. system) source mapping. The transcriber is responsible for mapping
those to `self` or `prospect`.

## Speaker Mapping Strategy

Mapping strategy must be deterministic and configurable:
- Prefer audio-source alignment: mic stream == `self`, system stream == `prospect`.
- If the diarization backend provides two speaker IDs, map the first observed ID to `self` and the second to `prospect`.
- If more than two speakers are detected, ignore additional speakers (return `None` from `map_speaker`).
- If mapping is unknown, drop the event (do not publish).

## WebSocket Message Schema (TTD.md Section 7)

Channel: `/ws/transcript`

```json
{
  "type": "transcript",
  "text": "string",
  "speaker": "self | prospect",
  "start_ms": 124500,
  "end_ms": 127800,
  "is_final": true
}
```

## Integration with Module 1 Audio Capture

- Reuse `DualAudioCapture` from `src/sales_copilot/audio/capture.py` for mic + system audio.
- Mic stream indicates `self` and system stream indicates `prospect` for mapping validation.
- The normalization/publish path is independent of capture transport and consumes backend output only.

## Error Handling Requirements

- Hub (`/ws/transcript`) disconnect during publish: exponential backoff reconnect (1s, 2s, 4s, max 30s).
- Timeout waiting for a transcription payload: emit a warning log and continue.
- Model loading failure: raise a clear `RuntimeError` with the backend/model name.
- `backend.transcribe()` raises: log a warning, drop the chunk, and continue the worker loop.
- Speaker mapping failure: drop event and log debug-level message.

## Non-goals

- No persistence of transcripts (handled by Module 4 or later).
- No translation or post-processing.
- No external network usage in the local (`whisper.cpp` / `mlx-whisper` / `parakeet`) backends; the opt-in `groq` backend uploads audio chunks to Groq only when explicitly selected.
