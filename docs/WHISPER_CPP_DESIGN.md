# whisper.cpp Integration Design

**Project:** OnCue
**Date:** 2026-04-16
**Status:** Implemented (2026-07)
**Audience:** Developers implementing Module 2 transcription changes

> **Status: Implemented — WhisperLiveKit removed; whisper.cpp-direct is the shipped engine (2026-07).** This document is the original design proposal, kept for history. Where it frames WhisperLiveKit as the "current" transport, that reflects the pre-migration state; WhisperLiveKit is no longer part of the codebase.

## 1. Goal

Add `whisper.cpp` as a first-class transcription backend in the current architecture, selectable from:

- global `.env`
- setup menu before a call
- preset payloads

The integration must preserve the existing downstream contract:

- transcript events still publish to `/ws/transcript`
- dashboard transcript panel remains unchanged
- pain point detection keeps consuming transcript events
- post-call reports keep consuming transcript events

This is an additive design. Existing backends such as `mlx-whisper` and future cloud backends stay possible.

## 2. Why This Design

The current architecture already separates:

- audio capture
- transcription transport
- transcript normalization/publication

That means `whisper.cpp` should replace only the current WhisperLiveKit-specific transport layer, not the entire transcription module.

The right design is therefore:

- keep `TranscriberEngine` as the canonical publisher/normalizer
- add a backend adapter layer
- route runtime selection through config and setup UI

## 3. Target User-Facing Choice

The setup screen should expose a new field:

- `Transcript backend`

Initial options:

- `mlx-whisper (local)`
- `whisper.cpp (local)`
- `cloud` (reserved for later)

Recommended labels in UI:

- `MLX Whisper`
- `whisper.cpp`
- `Cloud`

The call payload should include:

```json
{
  "transcript": {
    "backend": "whisper.cpp"
  }
}
```

Backward compatibility:

- if `transcript.backend` is absent, fall back to `.env`
- if `.env` is absent, default to `whisper.cpp` (with an automatic mlx-whisper fallback when the binary is missing)

## 4. Architecture Change

### Current

```text
Audio Capture -> AudioBridge -> WhisperLiveKit -> TranscriberEngine -> /ws/transcript
```

### Proposed

```text
Audio Capture -> Transcription Adapter -> TranscriberEngine -> /ws/transcript
```

### Modularity Boundary

`whisper.cpp` is designed as a modular backend inside Module 2, not as a standalone subsystem outside the app.

What is replaceable:

- the transcription backend implementation
- backend-specific transport/process logic
- backend-specific runtime configuration

What remains shared:

- audio capture layer
- canonical transcript event model
- transcript publication to `/ws/transcript`
- downstream consumers such as dashboard, detector, and reports

This means developers can swap `WhisperLiveKitAdapter` for `WhisperCppAdapter` without changing the rest of the system contract.

### Component Diagram

```text
                   Module 2: Transcription

   ┌────────────────────┐
   │   Audio Capture    │
   │ Mic / System input │
   └─────────┬──────────┘
             │ shared
             ▼
   ┌────────────────────┐
   │ Backend Interface  │
   │ TranscriptionAdapter│
   └───────┬─────┬──────┘
           │     │
   selectable    selectable
           │     │
           ▼     ▼
  ┌──────────────────┐   ┌──────────────────┐
  │ WhisperLiveKit   │   │ whisper.cpp      │
  │ Adapter          │   │ Adapter          │
  └────────┬─────────┘   └────────┬─────────┘
           │ raw payloads         │ raw payloads
           └──────────┬───────────┘
                      ▼
           ┌────────────────────┐
           │ TranscriberEngine  │
           │ normalize + publish│
           └─────────┬──────────┘
                     ▼
              /ws/transcript
                     ▼
      Dashboard / Detector / Reports
```

Adapters:

- `WhisperLiveKitAdapter`
- `WhisperCppAdapter`
- later: `CloudWhisperAdapter`

### Key Principle

`TranscriberEngine` should stop knowing transport details of WhisperLiveKit.
It should become the backend-agnostic transcript event publisher.

## 5. New Internal Interfaces

Create a backend adapter protocol, for example in:

- `src/sales_copilot/modules/transcriber/backends/base.py`

Suggested interface:

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import numpy as np

TranscriptCallback = Callable[[dict[str, object], str | None], Awaitable[None]]


class TranscriptionBackend(Protocol):
    async def start(self, stop_event: asyncio.Event) -> None: ...
    async def stop(self) -> None: ...
```
```

Design rule:

- backend adapters do not publish to the hub directly
- backend adapters emit raw transcript payloads via callback
- `TranscriberEngine` remains the single place that converts raw backend output into canonical transcript events

## 6. Canonical Transcript Contract

All backends must end up producing this event:

```json
{
  "type": "transcript",
  "text": "we zitten echt uren aan zo'n offerte te werken",
  "speaker": "self",
  "start_ms": 124500,
  "end_ms": 127800,
  "is_final": true
}
```

Rules:

