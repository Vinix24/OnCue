# Shared Inference Queue Design

**Project:** OnCue
**Date:** 2026-05-01
**Status:** Implemented (2026-07)
**Audience:** Developers implementing the transcriber module refactor

---

> **Status: Implemented (2026-07).** The shared-queue refactor shipped: `audio_bufferer.py`, `inference_queue.py`, and `inference_worker.py` are the live implementation, with `TRANSCRIBER_SHARED_QUEUE=true` as the default. This document is the original design proposal; the "Current Architecture" section describes the pre-refactor dual-engine state, not today's default.

## 1. Goal

Replace the current dual-`DirectWhisperEngine` architecture with a single inference worker fed by a priority queue. The mic stream (self/operator) and the system audio stream (prospect) each become a lightweight `AudioBufferer` that pushes ready speech chunks onto a shared `SharedInferenceQueue`. A single `InferenceWorker` owns the one `TranscriptionBackend` instance and drains the queue sequentially.

This matters for three reasons. First, HIGH-priority prospect chunks drain before LOW-priority self chunks, which means coaching tips remain real-time during prospect speech while the operator's own transcripts — only needed for post-call reports and phase detection — tolerate the resulting 5–10 second delay without any user-visible regression. Second, a single backend instance eliminates all shared-state races between concurrent Metal command-buffer accesses; the whisper.cpp backend solves today's SIGABRT on MLX, but the underlying architecture remains fragile as long as two calls to `backend.transcribe()` can overlap. Third, a single sequential inference path is the correct structural prerequisite for a future MLX or Core ML backend, where the GPU lock is non-reentrant by design.

---

## 2. Non-goals

- Does not change the wire format of transcript events. Payloads published to `/ws/transcript` remain identical (`type`, `text`, `speaker`, `start_ms`, `end_ms`, `is_final`).
- Does not change the public API of the transcriber module runner. CLI arguments (`--engine direct`, `--backend whisper.cpp`, etc.) stay as-is.
- Does not change the `TranscriptionBackend` Protocol defined in `src/sales_copilot/modules/transcriber/backends/base.py`. All four methods (`start`, `warmup`, `transcribe`, `stop`) stay unchanged.
- Does not introduce model fine-tuning, accuracy improvements, or changes to the hallucination filter logic in `whisper_direct.py:240–275`.
- Does not implement a multi-worker variant. The design is explicitly a single `InferenceWorker`. Parallelism over multiple workers is a separate future decision.

---

## 3. Current Architecture

The module runner in `__main__.py:235–245` creates one `DirectWhisperEngine` per audio stream, each with its own `TranscriptionBackend` instance:

```text
Mic stream    ──> DirectWhisperEngine("self",    backend=B1) ──> /ws/transcript
System stream ──> DirectWhisperEngine("prospect", backend=B2) ──> /ws/transcript
```

Both engines execute concurrently as independent asyncio tasks (`__main__.py:287–289`). Inside each engine, `_flush_buffer()` (`whisper_direct.py:104–136`) calls `await self.backend.transcribe(audio)` at `whisper_direct.py:117` as soon as a speech chunk is ready. With `mlx-whisper`, both engines can invoke their respective backends in the same event-loop turn, causing overlapping Metal dispatches and SIGABRTs.

Each `DirectWhisperEngine` also owns its own WebSocket client connection (`whisper_direct.py:40`, `_ensure_ws` at `whisper_direct.py:152–166`), meaning two connections are established to `/ws/transcript` for every call.

The `Speaker` type (`engine.py:6`) and `TranscriptEvent` dataclass (`engine.py:9–26`) are shared between both engines today and remain unchanged.

---

## 4. Proposed Architecture

```text
Mic stream    ──> AudioBufferer(speaker="self",     priority=LOW)  ─┐
                                                                     ├──> SharedInferenceQueue ──> InferenceWorker ──> /ws/transcript
System stream ──> AudioBufferer(speaker="prospect", priority=HIGH) ─┘
```

**AudioBufferer** replaces the buffering + VAD half of `DirectWhisperEngine`. It reads from an `AudioStream`, runs the Silero VAD + RMS gate, accumulates speech frames, and pushes complete chunks onto the shared queue as `InferenceQueueItem` values. It does not hold a backend reference and does not call `transcribe()`.

