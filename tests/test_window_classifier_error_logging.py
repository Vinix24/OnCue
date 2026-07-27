from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.window_classifier import WindowAnalysis, WindowClassifier


def _make_classifier(monkeypatch: pytest.MonkeyPatch, provider: str = "openai") -> WindowClassifier:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    if provider == "gemini":
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    config = DetectorConfig(
        llm_provider=provider,
        llm_model="gpt-4",
        pain_points_config="config/pain_points.yaml",
        objections_config="config/objections.yaml",
    )
    return WindowClassifier(config, provider)


async def test_llm_failure_logs_error_with_provider_context(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    classifier = _make_classifier(monkeypatch)
    classifier._llm._create = MagicMock(side_effect=RuntimeError("api timeout"))

    with caplog.at_level(logging.ERROR, logger="sales_copilot.modules.detector.window_classifier"):
        result = await classifier._call_model_sync("te duur", "discovery")

    assert result == WindowAnalysis(detections=[])
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "Expected at least one ERROR log record"
    msg = error_records[0].getMessage()
    assert "openai" in msg
    assert "gpt-4" in msg
    assert "RuntimeError" in msg
    assert "api timeout" in msg


async def test_llm_failure_includes_window_preview_in_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    classifier = _make_classifier(monkeypatch)
    classifier._llm._create = MagicMock(side_effect=ValueError("bad response"))
    window_text = "we hebben problemen met capaciteit"

    with caplog.at_level(logging.ERROR, logger="sales_copilot.modules.detector.window_classifier"):
        await classifier._call_model_sync(window_text, "discovery")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records
    msg = error_records[0].getMessage()
    assert "we hebben problemen" in msg


async def test_llm_failure_returns_empty_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    classifier = _make_classifier(monkeypatch)
    classifier._llm._create = MagicMock(side_effect=Exception("network error"))

    result = await classifier._call_model_sync("test input", "discovery")

    assert result == WindowAnalysis(detections=[])


async def test_classify_async_logs_error_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    classifier = _make_classifier(monkeypatch)
    classifier._llm._create = MagicMock(side_effect=RuntimeError("async failure"))

    with caplog.at_level(logging.ERROR, logger="sales_copilot.modules.detector.window_classifier"):
        result = await classifier.classify(window_text="test text", latest_chunk=None)

    assert result.detections == []
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records
    assert "RuntimeError" in error_records[0].getMessage()


async def test_llm_failure_includes_exc_type_in_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    classifier = _make_classifier(monkeypatch)
    classifier._llm._create = MagicMock(side_effect=ConnectionError("network down"))

    with caplog.at_level(logging.ERROR, logger="sales_copilot.modules.detector.window_classifier"):
        await classifier._call_model_sync("prospect zegt iets", "pitch")

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records
    msg = error_records[0].getMessage()
    assert "ConnectionError" in msg
    assert "network down" in msg