- `speaker` must be `"self"` or `"prospect"`
- `start_ms` and `end_ms` are milliseconds since call start
- `is_final=false` is allowed for partial/interim results
- if a backend cannot provide accurate timestamps, it may emit estimated timestamps

## 7. whisper.cpp Integration Strategy

## 7.1 Recommended Mode

Do not integrate `whisper.cpp` using the demo `examples/stream/stream.cpp` as-is.
Use `whisper.cpp` as an inference engine under our own buffering logic.

Recommended implementation path:

- capture audio using existing Python audio streams
- buffer per speaker in Python
- flush every N milliseconds or when VAD indicates speech boundary
- call `whisper.cpp` subprocess on the buffered chunk
- parse text output
- emit normalized transcript event

This is more stable and easier to control than trying to drive the `stream` example as a black box.

## 7.2 Execution Mode

Recommended first version:

- subprocess per call session
- invoke `whisper.cpp` CLI on short wav chunks written to a temp directory

Why this first:

- simplest failure isolation
- easiest logging and reproducibility
- easiest to test chunk-by-chunk
- avoids complex stdin/stdout streaming state

Possible later optimization:

- keep a long-lived subprocess alive
- stream PCM over stdin if benchmarking proves it matters

## 7.3 Buffering Model

For each active stream:

- collect PCM float32 frames from existing `AudioStream`
- convert to mono 16kHz float32 or int16
- keep rolling buffer of `3s` default
- flush on either:
  - `buffer_duration >= 3s`
  - or `speech ended + min_chunk_duration reached`

Suggested defaults:

- `WHISPER_CPP_CHUNK_MS=3000`
- `WHISPER_CPP_MIN_CHUNK_MS=1200`
- `WHISPER_CPP_MAX_CHUNK_MS=6000`
- `WHISPER_CPP_SILENCE_FLUSH_MS=400`

## 7.4 Speaker Model

For dual-stream capture:

- mic stream maps to `self`
- system stream maps to `prospect`
- no diarization required

For single-stream fallback:

- not in scope for first `whisper.cpp` implementation
- keep current single-stream fallback on existing backend only

Reason:

- single-stream diarization adds a separate problem
- first goal is a stable dual-stream local transcription path

## 7.5 Suggested File Layout

New files:

- `src/sales_copilot/modules/transcriber/backends/base.py`
- `src/sales_copilot/modules/transcriber/backends/wlk.py`
- `src/sales_copilot/modules/transcriber/backends/whisper_cpp.py`
- `src/sales_copilot/modules/transcriber/chunker.py`
- `src/sales_copilot/modules/transcriber/wav_utils.py`

Existing files to refactor:

- `src/sales_copilot/modules/transcriber/__main__.py`
- `src/sales_copilot/modules/transcriber/engine.py`
- `src/sales_copilot/core/config.py`
- `dashboard/index.html`
- `dashboard/js/setup.js`
- `config/presets.yaml`
- `.env.example`

## 8. Config Design

Extend `TranscriberConfig` with backend-specific fields.

Suggested config shape:

```python
@dataclass(frozen=True)
class TranscriberConfig:
    backend: str = "mlx-whisper"
    model: str = "large-v3"
    language: str = "nl"
    port: int = 8761
    system_port: int = 8762
    whisper_cpp_binary: str = "./vendor/whisper.cpp/build/bin/whisper-cli"
    whisper_cpp_model_path: str = "./vendor/whisper.cpp/models/ggml-large-v3.bin"
    whisper_cpp_chunk_ms: int = 3000
    whisper_cpp_min_chunk_ms: int = 1200
    whisper_cpp_max_chunk_ms: int = 6000
    whisper_cpp_threads: int = 4
    whisper_cpp_processors: str = "cpu"
    whisper_cpp_temperature: float = 0.0
```

Suggested `.env` keys:

```bash
WHISPER_BACKEND=whisper.cpp
WHISPER_CPP_BINARY=./vendor/whisper.cpp/build/bin/whisper-cli
WHISPER_CPP_MODEL_PATH=./vendor/whisper.cpp/models/ggml-large-v3.bin
WHISPER_CPP_CHUNK_MS=3000
WHISPER_CPP_MIN_CHUNK_MS=1200
WHISPER_CPP_MAX_CHUNK_MS=6000
WHISPER_CPP_THREADS=4
WHISPER_CPP_PROCESSORS=cpu
WHISPER_CPP_TEMPERATURE=0.0
```

Notes:

- `WHISPER_BACKEND` remains the global default
- setup-screen override should win for one call
- `whisper.cpp` should use explicit model path, not implicit discovery

## 9. Setup Screen Changes

## 9.1 UI

Add a new card or field under setup:

- label: `Transcript backend`
- control: `<select id="transcript-backend">`

Suggested options:

```html
<select id="transcript-backend">
  <option value="mlx-whisper">MLX Whisper</option>
  <option value="whisper.cpp">whisper.cpp</option>
  <option value="cloud">Cloud</option>
</select>
```

## 9.2 Payload Construction

Extend `collectConfig()` in `dashboard/js/setup.js`:

```json
{
  "transcript": {
    "backend": "whisper.cpp"
  }
}
```

## 9.3 Presets