**SharedInferenceQueue** is a bounded priority queue. HIGH-priority items (prospect) surface before LOW-priority items (self), and within the same priority, older items surface first via the `enqueued_at_ms` tiebreaker.

**InferenceWorker** owns the single `TranscriptionBackend` instance and the single WebSocket client connection. It loops: pop the highest-priority item, call `backend.transcribe(item.audio)`, apply the hallucination filter, and publish the resulting `TranscriptEvent` to `/ws/transcript`.

---

## 5. Data Types

The queue item is a sortable dataclass so Python's `heapq`/`asyncio.PriorityQueue` can order items without custom comparators:

```python
@dataclass(order=True)
class InferenceQueueItem:
    priority: int          # 0=HIGH (prospect), 1=LOW (self)
    enqueued_at_ms: int    # tiebreaker — older items first within same priority
    audio: np.ndarray = field(compare=False)
    speaker: Speaker = field(compare=False)
    start_ms: int = field(compare=False)
    end_ms: int = field(compare=False)
    speech_start_ms: int | None = field(default=None, compare=False)
```

The `(priority, enqueued_at_ms)` pair forms a stable, total sort key. `audio` and `speaker` are excluded from comparison because `np.ndarray` does not implement `__lt__`, and heap operations require a consistent ordering. Excluding non-comparable fields is the correct pattern — Python's `field(compare=False)` is designed for this.

---

## 6. Class Responsibilities

**`AudioBufferer`** (internal rename of `DirectWhisperEngine`, inference logic stripped):
- Reads frames from `AudioStream.read()`.
- Runs RMS gate and Silero VAD (`_is_speech` logic from `whisper_direct.py:183–197`).
- Accumulates frames in `_buffer` until `max_buffer_seconds` is reached or silence flush triggers.
- Pushes a complete `InferenceQueueItem` onto the shared queue with its configured `priority` and `speaker`.
- Does not hold a `TranscriptionBackend` reference.
- Handles `AudioRecorder.write_chunk` for session recording, identical to current `_record_chunk` at `whisper_direct.py:199–207`.

**`SharedInferenceQueue`**:
- Wraps `asyncio.PriorityQueue` with a configurable bound (`TRANSCRIBER_QUEUE_MAX_SIZE`, default 32).
- `put(item: InferenceQueueItem)`: if queue is full, drops the oldest LOW-priority item before inserting; if no LOW items are present, drops the oldest HIGH item with a WARN log.
- `get() -> InferenceQueueItem`: awaits the next highest-priority item.
- `qsize() -> int`: returns current depth for metric logging.

**`InferenceWorker`**:
- Owns the single shared `TranscriptionBackend` instance.
- Owns the single WebSocket client connection to `/ws/transcript` (same reconnect-with-backoff pattern as `_ensure_ws` at `whisper_direct.py:152–166`).
- Async loop: `item = await queue.get()` → `text = await backend.transcribe(item.audio)` → apply hallucination filter (`_is_hallucination` from `whisper_direct.py:240–275`) → construct `TranscriptEvent` → send via WebSocket.
- On `transcribe()` exception: log warning, drop item, continue. The loop must not crash.
- Honors `stop_event`: completes any in-flight `transcribe()` call before exiting.

---

## 7. Backwards Compatibility

- Transcript event wire format unchanged. `TranscriptEvent.to_dict()` (`engine.py:18–26`) is not modified.
- `Speaker` enum (`engine.py:6`) unchanged.
- `TranscriptionBackend` Protocol (`backends/base.py:9–14`) unchanged.
- Module runner CLI arguments unchanged. The `__main__.py` entry point continues to accept `--engine direct`, `--backend`, and related flags.
- `whisper_direct.py` module path preserved. The file stays at its current path so existing imports in tests and tooling continue to resolve. Internal class renaming is implementation-only.

---

## 8. Configuration

Three new env variables with defaults that preserve current behavior when `TRANSCRIBER_SHARED_QUEUE` is not set:

