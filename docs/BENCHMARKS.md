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

## Live suggestion latency: OpenRouter against Vertex (2026-11-20)

What was measured: the wall-clock time of one live-suggestion LLM call (`scripts/measure_suggestion_latency.py`, from `origin/main` at `f17af54`), in streaming and non-streaming mode, plus time to first partial (ttft) in streaming mode. Fragment: `tests/operator_scripts/scripted_sales_call_nl.md` (8 lines, sha256 prefix `f2c3d1780b607f11`). 30 runs per candidate and mode, per-call timeout 30000 ms, `max_output_tokens` 512. Run on 2026-11-20 between 06:10 and 06:20 UTC from a Mac mini on a home connection, so the numbers include the network round trip. Full result: [`docs/benchmarks/suggestion-latency-2026-11-20.json`](benchmarks/suggestion-latency-2026-11-20.json).

| candidate | provider | model | mode | n | ok | median (ms) | p95 (ms) | ttft50 (ms) | ttft95 (ms) |
|---|---|---|---|---|---|---|---|---|---|
| haiku | openrouter | `anthropic/claude-haiku-4.5` | streaming | 30 | 30 | 1577 | 3045 | 1577 | 3044 |
| haiku | openrouter | `anthropic/claude-haiku-4.5` | non-streaming | 30 | 30 | 1592 | 2278 | - | - |
| gemini | vertex | `gemini-2.5-flash` | streaming | 30 | 30 | 6394 | 9392 | 6394 | 9392 |
| gemini | vertex | `gemini-2.5-flash` | non-streaming | 30 | 30 | 6228 | 9130 | - | - |
| qwen | openrouter | `qwen/qwen3-30b-a3b` | both | 5 | 0 | not measured | - | - | - |

What it shows:

- Haiku via OpenRouter has a median about four times lower than gemini-2.5-flash via Vertex (1.6 s against 6.2 to 6.4 s). Its p95 (2.3 to 3.0 s) is below Vertex's median.
- Streaming gives no earlier first output in this suggestion path: for both candidates the time to first partial equals the full call time (haiku 1577 ms, gemini 6394 ms at the median). The streaming path hands every partial from the `instructor` stream to `on_partial` (`src/sales_copilot/modules/detector/suggestions.py`); this measurement does not establish why the first partial only arrives with the complete answer.
- Qwen was not measured because of upstream rate limiting, not because of a model error: OpenRouter returned HTTP 429 ("temporarily rate-limited") from the pinned upstream provider on 5 calls in a row, and the script stopped the candidate early (25 runs skipped per mode).
- The code default is consistent with this measurement: `DetectorConfig.llm_model` in `src/sales_copilot/core/config.py` defaults to `anthropic/claude-haiku-4.5`, and `config/llm_models.yaml` lists OpenRouter as the default public tier with that model first. The provider itself defaults to `none` and is chosen in setup. No default was changed; this section only records the measurement.

## Live detection without a key (free tier)

Question (launch criterion 1): does a free install, with no license key and no LLM key,
show a live transcript, detection cards and talk-time coaching through the real live path?

