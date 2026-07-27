from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.window_classifier import (
    PHASE_FOCUS,
    WindowAnalysis,
    WindowClassifier,
    WindowDetection,
)


def _make_classifier(provider: str, monkeypatch: pytest.MonkeyPatch) -> WindowClassifier:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    if provider == "gemini":
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    config = DetectorConfig(
        llm_provider=provider,
        llm_model="test-model",
        pain_points_config="config/pain_points.yaml",
        objections_config="config/objections.yaml",
    )
    return WindowClassifier(config, provider)


def test_empty_window_text_returns_empty_detections(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)
    classifier._llm._create = MagicMock(side_effect=AssertionError("should not be called"))

    result = asyncio.run(classifier.classify(window_text="", latest_chunk=None))

    assert result.detections == []
    classifier._llm._create.assert_not_called()


def test_whitespace_only_window_returns_empty_detections(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)
    classifier._llm._create = MagicMock(side_effect=AssertionError("should not be called"))

    result = asyncio.run(classifier.classify(window_text="   \n  ", latest_chunk=None))

    assert result.detections == []


async def test_openai_path_passes_messages_list(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)
    captured: dict = {}

    def mock_create(**kwargs):
        captured.update(kwargs)
        return WindowAnalysis(detections=[])

    classifier._llm._create = mock_create

    await classifier._call_model_sync("we hebben te weinig mensen", "discovery")

    assert "messages" in captured
    assert "config" not in captured
    assert isinstance(captured["messages"], list)
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["role"] == "user"


async def test_gemini_path_passes_messages_not_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("gemini", monkeypatch)
    captured: dict = {}

    def mock_create(**kwargs):
        captured.update(kwargs)
        return WindowAnalysis(detections=[])

    classifier._llm._create = mock_create
    await classifier._call_model_sync("we doen offertes nog steeds in Word", "discovery")

    assert "messages" in captured
    assert "contents" not in captured
    assert "config" not in captured
    assert "temperature" not in captured
    assert any("we doen offertes nog steeds in Word" in str(m.get("content", "")) for m in captured["messages"])


async def test_phase_aware_prompt_includes_correct_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)
    captured_user_prompts: list[str] = []

    def mock_create(**kwargs):
        msgs = kwargs.get("messages", [])
        for m in msgs:
            if m.get("role") == "user":
                captured_user_prompts.append(m["content"])
        return WindowAnalysis(detections=[])

    classifier._llm._create = mock_create

    await classifier._call_model_sync("text", "pitch")

    assert captured_user_prompts, "user prompt was not captured"
    assert "pitch" in captured_user_prompts[0]
    assert PHASE_FOCUS["pitch"][:30] in captured_user_prompts[0]


async def test_invalid_llm_response_returns_empty_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)
    classifier._llm._create = MagicMock(side_effect=RuntimeError("LLM unavailable"))

    result = await classifier._call_model_sync("we verliezen offertes", "discovery")

    assert isinstance(result, WindowAnalysis)
    assert result.detections == []


def test_system_prompt_contains_pain_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier("openai", monkeypatch)

    assert "offerteproces" in classifier.system_prompt
    assert "capaciteit" in classifier.system_prompt
    assert "NIET als detection markeren" in classifier.system_prompt


# ---------------------------------------------------------------------------
# WindowClassifierHooks + opt-in streaming (v2 e2e-latency-harness seam)
# ---------------------------------------------------------------------------


class _RecordingHooks:
    def __init__(self) -> None:
        self.start_calls = 0
        self.end_calls: list[tuple[WindowAnalysis, float, float | None]] = []

    def on_classify_start(self) -> None:
        self.start_calls += 1

    def on_classify_end(self, analysis: WindowAnalysis, latency_ms: float, ttft_ms: float | None) -> None:
        self.end_calls.append((analysis, latency_ms, ttft_ms))


def _make_classifier_with_hooks(
    provider: str, monkeypatch: pytest.MonkeyPatch, hooks: _RecordingHooks
) -> WindowClassifier:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    config = DetectorConfig(
        llm_provider=provider,
        llm_model="test-model",
        pain_points_config="config/pain_points.yaml",
        objections_config="config/objections.yaml",
    )
    return WindowClassifier(config, provider, hooks=hooks)


