import time
from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.core.llm_client import build_client, build_create
from sales_copilot.core.thinking_policy import ThinkingPolicy
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient, PainPointDetection


@pytest.mark.parametrize("provider", ["gemini", "openai", "groq", "ollama", "azure"])
def test_client_initializes_for_providers(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    if provider == "gemini":
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    if provider == "ollama":
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    if provider == "azure":
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://tenant.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    client = LLMConfirmClient()

    assert client.provider == provider
    assert client._llm is not None


def test_seam_azure_builds_azure_openai_client_with_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam builds AzureOpenAI with the BYO-tenant env vars plus the injected timeout."""
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key-123")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://mytenant.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    sentinel = MagicMock(name="AzureOpenAI_instance")

    with patch("openai.AzureOpenAI", return_value=sentinel) as mock_cls:
        result = build_client("azure", timeout_ms=7000)

    mock_cls.assert_called_once_with(
        api_key="az-key-123",
        azure_endpoint="https://mytenant.openai.azure.com",
        api_version="2024-10-21",
        timeout=7.0,
    )
    assert result is sentinel


def test_seam_azure_routes_through_instructor_from_openai() -> None:
    """Azure must land on instructor.from_openai, not from_genai."""
    sentinel_client = MagicMock(name="AzureOpenAI_instance")
    fake_patched = MagicMock()
    fake_patched.chat.completions.create = MagicMock(name="create")

    with patch("sales_copilot.core.llm_client.instructor") as mock_instructor:
        mock_instructor.from_openai.return_value = fake_patched
        mock_instructor.from_genai = MagicMock(side_effect=AssertionError("must not call from_genai for azure"))

        create_fn = build_create(sentinel_client, "azure")

    mock_instructor.from_openai.assert_called_once_with(sentinel_client)
    assert create_fn is fake_patched.chat.completions.create


def test_confirm_returns_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    client = LLMConfirmClient()

    client._llm._create = lambda **kwargs: PainPointDetection(
        category="offerteproces",
        confidence=0.72,
        trigger_phrase="offertes",
    )

    result = client.confirm("We doen offertes nog steeds in Word")

    assert isinstance(result, PainPointDetection)
    assert result.category == "offerteproces"
    assert result.confidence == 0.72


def test_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "10")
    client = LLMConfirmClient()

    def slow_call(_: str) -> PainPointDetection:
        time.sleep(0.05)
        return PainPointDetection(
            category="kosten",
            confidence=0.6,
            trigger_phrase="te duur",
        )

    client._call_model = slow_call

    start = time.monotonic()
    result = client.confirm("het is te duur")
    elapsed = time.monotonic() - start

    assert result is None
    # Generous CI-safe guard against a pathological hang, not a micro-benchmark.
    # The real timeout proof is `result is None` above (a live call returns a
    # PainPointDetection, never None). 0.1s rode the edge on shared CI runners.
    assert elapsed < 1.0


def test_thinking_policy_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behavior for existing callers: no thinking policy unless explicitly passed."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    client = LLMConfirmClient()

    assert client._thinking is None


def test_thinking_policy_is_forwarded_to_llm_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    policy = ThinkingPolicy(name="think-512", thinking_on=True, reasoning_budget=512)
    client = LLMConfirmClient(thinking=policy)

    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return PainPointDetection(category="kosten", confidence=0.5, trigger_phrase="duur")

    # Patch LLMClient.create itself (not the inner SDK-level _create) to observe exactly what
    # LLMConfirmClient forwards -- the provider-specific kwarg resolution is llm_client.py's
    # job and is covered separately in test_llm_client.py.
    client._llm.create = _capture
    client.confirm("het is te duur")

    assert calls[0]["thinking"] is policy
