# Replay Testing Guide

The replay harness lets you run previously-recorded WAV files through the full
audio → buffering → detection pipeline without a live microphone, a Whisper model,
or a running video call.

## How it works

```
WAV file
  └─ ReplayAudioStream (implements AudioStream Protocol)
       └─ AudioBufferer (RMS gating + VAD, produces InferenceQueueItem)
            └─ SharedInferenceQueue → [InferenceWorker → Whisper → transcript events]
                                                          ↑
                            WebSocketEventCollector captures all hub events
```

`ReplayAudioStream` (`src/sales_copilot/audio/replay.py`) is a drop-in
replacement for `MicStream` / `AudioTeeStream` / `BlackHoleStream`.  It reads a
16 kHz mono WAV and returns chunks via the same sync
`read() -> np.ndarray | None` interface.

## Test categories

| File | Marker | Runs in CI | Purpose |
|------|--------|-----------|---------|
| `test_replay_harness.py` | (none) | yes | Unit tests for ReplayAudioStream + WebSocketEventCollector |
| `test_replay_synthetic.py` | (none) | yes | Smoke: synthetic tone WAV through AudioBufferer |
| `test_replay_real_call.py` | `audio_fixture` | no | Real WAV files → speech segment detection |
| `test_replay_detector_e2e.py` | `audio_fixture` | no | Sliding-window detector e2e with mocked LLM |

Default `pytest tests/` skips all `audio_fixture` tests.

## Running audio_fixture tests locally

```bash
# Requires: data/sessions/2026-05-03T10-29-05/{self,prospect}.wav
pytest -m audio_fixture tests/test_replay_real_call.py -v

# Requires: data/sessions/2026-05-01T15-52-26/self.wav
pytest -m audio_fixture tests/test_replay_detector_e2e.py -v

# Run all audio_fixture tests
pytest -m audio_fixture -v
```

## Adding a new fixture test

1. Place your WAV recordings in `data/sessions/<session-id>/` (gitignored).
2. Recordings must be **16 kHz mono** WAV (the format the AudioRecorder writes).
3. Write a test with `@pytest.mark.audio_fixture` and skip if the file is absent:

```python
@pytest.mark.audio_fixture
async def test_my_session(tmp_path):
    wav = Path("data/sessions/2026-05-XX-YY/self.wav")
    if not wav.exists():
        pytest.skip(f"audio fixture not present: {wav}")

    queue = await run_bufferer_until_eof(wav, timeout_s=120.0)
    assert queue.qsize() >= 10
```

## Speed multipliers

| Mode | Setting | Use case |
|------|---------|---------|
| Test (default) | `real_time=False` | As fast as possible; no async blocking |
| Diagnostic real-time | `real_time=True, speed_multiplier=1.0` | Simulate live call, blocking |
| Fast diagnostic | `real_time=True, speed_multiplier=5.0` | 5x faster than real-time, blocking |
| Live replay (paced) | `paced=True, speed_multiplier=N` | Non-blocking real-time pacing for the live pipeline — `read()` returns `None` until the wall clock catches up instead of sleeping |

`real_time=True` uses `time.sleep()` and **must not** be used from inside an
asyncio task — it blocks the event loop.  Use it only in standalone scripts.
For anything running on an event loop (the live server), use `paced=True`,
which is what the `replay` capture method below wires up.

## Watch a real session live in the dashboard

`AUDIO_CAPTURE_METHOD=replay` plays an already-recorded session through the
**full live pipeline** — real transcription, real detection, real hub events —
so the dashboard shows the pain-point/objection cards live, without a
microphone or a running call. Both streams come from a recorded session
directory (`self.wav` = the rep, `prospect.wav` = the prospect, 16 kHz mono).

The one-command runner is `scripts/replay_session.py`. It does **not** set a
tier — run it once with `SALES_COPILOT_DEV_TIER=free` and once with `=pro` to
compare the Free lock-teaser against the Pro unlocked response on the same
conversation:

```bash
# Free tier run
SALES_COPILOT_DEV_TIER=free .venv/bin/python scripts/replay_session.py data/sessions/<session-id>

# Pro tier run (add --speed 2 for double-speed playback)
SALES_COPILOT_DEV_TIER=pro .venv/bin/python scripts/replay_session.py data/sessions/<session-id> --speed 2
```

Then open http://localhost:8760/dashboard and start the call as usual. The
recording plays back at real-time pace (`--speed N` for Nx), transcripts and
detection cards appear live, and the server keeps running (silent) after the
recording ends — stop it with Ctrl+C.

Under the hood the runner sets `AUDIO_CAPTURE_METHOD=replay`,
`REPLAY_SESSION_DIR=<dir>` and `REPLAY_SPEED=<N>`, then boots the normal
`python -m sales_copilot`. A missing directory or missing WAVs fails fast
with a clear error, before any server startup.

## Mocking the LLM for deterministic tests

For tests that need to go through the detector's window classifier without
making real Gemini API calls, monkeypatch `WindowClassifier.classify`:

```python
async def _mock_classify(self, window_text, latest_chunk, phase="discovery"):
    return WindowAnalysis(detections=[
        WindowDetection(
            category="pain_point",
            subcategory="offerteproces",
            confidence=0.9,
            evidence_quote=window_text[:50],
            reasoning="Mocked for test",
        )
    ])

monkeypatch.setattr(WindowClassifier, "classify", _mock_classify)
```

## Why personal audio stays in `data/` (gitignored)

Session recordings contain real sales conversations.  `data/sessions/` is
gitignored; only synthetic WAV files generated with `make_synthetic_wav()` are
committed to the repository.  Never commit session WAV files or metadata.

## WebSocketEventCollector

`WebSocketEventCollector` (in `tests/conftest_replay.py`) subscribes to all
hub channels and collects every broadcast message:

```python
stop = asyncio.Event()
collector = WebSocketEventCollector()
await collector.start_all("127.0.0.1", hub_port, stop)

# ... run your test ...

stop.set()
await collector.stop_all()
print(collector.report())  # {"transcripts": N, "pain_points": M, ...}
```
