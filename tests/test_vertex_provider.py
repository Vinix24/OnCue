"""Unit tests for the vertex provider path in detector modules."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.detector.suggestions import SuggestionLLMClient
from sales_copilot.modules.detector.window_classifier import WindowClassifier


def _vertex_config() -> DetectorConfig:
    return DetectorConfig(
        llm_provider="vertex",
        llm_model="gemini-2.5-flash",
        pain_points_config="config/pain_points.yaml",
        objections_config="config/objections.yaml",
    )


def _mock_genai_client() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# WindowClassifier
# ---------------------------------------------------------------------------

def test_window_classifier_build_create_vertex_returns_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GCP_LOCATION", "europe-west4")

    fake_client = _mock_genai_client()
    fake_patched = MagicMock()
    fake_create = MagicMock()
    fake_patched.chat.completions.create = fake_create

    with patch("google.genai.Client", return_value=fake_client):
        with patch("instructor.from_genai", return_value=fake_patched) as mock_from_genai:
            config = _vertex_config()
            classifier = WindowClassifier(config, "vertex")

    assert callable(classifier._llm._create)
    mock_from_genai.assert_called_once()


def test_window_classifier_build_client_vertex_passes_project_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GCP_PROJECT", "my-project")
    monkeypatch.setenv("GCP_LOCATION", "us-central1")

    fake_client = _mock_genai_client()
    fake_patched = MagicMock()

    with patch("google.genai.Client", return_value=fake_client) as mock_client_cls:
        with patch("instructor.from_genai", return_value=fake_patched):
            config = _vertex_config()
            WindowClassifier(config, "vertex")

    call_kwargs = mock_client_cls.call_args.kwargs
    assert call_kwargs.get("vertexai") is True
    assert call_kwargs.get("project") == "my-project"
    assert call_kwargs.get("location") == "us-central1"


# ---------------------------------------------------------------------------
# LLMConfirmClient
# ---------------------------------------------------------------------------

def test_llm_confirm_build_create_vertex_returns_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GCP_LOCATION", "europe-west4")

    fake_client = _mock_genai_client()
    fake_patched = MagicMock()

    with patch("google.genai.Client", return_value=fake_client):
        with patch("instructor.from_genai", return_value=fake_patched):
            config = _vertex_config()
            client = LLMConfirmClient(config)

    assert callable(client._llm._create)


def test_llm_confirm_build_client_vertex_uses_vertexai_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("GCP_PROJECT", "proj-x")
    monkeypatch.setenv("GCP_LOCATION", "europe-west1")

    fake_client = _mock_genai_client()
    fake_patched = MagicMock()

    with patch("google.genai.Client", return_value=fake_client) as mock_cls:
        with patch("instructor.from_genai", return_value=fake_patched):
            config = _vertex_config()
            LLMConfirmClient(config)

    call_kwargs = mock_cls.call_args.kwargs
    assert call_kwargs.get("vertexai") is True
    assert call_kwargs.get("project") == "proj-x"


# ---------------------------------------------------------------------------
# SuggestionLLMClient
# ---------------------------------------------------------------------------

def test_suggestion_build_create_vertex_returns_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("GCP_LOCATION", "europe-west4")

    fake_client = _mock_genai_client()
    fake_patched = MagicMock()

    with patch("google.genai.Client", return_value=fake_client):
        with patch("instructor.from_genai", return_value=fake_patched):
            config = _vertex_config()
            client = SuggestionLLMClient(config)

    assert callable(client._llm._create)


def test_suggestion_unsupported_provider_raises() -> None:
    config = DetectorConfig(
        llm_provider="unknown-provider",
        llm_model="some-model",
        pain_points_config="config/pain_points.yaml",
        objections_config="config/objections.yaml",
    )
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        SuggestionLLMClient(config)