Allow presets to include:

```yaml
transcript:
  backend: whisper.cpp
```

Backward compatibility:

- if absent, do not change current behavior

## 10. Orchestrator Parsing

Update `_parse_call_config()` in `src/sales_copilot/__main__.py` to accept:

```json
{
  "transcript": {
    "backend": "whisper.cpp"
  }
}
```

Store the override in `CallConfig`, for example:

```python
transcript_backend: str | None = None
```

Then inject that override into `TranscriberConfig` in `build_module_configs()`.

## 11. Backend Adapter Behavior

## 11.1 WhisperLiveKit Adapter

Move current WLK-specific bridge logic out of `__main__.py`.

Responsibilities:

- create audio bridges
- manage WLK websocket lifecycle
- emit raw transcript payloads to callback

## 11.2 whisper.cpp Adapter

Responsibilities:

- create mic/system audio streams using existing capture layer
- maintain chunk buffer per stream
- write chunk to temp `.wav`
- call subprocess
- parse stdout into text
- create raw payload for engine callback

Suggested raw payload shape from adapter to engine:

```python
{
    "text": "detected text",
    "start": 12.0,
    "end": 15.0,
    "is_final": True,
}
```

This lets `TranscriberEngine._payload_to_event()` stay reusable.

## 11.3 Subprocess Command

Representative command:

```bash
whisper-cli \
  -m ./models/ggml-large-v3.bin \
  -f /tmp/chunk.wav \
  -l nl \
  -t 4 \
  --no-context \
  --temperature 0
```

The exact flags depend on the installed build.
Keep flags centralized in one helper method so the binary can evolve without touching orchestration code.

## 11.4 Temp Files

Use one temp directory per call:

- `data/runtime/transcriber/<session-id>/`

Files:

- `self-0001.wav`
- `prospect-0001.wav`
- optional logs per chunk

Delete on graceful shutdown.
Retain only if debug mode is enabled.

## 12. Error Handling

The adapter must degrade predictably.

Rules:

- if `whisper.cpp` binary is missing, fail fast with clear startup error
- if subprocess returns non-zero, log stderr and skip the chunk
- if one speaker stream fails, other modules must continue
- if three consecutive chunks fail for the same speaker, emit warning to logs and to `/ws/coaching` only if desired later

Explicit non-goal for v1:

- no auto-restart of a broken binary mid-call beyond normal per-chunk subprocess isolation

## 13. Performance Expectations

For `whisper.cpp`, the practical latency is:

- capture buffering: `1.2s - 3s`
- inference: model/hardware dependent
- publish: negligible

Expected UX target for first release:

- stable transcript with `2s - 5s` delay

This is acceptable if the product goal is reliability over ultra-low latency.

## 14. Test Plan

## 14.1 Unit Tests

- `TranscriberConfig` reads `WHISPER_BACKEND=whisper.cpp`
- chunker flushes on max duration
- chunker flushes on speech-end condition
- wav writer outputs valid mono 16kHz wav
- subprocess parser extracts transcript text from known stdout
- adapter maps mic to `self` and system to `prospect`
- engine publishes canonical transcript event unchanged

## 14.2 Integration Tests

- mock `whisper.cpp` subprocess returns transcript for one chunk
- dual-stream adapter publishes separate `self` and `prospect` events to hub
- dashboard transcript panel renders both streams unchanged
- detector still consumes `/ws/transcript` events from whisper.cpp backend

## 14.3 Manual Validation

- start call with `MLX Whisper`
- start call with `whisper.cpp`
- switch presets and confirm backend selection persists
- long-running `15m` call with `whisper.cpp`
- silence periods do not spam repeated transcript rows
- prospect system audio via AudioTee (or the BlackHole fallback) still flows

## 15. Rollout Plan

### Phase 1

Backend abstraction only.

- refactor current WLK path behind adapter interface
- no UI change yet

### Phase 2

Add `whisper.cpp` local backend.

- subprocess chunk mode
- dual-stream only
- env-only selection

### Phase 3

Expose backend choice in setup menu and presets.

- add `Transcript backend` field
- preserve `.env` defaults

### Phase 4

Hardening.

- regression tests
- long-call soak test
- chunk timing tuning

## 16. Recommended First Implementation Scope

To keep risk down, the first shipping version of `whisper.cpp` should include only:

- dual-stream mode
- env-based backend switch
- subprocess-on-chunk execution
- canonical transcript publishing

Do not include in the first PR:

- single-stream diarization
- realtime stdin streaming to binary
- dynamic model switching in-call
- cloud fallback

## 17. Recommendation

For this codebase, `whisper.cpp` is a viable backend addition, but not a one-file swap.
The correct implementation is to treat it as a new local inference adapter under the existing transcript event pipeline.

The safest path is:

1. abstract current transcription transport behind backend adapters
2. add `whisper.cpp` in buffered chunk mode
3. expose `Transcript backend` in the setup menu
4. default to `whisper.cpp` (validated, built by `scripts/install.sh`); `mlx-whisper` stays as the experimental fallback

This gives the team a stable migration path without breaking dashboard, detector, or report consumers.