**Commit measured:** `80daf0d` (origin/main on 2026-11-20, after #370 moved `coaching.live` to the free tier).

**Method.** `scripts/measure_live_detection.py` starts `python -m sales_copilot` (replay capture,
`LLM_PROVIDER=none`, `SALES_COPILOT_DEV_TIER=free`) on scratch port 8771 with a per-run token, subscribes to
every hub channel, POSTs `/api/start-call`, records 85 s, POSTs `/api/end-call` and stops only its own child.
Audio: a ten-turn quoting dialogue rendered with macOS `say` (EN, NL), replayed at speed 1.0 through VAD and
whisper.cpp `large-v3-turbo-q8_0`. `LANGUAGE` and `CALL_LANGUAGE` are pinned to the call language so a local
`.env` cannot change the run. Only the local router ran; the LLM classifier, suggestions, summary, phase and
insights are not covered. One run per language; whisper chunking varies by about one event between runs.

| Channel | EN free | NL free |
|---|---|---|
| transcript | 8 final, 8 partial | 7 final, 7 partial |
| pain-points | 2 (`quote_process`, `capacity`) | 2 (`offerteproces`, `kosten`) |
| objections | 2 (`price`, `timing`) | 2 (`prijs`, `timing`) |
| buying-signals | 1 (`koopsignaal`) | 2 (`gap-behoefte`, `koopsignaal`) |
| talk-time | 85 snapshots | 89 snapshots |
| coaching | 6 `coaching_alert` (`ratio_warning`), 2 `system_status` | 8 `coaching_alert`, 2 `system_status` |
| script-tracking, insights, suggestions, summary, phase, slide-control | 0 | 0 |
| detector-status | 16 | 16 |

Arrival of a detection after the transcript event that carried its text (matched on `timestamp_ms`
and `end_ms`): the first pain point of the call took 8324 ms (EN) and 7940 ms (NL); all other
detections took 7-51 ms. The `result.json` of a run reports the same per channel as first and median
delay after the latest preceding transcript event.

**Answers.**
- Transcript events: yes, in both languages.
- Pain-point, objection and buying-signal events: yes, in both languages. The objection and buying-signal
  cards carry the Pro lock teaser instead of the response text; pain-point cards carry an empty response.
- Talk-time coaching events: **yes, now.** The previous measurement (bba55fa) showed 0 coaching events for
  free and a hub log line "Suppressing coaching broadcast". Both runs now deliver `coaching_alert` and
  `system_status`, and the hub logged no suppression. Gate: `FREE_FEATURES` contains
  `FEATURE_LIVE_COACHING` (`src/sales_copilot/auth/license_format.py:101`), checked at
  `src/sales_copilot/websocket/hub_core.py:110`. Script-tracking and insights stay Pro-gated.
- Criterion 1 holds for the local path with synthetic TTS audio. It is not proven for real call audio
  or for a configured LLM provider.

**Earlier open items.**
- Item 2, `module_failure` visible to free users: by code, yes. It is published on the coaching channel
  (`src/sales_copilot/__main__.py:595`), which is no longer gated for free. Not exercised: no module crashed
  in either run (the talk-time crash at end of replay did not recur).
- Item 3, Dutch lock teaser in English calls: fixed when `LANGUAGE` matches the call. The teaser is an i18n key
  (`config/i18n/en.yaml:27`, `nl.yaml:31`, `src/sales_copilot/auth/feature_policy.py:256`) and the EN run showed
  English text. It follows `LANGUAGE`, not the call language (`src/sales_copilot/core/i18n.py:97`), so a Dutch UI
  with an English call still gets Dutch. The "AI opwarmen..." status
  (`src/sales_copilot/modules/transcriber/__main__.py:467`, `:518`) was Dutch in the EN run.
- Item 5, first detection about 5 s late: **still open, now 7.9-8.3 s.** The embedding router is built on first
  use (`src/sales_copilot/modules/detector/router.py:171`) and nothing builds it at startup.

**Reproduce.** Build the replay folders with `scripts/build_replay_fixture.sh` into a scratch directory, then run
`python scripts/measure_live_detection.py --fixture-dir DIR --lang en --tier free --out-dir OUT --wait 85`.

**After the router warm-up (#421).** The detector module now builds the embedding router in an executor thread
when it starts (`PainPointRouter.warm_up`, started from `src/sales_copilot/modules/detector/__main__.py`); a
`classify` call during the build waits for it, and a failed build falls back to build-on-first-use. Commit measured:
`d980776`, same method as above, one run per language. The first pain point arrived 2906 ms (EN) and 2936 ms (NL)
after the transcript event that carried its text, against 8324 ms and 7940 ms before. Later detections took
28-34 ms (EN) and 28 ms (NL). The dialogue differed from the run above, so the comparison is in size only. The
warm-up itself took 10.4 s (EN) and 7.5 s (NL) while whisper warmed up in parallel. The remaining about 3 s is not
the pain-point router: the script tracker and the objection router still build their own embedding model on first
use, at the first transcript, and compete for the CPU.

**After the objection router and script tracker warm-up.** The detector now also builds the objection router
and, only when the tier computes script coverage, the script tracker model at start, in daemon threads
(`_start_warmup` in `src/sales_copilot/modules/detector/__main__.py`). Commit measured: `75624be` plus this change,
free tier, speed 1.0, one run per language, short synthetic dialogue. The first pain point arrived 2039 ms (EN)
and 2064 ms (NL) after the transcript event that carried its text, against 2906 ms and 2936 ms after #421. The
goal of under 1 s is not reached: the rest is not model loading in the detector, and was not traced further.
