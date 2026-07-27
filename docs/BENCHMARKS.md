# Benchmarks — local model shootout

This copilot does three jobs during a live call — transcribe, detect, and summarize —
and each one runs on a different latency budget. The table below is the local-model
shootout result: which model handles each job, and how fast it actually is, measured
on real hardware (Apple Silicon), not vendor-quoted numbers.

| Job | Latency budget | Local model | Measured |
|---|---|---|---|
| Transcribe (continuous) | real-time | whisper.cpp `large-v3-turbo` (default); `large-v3-turbo-q8_0` value pick | p50 2.2-4.4s per 3000ms streamed chunk across all whisper.cpp sizes (1x real-time feed keeps up); `q8_0` streams at p50 2.79s |
| Detect (is this an objection?) | instant (<0.1s) | semantic-router embedding fast-route | ~5ms, 8/12 objections matched on the fast route |
| Summarize (running/post-call) | 10-60s | `gemma-3n-e4b` | best small local summarizer on opportunity-recall in the set; comfortably inside the 10-60s budget |

All three jobs run fully local — no cloud call required for transcription, detection,
or summarization. Measured 2026-07-09 on Apple Silicon with the machine otherwise idle,
so these are model numbers, not contention artifacts.

### Methodology: two kinds of transcription speed

The transcription row measures **streaming latency**, not per-run batch time. These are different metrics and the numbers are not directly comparable.

**(a) Streaming latency** (what the table measures). Definition: `latency = emit_time − audio_end_time`, where `emit_time` is the wall-clock moment a transcribed segment appears and `audio_end_time` is that segment's end position in the audio. The audio is fed at 1x real-time from a background feeder thread into a single sequential consumer — the same single-worker model the live engine uses, so transcription backlog shows up as growing latency. With chunked backends (whisper.cpp, mlx-whisper) a segment cannot appear before its chunk is full, so the **chunk size is the hard latency floor** (3000 ms in the default configuration). Parakeet's `transcribe_stream` emits at sentence granularity, finer than the fixed chunk, which is why its numbers look different even when per-unit compute time is similar.

**(b) Per-run processing time** of a single static audio fragment (batch transcription of a pre-recorded file, without a real-time feeder). This is much lower than streaming latency because there is no backlog: the backend processes the whole fragment in one pass with no chunk-size floor. Cloud ASR comparisons (e.g. "Deepgram transcribes in X seconds") typically report this number, not streaming latency. Comparing a cloud batch number to the streaming-latency numbers in the table above gives a misleading result — you are comparing (b) to (a).

**Thread count matters and must be reported.** The number of whisper.cpp CPU threads (`WHISPER_CPP_THREADS` env variable, or the `--threads` flag in `scripts/benchmark_transcription.py`) directly affects streaming latency. Every reported p50/p95 should name the thread count used. On Apple Silicon more CPU threads do not improve throughput; the open question is whether stronger hardware (discrete GPU, high-core-count x86) changes this.