```bash
TRANSCRIBER_SHARED_QUEUE=true        # true = new queue architecture, false = legacy dual-engine
TRANSCRIBER_QUEUE_MAX_SIZE=32        # max items in queue before backpressure kicks in
TRANSCRIBER_SELF_PRIORITY=low        # low = self goes to LOW lane; equal = both streams same priority
```

`TRANSCRIBER_SHARED_QUEUE=false` restores the current two-engine path without any other code change, enabling a safe rollback without redeployment.

---

## 9. Error Handling

| Scenario | Behavior |
|---|---|
| `backend.transcribe()` raises | Log WARNING, drop item, continue worker loop. |
| Queue full + LOW item arriving | Drop oldest LOW item silently, log `queue_dropped` at DEBUG with `speaker=self`. |
| Queue full + HIGH item arriving | Drop oldest LOW item; if none present, drop oldest HIGH item with WARN log. |
| WebSocket disconnect during publish | Reconnect with exponential backoff (pattern already in `_ensure_ws`). Retry publish once after reconnect. |
| Stop event during in-flight `transcribe()` | Allow `transcribe()` to complete, then exit the worker loop. Do not interrupt mid-inference. |
| `AudioBufferer` frame read returns `None` | Silence-flush current buffer (if any), sleep 10ms, continue. Same as `whisper_direct.py:71–75`. |

---

## 10. Testing Strategy

**Unit tests:**
- Queue ordering: verify HIGH items surface before LOW items regardless of insertion order.
- FIFO within same priority: two HIGH items resolve in `enqueued_at_ms` order.
- Backpressure — LOW dropped: fill queue with LOW items, insert one HIGH item, verify HIGH is not dropped.
- Backpressure — HIGH fallback: fill queue with only HIGH items, verify oldest HIGH is dropped with a WARN log.
- `AudioBufferer` flush conditions: `max_buffer_seconds` triggers flush; silence gap triggers flush; empty buffer on silence returns without pushing.

**Integration tests:**
- Two `AudioBufferer` instances feeding one `InferenceWorker`: under artificial backpressure, prospect chunks emerge before self chunks.
- Backend error does not crash worker: mock `backend.transcribe` to raise, verify worker continues processing subsequent items.

**Regression tests:**
- All existing `test_whisper_direct*.py` tests continue to pass after the internal rename.
- Transcript events published by `InferenceWorker` are structurally identical to events published by the current `DirectWhisperEngine._flush_buffer()`.

---

## 11. Migration Plan

**Phase 1 — Feature behind flag** (`TRANSCRIBER_SHARED_QUEUE=false` default):
Land `AudioBufferer`, `SharedInferenceQueue`, and `InferenceWorker` alongside the existing `DirectWhisperEngine`. Both paths must coexist; all existing tests must pass. The flag defaults to `false`, so no behavior changes on existing installs.

**Phase 2 — Flip default** (`TRANSCRIBER_SHARED_QUEUE=true`):
After one round of live testing confirms stability, flip the default to `true`. Update `.env.example`. Keep the legacy path present and reachable by setting the flag explicitly to `false`.

**Phase 3 — Remove legacy path** (follow-up PR, one week after Phase 2):
Delete the dual-engine code path from `__main__.py` and remove the `TRANSCRIBER_SHARED_QUEUE` flag once the `true` default has run without regressions. Clean up the dead conditional.

---

## 12. Open Questions / Future Work

- **CPU model for self-stream (Option C):** running a small CPU-only model on the LOW-priority self stream while the GPU serves the HIGH-priority prospect stream would further reduce self-stream latency without competing for GPU time. Out of scope for this PR.
- **Queue depth metrics on dashboard:** exposing `SharedInferenceQueue.qsize()` and `queue_dropped` counts as a live metric panel would aid tuning of `TRANSCRIBER_QUEUE_MAX_SIZE`. Deferred to a future dashboard PR.
- **Streaming partial outputs (Option 4):** incremental partial transcripts from `whisper.cpp` stream mode are architecturally easier on a single-worker pipeline than on the current dual-engine model, since there is no risk of partial outputs from two concurrent streams interleaving on the WebSocket. This refactor is a prerequisite for that work, but streaming partials are out of scope here.
