import asyncio
import json
import logging
import types

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector.sliding_window import SlidingWindowBuffer
from sales_copilot.modules.detector.window_classifier import WindowAnalysis, WindowDetection
from sales_copilot.websocket import hub_auth


def test_decode_payload_parses_json() -> None:
    payload = {"type": "transcript", "text": "hi"}
    raw = json.dumps(payload)

    assert detector_main._decode_payload(raw) == payload


def test_decode_payload_returns_raw_on_invalid_json() -> None:
    raw = "{not-json"

    assert detector_main._decode_payload(raw) == raw


def test_extract_transcript_fields_uses_end_ms() -> None:
    payload = {
        "type": "transcript",
        "text": "hello",
        "speaker": "prospect",
        "start_ms": 100,
        "end_ms": 250,
    }

    assert detector_main._extract_transcript_fields(payload) == ("hello", "prospect", 250)


def test_extract_transcript_fields_falls_back_to_start_ms() -> None:
    payload = {
        "type": "transcript",
        "text": "hello",
        "speaker": "self",
        "start_ms": 123,
    }

    assert detector_main._extract_transcript_fields(payload) == ("hello", "self", 123)


def test_extract_transcript_fields_rejects_invalid_payload() -> None:
    assert detector_main._extract_transcript_fields({"type": "coaching"}) is None
    assert detector_main._extract_transcript_fields({"type": "transcript", "text": "", "speaker": "self"}) is None
    assert detector_main._extract_transcript_fields({"type": "transcript", "text": "hi"}) is None


async def _noop_async(*args, **kwargs) -> None:
    return None


