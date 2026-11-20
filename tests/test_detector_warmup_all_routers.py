"""Objection router and script tracker model warm-up at detector start, shutdown, blackhole text."""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import pytest

from sales_copilot.audio import blackhole as blackhole_module
from sales_copilot.core import i18n
from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.coaching import script_tracker as script_module
from sales_copilot.modules.coaching.script_tracker import ScriptPoint, ScriptRouter
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector import router as router_module
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter

CATALOGS = Path(__file__).resolve().parents[1] / "config" / "i18n"


class _FakeSemanticRouter:
    def __call__(self, text: str):
        return None


def _slow_builder(monkeypatch: pytest.MonkeyPatch, module, delay: float) -> list[float]:
    calls: list[float] = []

    def build(routes, model):
        calls.append(time.monotonic())
        time.sleep(delay)
        return _FakeSemanticRouter()

    monkeypatch.setattr(module, "_build_router", build)
    return calls


def _script_router() -> ScriptRouter:
    point = ScriptPoint(
        id="intro",
        title="Intro",
        phase="opening",
        required=True,
        example_phrases=["thanks for taking the time today"],
    )
    return ScriptRouter(DetectorConfig.from_env(), [point])


def _objection_router() -> ObjectionRouter:
    return ObjectionRouter(DetectorConfig.from_env(), "en")


@pytest.mark.parametrize(
    ("factory", "module"),
    [(_objection_router, router_module), (_script_router, script_module)],
    ids=["objection", "script"],
)
def test_one_build_a_waiting_call_and_failure_fallback(factory, module, monkeypatch) -> None:
    router = factory()
    calls = _slow_builder(monkeypatch, module, 0.4)
    warm = threading.Thread(target=router.warm_up)
    warm.start()
    while not calls:
        time.sleep(0.005)
    start = time.monotonic()
    router._ensure_router()  # what classify() calls; must wait on the running build
    assert time.monotonic() - start >= 0.2
    warm.join()
    assert router.warm_up() is True
    assert len(calls) == 1

    failing = factory()
    attempts: list[int] = []

    def build(routes, model):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("model unavailable")
        return _FakeSemanticRouter()

    monkeypatch.setattr(module, "_build_router", build)
    assert failing.warm_up() is False
    assert failing._router is None  # the failure stays visible, nothing cached
    failing._ensure_router()
    assert len(attempts) == 2


def test_objection_detector_warm_up_delegates_to_its_router() -> None:
    seen: list[bool] = []
    detector = ObjectionDetector.__new__(ObjectionDetector)
    detector._router = types.SimpleNamespace(warm_up=lambda: seen.append(True) or True)
    assert detector.warm_up() is True
    assert seen == [True]


async def test_start_warmup_does_not_block_the_loop_and_reports_failure() -> None:
    def slow() -> bool:
        time.sleep(0.2)
        return True

    def boom() -> bool:
        raise RuntimeError("no model")

    started = time.monotonic()
    task = detector_main._start_warmup(slow, "t-slow")
    assert time.monotonic() - started < 0.1
    assert await task is True
    assert await detector_main._start_warmup(boom, "t-boom") is False


async def test_shutdown_does_not_wait_for_an_unfinished_warmup() -> None:
    release = threading.Event()
    thread_started = threading.Event()

    def stuck() -> bool:
        thread_started.set()
        release.wait(timeout=30)
        return True

    task = detector_main._start_warmup(stuck, "t-stuck")
    assert thread_started.wait(2)
    started = time.monotonic()
    await detector_main._cancel_warmups([task, None])
    assert time.monotonic() - started < 0.5
    assert task.cancelled()
    worker = next(t for t in threading.enumerate() if t.name == "t-stuck")
    assert worker.daemon  # a daemon thread is not joined at interpreter exit
    release.set()
    worker.join(2)  # finishing after cancel must not raise into a closed loop


