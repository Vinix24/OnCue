import json
import types

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector import __main__ as detector_main


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