async def test_main_passes_session_id_to_script_tracker(monkeypatch) -> None:
    """Phase 3 wiring: the call's session_id must reach ScriptTracker so the
    coverage checkpoint is persisted per-call (and re-checkpointed at call end
    for the post-call Free scorecard)."""
    captured: dict[str, object] = {}

    class _FakeTracker:
        def __init__(self, config, **kwargs) -> None:
            captured["tracker_kwargs"] = kwargs
            captured["tracker"] = self
            self.checkpoint_calls = 0

        async def checkpoint(self) -> None:
            self.checkpoint_calls += 1

    class _FakeEngine:
        def __init__(self, tracker, ws_config) -> None:
            self._tracker = tracker

        async def run(self, stop_event) -> None:
            await stop_event.wait()

    class _FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeCaseDB:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def list_cases(self):
            return []

    monkeypatch.setattr(detector_main.websockets, "connect", lambda *a, **k: _FakeWS())
    monkeypatch.setattr(
        detector_main, "PainPointRouter", lambda *a, **k: types.SimpleNamespace(routes=[])
    )
    monkeypatch.setattr(detector_main, "LLMConfirmClient", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "PainPointDebouncer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "DetectionPipeline", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "SQLiteCaseDB", _FakeCaseDB)
    monkeypatch.setattr(
        detector_main,
        "load_preset",
        lambda name: types.SimpleNamespace(name=name, objections=[], ui_labels={}),
    )
    monkeypatch.setattr(
        detector_main,
        "SlideInjector",
        lambda *a, **k: types.SimpleNamespace(close=_noop_async),
    )
    monkeypatch.setattr(detector_main, "SlidingWindowBuffer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "WindowClassifier", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "ScriptTracker", _FakeTracker)
    monkeypatch.setattr(detector_main, "ScriptTrackerEngine", _FakeEngine)
    monkeypatch.setattr(
        detector_main,
        "get_feature_policy",
        lambda: types.SimpleNamespace(allows=lambda feature: True),
    )
    monkeypatch.setattr(detector_main, "_print_banner", lambda *a, **k: None)
    monkeypatch.setattr(detector_main, "_consume_transcripts", _noop_async)

    config = DetectorConfig(
        llm_provider="none",
        enable_objection_detection=False,
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
        enable_script_tracking=True,
    )

    await detector_main.main(
        detector_config=config,
        register_signals=False,
        session_id="session-xyz",
    )

    tracker_kwargs = captured.get("tracker_kwargs") or {}
    assert tracker_kwargs.get("session_id") == "session-xyz"
    # Final checkpoint at call end, so the scorecard never reads a stale
    # snapshot when the call ended before the next confirmation cycle.
    assert captured["tracker"].checkpoint_calls == 1


# --- transcript-channel subscription confirmation (D-c197e0c3) --------------
#
# __main__.py already logged "Connecting to transcript stream: %s" *before*
# websockets.connect, which is indistinguishable in the log from a connect
# that then failed or hung. This test drives the real main() with a fake
# websocket connect and asserts a distinct INFO line only fires once the
# `async with websockets.connect(...)` context has actually been entered.


async def test_main_logs_transcript_subscription_confirmed(monkeypatch, caplog) -> None:
    class _FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeCaseDB:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def list_cases(self):
            return []

    monkeypatch.setattr(detector_main.websockets, "connect", lambda *a, **k: _FakeWS())
    monkeypatch.setattr(
        detector_main, "PainPointRouter", lambda *a, **k: types.SimpleNamespace(routes=[])
    )
    monkeypatch.setattr(detector_main, "LLMConfirmClient", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "PainPointDebouncer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "DetectionPipeline", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "SQLiteCaseDB", _FakeCaseDB)
    monkeypatch.setattr(
        detector_main,
        "load_preset",
        lambda name: types.SimpleNamespace(name=name, objections=[], ui_labels={}),
    )
    monkeypatch.setattr(
        detector_main,
        "SlideInjector",
        lambda *a, **k: types.SimpleNamespace(close=_noop_async),
    )
    monkeypatch.setattr(detector_main, "SlidingWindowBuffer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "WindowClassifier", lambda *a, **k: object())
    monkeypatch.setattr(
        detector_main,
        "get_feature_policy",
        lambda: types.SimpleNamespace(allows=lambda feature: True),
    )
    monkeypatch.setattr(detector_main, "_print_banner", lambda *a, **k: None)
    monkeypatch.setattr(detector_main, "_consume_transcripts", _noop_async)

    config = DetectorConfig(
        llm_provider="none",
        enable_objection_detection=False,
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
        enable_script_tracking=False,
    )

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await detector_main.main(
            detector_config=config,
            register_signals=False,
        )

    connecting_records = [
        r for r in caplog.records if "Connecting to transcript stream" in r.getMessage()
    ]
    subscribed_records = [
        r for r in caplog.records if "Subscribed to transcript channel" in r.getMessage()
    ]
    assert connecting_records, "expected the pre-connect attempt line to still fire"
    assert subscribed_records, "expected a distinct confirmation line once connect succeeded"
    # The confirmation must follow the attempt -- a failed/hung connect must
    # never reach it, so ordering is how a reader tells the two apart.
    assert caplog.records.index(subscribed_records[0]) > caplog.records.index(connecting_records[0])


# --- hub token must not leak into the log (PR #240 follow-up) ---------------
#
# Both the pre-connect "Connecting to transcript stream" line and the
# "Subscribed to transcript channel" confirmation log the URL returned by
# channel_ws_url(), which carries the hub token as HTTP Basic userinfo
# (ws://token:<TOKEN>@host:port/ws/transcript). A DEBUG run on 2026-09-27
# confirmed both lines wrote the raw token to the server log. This test forces
# a known token via a monkeypatch on hub_auth.get_hub_token so the assertion
# knows exactly which string must never appear, and drives the real main()
# so a regression in either log call site is caught.


async def test_main_never_logs_the_hub_token(monkeypatch, caplog) -> None:
    secret_token = "SECRET-HUB-TOKEN-do-not-log-9f3a1c"
    monkeypatch.setattr(hub_auth, "get_hub_token", lambda: secret_token)
    monkeypatch.setenv("WS_HUB_HOST", "127.0.0.1")
    monkeypatch.setenv("WS_HUB_PORT", "19999")

    class _FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeCaseDB:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def list_cases(self):
            return []

    monkeypatch.setattr(detector_main.websockets, "connect", lambda *a, **k: _FakeWS())
    monkeypatch.setattr(
        detector_main, "PainPointRouter", lambda *a, **k: types.SimpleNamespace(routes=[])
    )
    monkeypatch.setattr(detector_main, "LLMConfirmClient", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "PainPointDebouncer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "DetectionPipeline", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "SQLiteCaseDB", _FakeCaseDB)
    monkeypatch.setattr(
        detector_main,
        "load_preset",
        lambda name: types.SimpleNamespace(name=name, objections=[], ui_labels={}),
    )
    monkeypatch.setattr(
        detector_main,
        "SlideInjector",
        lambda *a, **k: types.SimpleNamespace(close=_noop_async),
    )
    monkeypatch.setattr(detector_main, "SlidingWindowBuffer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "WindowClassifier", lambda *a, **k: object())
    monkeypatch.setattr(
        detector_main,
        "get_feature_policy",
        lambda: types.SimpleNamespace(allows=lambda feature: True),
    )
    monkeypatch.setattr(detector_main, "_print_banner", lambda *a, **k: None)
    monkeypatch.setattr(detector_main, "_consume_transcripts", _noop_async)

    config = DetectorConfig(
        llm_provider="none",
        enable_objection_detection=False,
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
        enable_script_tracking=False,
    )

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await detector_main.main(
            detector_config=config,
            register_signals=False,
        )

    connecting_records = [
        r for r in caplog.records if "Connecting to transcript stream" in r.getMessage()
    ]
    subscribed_records = [
        r for r in caplog.records if "Subscribed to transcript channel" in r.getMessage()
    ]
    assert connecting_records, "expected the pre-connect attempt line to still fire"
    assert subscribed_records, "expected the subscription confirmation line to still fire"

    for record in connecting_records + subscribed_records:
        message = record.getMessage()
        assert secret_token not in message, f"hub token leaked into log line: {message!r}"
        assert "127.0.0.1:19999" in message
        assert "/ws/transcript" in message


# --- _consume_transcripts heartbeat (D-4d04b337) -----------------------------
#
# The hot-path instrumentation in _consume_transcripts is DEBUG-only, and the
# default LOG_LEVEL is INFO, so a detector silently dropping every chunk used
# to be indistinguishable, at the level anybody actually runs at, from a
# detector that was never started. These tests drive the real
# _consume_transcripts loop with a fake WebSocket to prove the INFO-level
# heartbeat and closing summary always leave a trace.


class _FakeTranscriptWS:
    """Minimal recv()-only stand-in for a websockets client connection.

    Messages are queued up front. wait_drained() resolves once every queued
    message has been dequeued by _consume_transcripts; after that, recv()
    blocks on an empty queue so the caller's asyncio.wait_for(..., timeout=...)
    times out naturally, exactly like polling a live socket with nothing new
    to read.
    """

    def __init__(self, messages: list[str]) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        for message in messages:
            self._queue.put_nowait(message)

    async def recv(self) -> str:
        item = await self._queue.get()
        self._queue.task_done()
        return item

    async def wait_drained(self) -> None:
        await self._queue.join()


class _FakeInjector:
    def __init__(self) -> None:
        self.dispatched: list[tuple[str, float, str, int]] = []

    async def handle_window_detection(
        self, *, category: str, confidence: float, evidence_quote: str, timestamp_ms: int
    ) -> None:
        self.dispatched.append((category, confidence, evidence_quote, timestamp_ms))

    async def process_transcript(self, text: str, speaker: str, timestamp_ms: int) -> None:
        pass


class _FakeWindowClassifier:
    def __init__(self, detections: list[WindowDetection] | None = None) -> None:
        self._detections = detections or []
        self.calls = 0

    async def classify(self, *, window_text: str, latest_chunk) -> WindowAnalysis:  # noqa: ANN001
        self.calls += 1
        return WindowAnalysis(detections=self._detections)


def _transcript_message(text: str, speaker: str, end_ms: int) -> str:
    return json.dumps({"type": "transcript", "text": text, "speaker": speaker, "end_ms": end_ms})


async def _drive_consume_transcripts(
    messages: list[str],
    *,
    config: DetectorConfig,
    injector: _FakeInjector | None = None,
    window_classifier: _FakeWindowClassifier | None = None,
    status_publisher=None,
) -> None:
    ws = _FakeTranscriptWS(messages)
    stop_event = asyncio.Event()
    window_buf = SlidingWindowBuffer(config.sliding_window_size)
    task = asyncio.create_task(
        detector_main._consume_transcripts(
            injector if injector is not None else _FakeInjector(),
            None,
            None,
            None,
            None,
            ws,
            stop_event,
            window_buf=window_buf,
            window_classifier=window_classifier if window_classifier is not None else _FakeWindowClassifier(),
            objection_detector=None,
            ws_config=WebSocketConfig(),
            config=config,
            ui_labels={},
            status_publisher=status_publisher,
        )
    )
    await asyncio.wait_for(ws.wait_drained(), timeout=2.0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_consume_transcripts_emits_first_chunk_line(caplog) -> None:
    config = DetectorConfig(min_chunks_to_classify=1000)
    messages = [_transcript_message("hallo daar", "prospect", 1000)]

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await _drive_consume_transcripts(messages, config=config)

    first_chunk_records = [
        r for r in caplog.records if "first transcript chunk received" in r.getMessage()
    ]
    assert first_chunk_records, "Expected an INFO line for the first transcript chunk"
    msg = first_chunk_records[0].getMessage()
    assert "speaker=prospect" in msg
    assert "end_ms=1000" in msg


async def test_consume_transcripts_heartbeat_fires_every_interval(caplog) -> None:
    """The heartbeat must fire at the configured chunk interval and name the
    counters, so activity is visible at INFO without waiting for call end."""
    interval = detector_main._HEARTBEAT_CHUNK_INTERVAL
    config = DetectorConfig(min_chunks_to_classify=1000)  # never reaches classify
    messages = [
        _transcript_message(f"chunk {i}", "prospect", i * 1000) for i in range(interval)
    ]

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await _drive_consume_transcripts(messages, config=config)

    heartbeat_records = [r for r in caplog.records if "Detector heartbeat" in r.getMessage()]
    assert heartbeat_records, "Expected a heartbeat line after the configured chunk interval"
    assert f"received={interval}" in heartbeat_records[0].getMessage()

    summary_records = [r for r in caplog.records if "Detector session summary" in r.getMessage()]
    assert summary_records, "Expected a closing summary line at the end of the session"
    summary_msg = summary_records[0].getMessage()
    assert f"received={interval}" in summary_msg
    assert f"buffered_below_min={interval}" in summary_msg
    assert "dispatched=0" in summary_msg


async def test_consume_transcripts_all_dropped_by_only_classify_prospect(caplog) -> None:
    """A run where every chunk is filtered by only_classify_prospect must still
    leave a non-silent INFO record that names the reason -- not just silence
    that looks identical to a detector that never started."""
    interval = detector_main._HEARTBEAT_CHUNK_INTERVAL
    config = DetectorConfig(only_classify_prospect=True)
    messages = [
        _transcript_message(f"chunk {i}", "self", i * 1000) for i in range(interval)
    ]

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await _drive_consume_transcripts(messages, config=config)

    summary_records = [r for r in caplog.records if "Detector session summary" in r.getMessage()]
    assert summary_records, "Expected a closing summary even when every chunk is filtered"
    summary_msg = summary_records[0].getMessage()
    assert f"received={interval}" in summary_msg
    assert f"skipped_not_prospect={interval}" in summary_msg
    assert "dispatched=0" in summary_msg


async def test_consume_transcripts_closing_summary_emitted_below_heartbeat_interval(caplog) -> None:
    """A short call that never reaches the heartbeat interval must still leave
    a closing count behind."""
    config = DetectorConfig(min_chunks_to_classify=1000)
    messages = [_transcript_message("chunk", "prospect", 1000)]
    assert len(messages) < detector_main._HEARTBEAT_CHUNK_INTERVAL

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await _drive_consume_transcripts(messages, config=config)

    heartbeat_records = [r for r in caplog.records if "Detector heartbeat" in r.getMessage()]
    assert not heartbeat_records, "Should not heartbeat before the configured interval"

    summary_records = [r for r in caplog.records if "Detector session summary" in r.getMessage()]
    assert summary_records, "Expected a closing summary even for a short session"
    assert "received=1" in summary_records[0].getMessage()


async def test_consume_transcripts_dispatches_pain_point_detection(caplog) -> None:
    """A real classification + dispatch path must show up as classified=1 and
    dispatched=1 in the closing summary."""
    config = DetectorConfig(
        min_chunks_to_classify=1,
        classification_debounce_seconds=0.0,
        confidence_threshold_low=0.5,
    )
    detection = WindowDetection(
        category="pain_point",
        subcategory="offerteproces",
        confidence=0.9,
        evidence_quote="we zitten uren aan offertes",
        reasoning="test detectie",
    )
    classifier = _FakeWindowClassifier(detections=[detection])
    injector = _FakeInjector()
    messages = [_transcript_message("we zitten uren aan offertes", "prospect", 1000)]

    with caplog.at_level(logging.INFO, logger="sales_copilot.modules.detector.__main__"):
        await _drive_consume_transcripts(
            messages, config=config, injector=injector, window_classifier=classifier
        )

    assert classifier.calls == 1
    assert injector.dispatched == [("offerteproces", 0.9, "we zitten uren aan offertes", 1000)]

    summary_records = [r for r in caplog.records if "Detector session summary" in r.getMessage()]
    assert summary_records
    summary_msg = summary_records[0].getMessage()
    assert "classified=1" in summary_msg
    assert "dispatched=1" in summary_msg


# --- detector status published to the dashboard ------------------------------
#
# #203 put the counters in the log; the seller does not read the log. These
# tests cover the wiring that carries the same counters onto the hub: the three
# moments that must never be throttled away (loop start, first chunk, closing
# summary) and the fact that the payload never carries transcript text.


class _RecordingStatusPublisher:
    """Stands in for DetectorStatusPublisher, recording every publish call."""

    def __init__(self) -> None:
        self.publishes: list[dict] = []
        self.chunks_noted = 0

    def note_chunk(self) -> None:
        self.chunks_noted += 1

    async def publish(self, counts, *, force: bool = False, stopped: bool = False) -> bool:
        self.publishes.append({"counts": dict(counts), "force": force, "stopped": stopped})
        return True


async def test_consume_transcripts_publishes_status_before_the_first_chunk() -> None:
    """The dashboard must be able to say "listening" from the moment the loop
    is up, not only once transcript traffic starts."""
    publisher = _RecordingStatusPublisher()
    config = DetectorConfig(min_chunks_to_classify=1000)

    await _drive_consume_transcripts([], config=config, status_publisher=publisher)

    assert publisher.publishes, "expected a proof-of-life publish at loop start"
    first = publisher.publishes[0]
    assert first["force"] is True
    assert first["counts"]["received"] == 0


async def test_consume_transcripts_forces_a_publish_on_the_first_chunk() -> None:
    publisher = _RecordingStatusPublisher()
    config = DetectorConfig(min_chunks_to_classify=1000)
    messages = [
        _transcript_message("hallo daar", "prospect", 1000),
        _transcript_message("en verder", "prospect", 2000),
    ]

    await _drive_consume_transcripts(messages, config=config, status_publisher=publisher)

    forced_with_a_chunk = [
        entry for entry in publisher.publishes
        if entry["force"] and not entry["stopped"] and entry["counts"]["received"] == 1
    ]
    assert forced_with_a_chunk, "the first chunk must force a status publish"
    assert publisher.chunks_noted == 2


async def test_consume_transcripts_publishes_a_final_stopped_summary() -> None:
    publisher = _RecordingStatusPublisher()
    config = DetectorConfig(min_chunks_to_classify=1000)
    messages = [_transcript_message("hallo daar", "prospect", 1000)]

    await _drive_consume_transcripts(messages, config=config, status_publisher=publisher)

    last = publisher.publishes[-1]
    assert last["stopped"] is True
    assert last["force"] is True
    assert last["counts"]["received"] == 1


async def test_status_publish_carries_counters_only_never_the_utterance() -> None:
    publisher = _RecordingStatusPublisher()
    config = DetectorConfig(min_chunks_to_classify=1000)
    secret = "ons offerteproces kost twintig uur per week"

    await _drive_consume_transcripts(
        [_transcript_message(secret, "prospect", 1000)],
        config=config,
        status_publisher=publisher,
    )

    for entry in publisher.publishes:
        assert secret not in json.dumps(entry["counts"])
        assert all(isinstance(value, int) for value in entry["counts"].values())


async def test_consume_transcripts_still_runs_without_a_status_publisher() -> None:
    """The publisher is optional: a detector with no hub still consumes."""
    injector = _FakeInjector()
    config = DetectorConfig(min_chunks_to_classify=1000)

    await _drive_consume_transcripts(
        [_transcript_message("hallo daar", "prospect", 1000)],
        config=config,
        injector=injector,
    )