def _drive_main(monkeypatch, *, script_allowed: bool, objections: bool) -> dict[str, list[str]]:
    started: dict[str, list[str]] = {"names": []}
    real_start = detector_main._start_warmup

    def record(warm_up, name):
        started["names"].append(name)
        return real_start(lambda: True, name)

    monkeypatch.setattr(detector_main, "_start_warmup", record)

    class _WS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _CaseDB:
        def __init__(self, *a, **k) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def list_cases(self):
            return []

    async def noop(*a, **k) -> None:
        return None

    class _Tracker:
        def __init__(self, *a, **k) -> None:
            self.router = types.SimpleNamespace(warm_up=lambda: True)

        async def checkpoint(self) -> None:
            return None

    class _Engine:
        def __init__(self, *a) -> None:
            pass

        async def run(self, stop_event) -> None:
            await stop_event.wait()

    ns = types.SimpleNamespace
    monkeypatch.setattr(detector_main.websockets, "connect", lambda *a, **k: _WS())
    monkeypatch.setattr(detector_main, "PainPointRouter", lambda *a, **k: ns(routes=[], warm_up=lambda: True))
    monkeypatch.setattr(detector_main, "LLMConfirmClient", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "PainPointDebouncer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "DetectionPipeline", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "SQLiteCaseDB", _CaseDB)
    monkeypatch.setattr(
        detector_main, "load_preset", lambda name, language=None: ns(name=name, objections={}, ui_labels={})
    )
    monkeypatch.setattr(detector_main, "SlideInjector", lambda *a, **k: ns(close=noop))
    monkeypatch.setattr(detector_main, "SlidingWindowBuffer", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "WindowClassifier", lambda *a, **k: object())
    monkeypatch.setattr(detector_main, "ObjectionDetector", lambda *a, **k: ns(close=noop, warm_up=lambda: True))
    monkeypatch.setattr(detector_main, "ScriptTracker", _Tracker)
    monkeypatch.setattr(detector_main, "ScriptTrackerEngine", _Engine)
    monkeypatch.setattr(detector_main, "get_feature_policy", lambda: ns(allows=lambda feature: script_allowed))
    monkeypatch.setattr(detector_main, "_print_banner", lambda *a, **k: None)
    monkeypatch.setattr(detector_main, "_consume_transcripts", noop)
    return started


def _config(objections: bool) -> DetectorConfig:
    return DetectorConfig(
        llm_provider="none",
        enable_objection_detection=objections,
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
        enable_script_tracking=True,
    )


async def test_all_three_warmups_start_at_detector_start(monkeypatch) -> None:
    started = _drive_main(monkeypatch, script_allowed=True, objections=True)
    await detector_main.main(detector_config=_config(True), register_signals=False)
    assert started["names"] == ["router-warmup", "objection-warmup", "script-warmup"]


async def test_no_script_warmup_when_the_tier_cannot_use_it(monkeypatch) -> None:
    started = _drive_main(monkeypatch, script_allowed=False, objections=True)
    await detector_main.main(detector_config=_config(True), register_signals=False)
    assert "script-warmup" not in started["names"]
    assert "objection-warmup" in started["names"]


async def test_no_objection_warmup_when_objection_detection_is_off(monkeypatch) -> None:
    started = _drive_main(monkeypatch, script_allowed=False, objections=False)
    await detector_main.main(detector_config=_config(False), register_signals=False)
    assert started["names"] == ["router-warmup"]


@pytest.fixture
def real_catalogs():
    i18n.set_catalog_dir(CATALOGS)
    yield
    i18n.set_catalog_dir(None)


@pytest.mark.parametrize(
    ("lang", "text"),
    [
        ("en", "InputStream could not be opened on 'BlackHole 2ch'"),
        ("nl", "InputStream kon niet openen op 'BlackHole 2ch'"),
    ],
)
def test_blackhole_attach_failure_detail_follows_language(real_catalogs, lang, text) -> None:
    assert i18n.t("audio.detail_inputstream_open_failed_on", lang=lang, device="BlackHole 2ch") == text


@pytest.mark.parametrize(("lang", "needle"), [("en", "could not be opened on"), ("nl", "kon niet openen op")])
def test_blackhole_start_failure_records_the_localised_detail(real_catalogs, monkeypatch, lang, needle) -> None:
    monkeypatch.setenv("LANGUAGE", lang)
    stream = blackhole_module.BlackHoleStream.__new__(blackhole_module.BlackHoleStream)
    marked: list[str] = []
    stream._lock = threading.Lock()
    stream._stream = None
    stream._device_name = "BlackHole 2ch"
    stream._reopen_backoff_s = 1.0
    stream._track = types.SimpleNamespace(reset=lambda: None)
    stream._health = types.SimpleNamespace(mark_attach_failed=marked.append)

    def fail() -> None:
        raise OSError("no device")

    monkeypatch.setattr(stream, "_open", fail)
    with pytest.raises(OSError):
        stream.start()
    assert len(marked) == 1
    assert needle in marked[0]
    assert "BlackHole 2ch" in marked[0]