async def test_classify_default_does_not_stream_and_uses_acreate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production call sites never pass ``use_streaming`` -- default must keep hitting
    the existing non-streaming ``_create`` seam, not ``astream``."""
    hooks = _RecordingHooks()
    classifier = _make_classifier_with_hooks("openai", monkeypatch, hooks)
    classifier._llm._create = MagicMock(return_value=WindowAnalysis(detections=[]))
    classifier._llm.astream = MagicMock(side_effect=AssertionError("astream must not be called by default"))

    result = await classifier.classify(window_text="we hebben te weinig mensen", latest_chunk=None)

    assert result == WindowAnalysis(detections=[])
    classifier._llm._create.assert_called_once()
    assert hooks.start_calls == 1
    assert len(hooks.end_calls) == 1
    analysis, latency_ms, ttft_ms = hooks.end_calls[0]
    assert analysis == result
    assert latency_ms >= 0
    assert ttft_ms is None  # non-streaming path never populates TTFT


async def test_classify_streaming_drains_astream_and_populates_ttft(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = _RecordingHooks()
    classifier = _make_classifier_with_hooks("openai", monkeypatch, hooks)
    expected = WindowAnalysis(
        detections=[
            WindowDetection(
                category="pain_point",
                subcategory="offerteproces",
                confidence=0.9,
                evidence_quote="offertes kosten te veel tijd",
                reasoning="duidelijk pijnpunt",
            )
        ]
    )
    partial = WindowAnalysis(detections=[])
    captured: dict = {}

    async def fake_astream(**kwargs):
        captured.update(kwargs)
        classifier._llm._last_ttft_ms = 12.5
        yield partial
        yield expected

    classifier._llm.astream = fake_astream
    classifier._llm._create = MagicMock(side_effect=AssertionError("acreate must not be called when streaming"))

    result = await classifier.classify(
        window_text="offertes kosten te veel tijd", latest_chunk=None, use_streaming=True
    )

    assert result == expected  # last yielded item wins, not the first partial
    assert captured["model"] == classifier.config.llm_model
    assert captured["response_model"] is WindowAnalysis
    assert hooks.start_calls == 1
    assert len(hooks.end_calls) == 1
    analysis, latency_ms, ttft_ms = hooks.end_calls[0]
    assert analysis == expected
    assert latency_ms >= 0
    assert ttft_ms == pytest.approx(12.5)


async def test_classify_streaming_no_yields_returns_empty_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = _RecordingHooks()
    classifier = _make_classifier_with_hooks("openai", monkeypatch, hooks)

    async def empty_astream(**_kwargs):
        return
        yield  # pragma: no cover -- makes this an async generator

    classifier._llm.astream = empty_astream

    result = await classifier.classify(window_text="niets bijzonders", latest_chunk=None, use_streaming=True)

    assert result == WindowAnalysis(detections=[])


async def test_hook_exception_is_swallowed_and_does_not_break_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingHooks:
        def on_classify_start(self) -> None:
            raise RuntimeError("boom")

        def on_classify_end(self, *_args: object) -> None:
            raise RuntimeError("boom")

    classifier = _make_classifier_with_hooks("openai", monkeypatch, _RaisingHooks())
    classifier._llm._create = MagicMock(return_value=WindowAnalysis(detections=[]))

    result = await classifier.classify(window_text="we hebben te weinig mensen", latest_chunk=None)

    assert result == WindowAnalysis(detections=[])


def test_no_hooks_defaults_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """WindowClassifier() without a hooks= kwarg (every existing production call
    site) must keep working exactly as before -- covered by the pre-existing
    _make_classifier() tests above, this just asserts the no-op default exists."""
    classifier = _make_classifier("openai", monkeypatch)
    assert classifier._hooks is not None
    classifier._hooks.on_classify_start()
    classifier._hooks.on_classify_end(WindowAnalysis(detections=[]), 1.0, None)
