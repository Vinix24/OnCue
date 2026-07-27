"""Unit tests for the central LLM client seam.

Covers provider routing (all six providers, vertex/azure no longer raise), the gemini/vertex
temperature-omission quirk, and the three-mode outbound PII policy. The SDK clients are mocked;
no test touches the network.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core import outbound_policy
from sales_copilot.core.llm_client import LLMClient, build_client, build_create
from sales_copilot.core.thinking_policy import ThinkingPolicy

_REPO_ROOT = Path(__file__).resolve().parent.parent


class _Resp(BaseModel):
    value: str = ""


class _FakeOllamaCreateResponse:
    """Stand-in for the httpx.Response returned by a successful ``/api/create``."""

    def raise_for_status(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _stub_ollama_num_ctx_ensure(monkeypatch: pytest.MonkeyPatch):
    """Safety net: ``LLMClient.create()`` for provider=ollama now calls
    ``_ensure_ollama_num_ctx_model``, which POSTs to Ollama's ``/api/create``. No test in this
    suite (or elsewhere) may depend on a real (or absent) local Ollama server, so this stubs
    ``httpx.post`` to a no-op success by default and resets the process-level "already
    created" cache before and after every test. Individual tests override the stub via their
    own ``monkeypatch.setattr`` call when they need to observe or fail the POST."""
    llm_client_mod._ollama_ensured_models.clear()
    monkeypatch.setattr(llm_client_mod.httpx, "post", lambda *a, **k: _FakeOllamaCreateResponse())
    yield
    llm_client_mod._ollama_ensured_models.clear()


# ---------------------------------------------------------------------------
# build_client — provider routing + timeout injection
# ---------------------------------------------------------------------------


def test_build_client_openai_passes_timeout_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    calls: dict = {}

    def _fake_openai(**kwargs):
        calls.update(kwargs)
        return sentinel

    monkeypatch.setattr("openai.OpenAI", _fake_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = build_client("openai", timeout_ms=7000)

    assert result is sentinel
    assert calls["api_key"] == "sk-test"
    assert calls["timeout"] == 7.0


def test_build_client_azure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: azure used to crash phase_detector at construction (ValueError)."""
    sentinel = object()
    calls: dict = {}

    def _fake_azure(**kwargs):
        calls.update(kwargs)
        return sentinel

    monkeypatch.setattr("openai.AzureOpenAI", _fake_azure)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://tenant.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

    result = build_client("azure", timeout_ms=5000)

    assert result is sentinel
    assert calls["azure_endpoint"] == "https://tenant.openai.azure.com"
    assert calls["api_version"] == "2024-10-21"
    assert calls["timeout"] == 5.0


def test_build_client_groq_uses_groq_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.delenv("GROQ_BASE_URL", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")

    build_client("groq", timeout_ms=7000)

    assert calls["base_url"] == "https://api.groq.com/openai/v1"
    assert calls["timeout"] == 7.0


def test_build_client_openrouter_uses_openrouter_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

    build_client("openrouter", timeout_ms=7000)

    assert calls["base_url"] == "https://openrouter.ai/api/v1"
    assert calls["api_key"] == "or-test"
    assert calls["timeout"] == 7.0


def test_build_client_ollama_uses_local_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    build_client("ollama", timeout_ms=7000)

    assert calls["base_url"] == "http://localhost:11434/v1"


def test_env_example_ollama_base_url_includes_v1() -> None:
    """Regression: .env.example shipped OLLAMA_BASE_URL without /v1, so the OpenAI-compatible
    adapter 404'd against Ollama's chat/completions endpoint out of the box."""
    env_example = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    match = re.search(r"^OLLAMA_BASE_URL=(\S+)$", env_example, flags=re.MULTILINE)

    assert match is not None, "OLLAMA_BASE_URL not found in .env.example"
    assert match.group(1) == "http://localhost:11434/v1"


def test_build_client_gemini_injects_http_options_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}
    monkeypatch.setattr("google.genai.Client", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")

    build_client("gemini", timeout_ms=7000)

    assert "http_options" in calls
    assert calls.get("vertexai") is None  # gemini path is not vertex
    # google-genai expresses the timeout in milliseconds.
    assert calls["http_options"].timeout == 7000


def test_build_client_vertex_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: vertex used to crash phase_detector at construction (ValueError)."""
    calls: dict = {}
    monkeypatch.setattr("google.genai.Client", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.setenv("GCP_PROJECT", "proj-x")
    monkeypatch.setenv("GCP_LOCATION", "europe-west4")

    build_client("vertex", timeout_ms=7000)

    assert calls["vertexai"] is True
    assert calls["project"] == "proj-x"
    assert calls["location"] == "europe-west4"
    assert calls["http_options"].timeout == 7000


def test_build_client_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        build_client("banana", timeout_ms=7000)


@pytest.mark.parametrize(
    "provider,env_var",
    [
        ("openai", "OPENAI_API_KEY"),
        ("azure", "AZURE_OPENAI_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ],
)
def test_build_client_empty_key_raises_clear_error(
    provider: str, env_var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard fires before SDK construction, so a misconfigured key fails loudly.
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: object())
    monkeypatch.setattr("openai.AzureOpenAI", lambda **kwargs: object())
    monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(ValueError, match=env_var):
        build_client(provider, timeout_ms=7000)


def test_build_client_gemini_empty_keys_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("google.genai.Client", lambda **kwargs: object())
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_API_KEY/GEMINI_API_KEY"):
        build_client("gemini", timeout_ms=7000)


def test_build_client_ollama_defaults_placeholder_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ollama is local: an empty key must not raise; it falls back to a placeholder.
    calls: dict = {}
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: calls.update(kwargs) or object())
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    build_client("ollama", timeout_ms=7000)

    assert calls["api_key"] == "ollama"


# ---------------------------------------------------------------------------
# build_create — instructor patching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_build_create_genai_uses_from_genai(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    fake_instructor = MagicMock()
    fake_instructor.Mode.GENAI_STRUCTURED_OUTPUTS = "GENAI_STRUCTURED_OUTPUTS"
    monkeypatch.setattr(llm_client_mod, "instructor", fake_instructor)

    client = object()
    build_create(client, provider)

    fake_instructor.from_genai.assert_called_once_with(client, mode="GENAI_STRUCTURED_OUTPUTS")
    fake_instructor.from_openai.assert_not_called()


@pytest.mark.parametrize("provider", ["openai", "azure", "groq", "openrouter"])
def test_build_create_openai_family_uses_from_openai(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    fake_instructor = MagicMock()
    monkeypatch.setattr(llm_client_mod, "instructor", fake_instructor)

    client = object()
    build_create(client, provider)

    fake_instructor.from_openai.assert_called_once_with(client)
    fake_instructor.from_genai.assert_not_called()


def test_build_create_ollama_uses_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: Mode.TOOLS (the from_openai default) cannot parse local Ollama tool-calls
    that omit required schema fields, so summarize() silently returned None. Mode.JSON asks
    the model to answer with a JSON blob directly, which local models honor reliably."""
    from unittest.mock import MagicMock

    fake_instructor = MagicMock()
    fake_instructor.Mode.JSON = "JSON"
    monkeypatch.setattr(llm_client_mod, "instructor", fake_instructor)

    client = object()
    build_create(client, "ollama")

    fake_instructor.from_openai.assert_called_once_with(client, mode="JSON")
    fake_instructor.from_genai.assert_not_called()


def test_build_create_hosted_providers_do_not_use_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted providers keep the from_openai default (tool-calling); only ollama is scoped
    to Mode.JSON so this fix cannot regress openai/azure/groq behavior."""
    from unittest.mock import MagicMock

    for provider in ("openai", "azure", "groq", "openrouter"):
        fake_instructor = MagicMock()
        monkeypatch.setattr(llm_client_mod, "instructor", fake_instructor)

        build_create(object(), provider)

        # Called with exactly one positional arg (the client) — no mode kwarg.
        fake_instructor.from_openai.assert_called_once_with(fake_instructor.from_openai.call_args[0][0])
        assert "mode" not in fake_instructor.from_openai.call_args.kwargs


# ---------------------------------------------------------------------------
# LLMClient.create — temperature omission + message assembly
# ---------------------------------------------------------------------------


def _capturing_client(provider: str, monkeypatch: pytest.MonkeyPatch) -> tuple[LLMClient, list[dict]]:
    """Build an LLMClient whose SDK construction is mocked and whose _create records kwargs."""
    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return _Resp(value="ok")

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: _capture)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: None)
    client = LLMClient(provider, timeout_ms=7000)
    return client, calls


def test_create_openai_passes_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client("openai", monkeypatch)

    client.create(
        model="gpt-4o-mini",
        system_prompt="sys",
        user_text="hallo zonder pii",
        response_model=_Resp,
        temperature=0.3,
    )

    kwargs = calls[0]
    assert kwargs["temperature"] == 0.3
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["messages"][1]["role"] == "user"
    assert kwargs["messages"][1]["content"] == "hallo zonder pii"


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_create_genai_omits_temperature(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(
        model="gemini-2.5-flash",
        system_prompt="sys",
        user_text="hallo",
        response_model=_Resp,
        temperature=0.3,
    )

    assert "temperature" not in calls[0]
    assert "contents" not in calls[0]
    assert "messages" in calls[0]


def test_create_openai_passes_max_tokens_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """A thinking-capable model needs a generous explicit budget to avoid finish_reason='length'."""
    client, calls = _capturing_client("openai", monkeypatch)

    client.create(
        model="gpt-4o-mini",
        system_prompt="sys",
        user_text="hallo",
        response_model=_Resp,
        max_tokens=4096,
    )

    assert calls[0]["max_tokens"] == 4096


def test_create_omits_max_tokens_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every existing caller omits max_tokens -- provider default stays unchanged."""
    client, calls = _capturing_client("openai", monkeypatch)

    client.create(
        model="gpt-4o-mini",
        system_prompt="sys",
        user_text="hallo",
        response_model=_Resp,
    )

    assert "max_tokens" not in calls[0]


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_create_genai_omits_max_tokens_even_when_given(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The genai adapter has no equivalent top-level kwarg on this seam; ignore rather than break."""
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(
        model="gemini-2.5-flash",
        system_prompt="sys",
        user_text="hallo",
        response_model=_Resp,
        max_tokens=4096,
    )

    assert "max_tokens" not in calls[0]


# ---------------------------------------------------------------------------
# LLMClient.create / astream — prompt caching (stable system prefix)
# ---------------------------------------------------------------------------


def test_create_openrouter_anthropic_model_marks_system_prompt_cacheable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _capturing_client("openrouter", monkeypatch)

    client.create(
        model="anthropic/claude-haiku-4.5",
        system_prompt="stable system + cards",
        user_text="volatile transcript",
        response_model=_Resp,
    )

    system_message, user_message = calls[0]["messages"]
    assert system_message == {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": "stable system + cards",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    # The volatile per-turn user message never carries a cache marker.
    assert user_message == {"role": "user", "content": "volatile transcript"}


def test_create_openrouter_non_anthropic_model_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only anthropic/... models get cache_control -- OpenRouter only passes it through for
    the Anthropic-backed models it proxies."""
    client, calls = _capturing_client("openrouter", monkeypatch)

    client.create(
        model="qwen/qwen3.6-35b-a3b",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
    )

    assert calls[0]["messages"][0] == {"role": "system", "content": "s"}


@pytest.mark.parametrize("provider", ["gemini", "vertex", "openai", "azure", "groq", "ollama"])
def test_create_non_openrouter_providers_never_get_cache_marker(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only openrouter gets cache_control -- gemini/vertex have their own (unimplemented-here)
    caching mechanism, and openai/azure/groq/ollama have no equivalent wire field. Uses an
    anthropic/... model string to prove the provider check, not the model check, is what gates
    this -- these paths must stay byte-identical to before this feature."""
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(
        model="anthropic/claude-haiku-4.5",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
    )

    assert calls[0]["messages"][0] == {"role": "system", "content": "s"}


def test_create_prompt_cache_disabled_via_constructor_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return _Resp(value="ok")

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: _capture)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: None)
    client = LLMClient("openrouter", timeout_ms=7000, prompt_cache=False)

    client.create(
        model="anthropic/claude-haiku-4.5",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
    )

    assert calls[0]["messages"][0] == {"role": "system", "content": "s"}


def test_create_output_identical_with_and_without_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching only changes the outbound request shape -- the parsed response object returned
    to the caller is untouched either way."""
    cached_client, _ = _capturing_client("openrouter", monkeypatch)

    uncached_calls: list[dict] = []

    def _capture(**kwargs):
        uncached_calls.append(kwargs)
        return _Resp(value="ok")

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: _capture)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: None)
    uncached_client = LLMClient("openrouter", timeout_ms=7000, prompt_cache=False)

    cached_result = cached_client.create(
        model="anthropic/claude-haiku-4.5", system_prompt="s", user_text="hallo", response_model=_Resp
    )
    uncached_result = uncached_client.create(
        model="anthropic/claude-haiku-4.5", system_prompt="s", user_text="hallo", response_model=_Resp
    )

    assert cached_result == uncached_result == _Resp(value="ok")


@pytest.mark.asyncio
async def test_astream_openrouter_anthropic_model_marks_system_prompt_cacheable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [_Resp(value="ok")]
    client, partial_calls, _ = _capturing_stream_client("openrouter", monkeypatch, chunks)

    async for _ in client.astream(
        model="anthropic/claude-haiku-4.5",
        system_prompt="stable",
        user_text="volatile",
        response_model=_Resp,
    ):
        pass

    system_message, user_message = partial_calls[0]["messages"]
    assert system_message == {
        "role": "system",
        "content": [
            {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}}
        ],
    }
    assert user_message == {"role": "user", "content": "volatile"}


@pytest.mark.asyncio
async def test_astream_non_anthropic_model_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_Resp(value="ok")]
    client, partial_calls, _ = _capturing_stream_client("openrouter", monkeypatch, chunks)

    async for _ in client.astream(
        model="qwen/qwen3.6-35b-a3b",
        system_prompt="stable",
        user_text="volatile",
        response_model=_Resp,
    ):
        pass

    assert partial_calls[0]["messages"][0] == {"role": "system", "content": "stable"}


# ---------------------------------------------------------------------------
# _extract_usage / last_cache_read_tokens -- surfacing cache-hit evidence
# ---------------------------------------------------------------------------


class _UsageAnthropicShape:
    """Stand-in for a usage object carrying the native Anthropic cache field."""

    def __init__(self, cache_read_input_tokens: int) -> None:
        self.prompt_tokens = 500
        self.completion_tokens = 20
        self.total_tokens = 520
        self.cache_read_input_tokens = cache_read_input_tokens


class _PromptTokensDetails:
    def __init__(self, cached_tokens: int) -> None:
        self.cached_tokens = cached_tokens


class _UsageOpenAIShape:
    """Stand-in for a usage object carrying the OpenAI-compatible nested cache field."""

    def __init__(self, cached_tokens: int) -> None:
        self.prompt_tokens = 500
        self.completion_tokens = 20
        self.total_tokens = 520
        self.prompt_tokens_details = _PromptTokensDetails(cached_tokens)


class _RespWithUsage:
    def __init__(self, usage: object) -> None:
        self.usage = usage


def test_extract_usage_reads_native_cache_read_input_tokens() -> None:
    response = _RespWithUsage(_UsageAnthropicShape(cache_read_input_tokens=480))

    usage = llm_client_mod._extract_usage(response)

    assert usage["cache_read_tokens"] == 480


def test_extract_usage_reads_openai_style_prompt_tokens_details() -> None:
    response = _RespWithUsage(_UsageOpenAIShape(cached_tokens=480))

    usage = llm_client_mod._extract_usage(response)

    assert usage["cache_read_tokens"] == 480


def test_extract_usage_defaults_cache_read_tokens_to_zero_when_absent() -> None:
    usage_obj = _UsageAnthropicShape(cache_read_input_tokens=0)
    del usage_obj.cache_read_input_tokens  # simulate a shape with neither cache field at all
    response = _RespWithUsage(usage_obj)

    usage = llm_client_mod._extract_usage(response)

    assert usage["cache_read_tokens"] == 0


def test_create_surfaces_cache_read_tokens_on_last_usage_and_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _capture(**kwargs):
        return _RespWithUsage(_UsageOpenAIShape(cached_tokens=480))

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: _capture)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: None)
    client = LLMClient("openrouter", timeout_ms=7000)

    client.create(
        model="anthropic/claude-haiku-4.5", system_prompt="s", user_text="hallo", response_model=_Resp
    )

    assert client.last_usage["cache_read_tokens"] == 480
    assert client.last_cache_read_tokens == 480


def test_last_cache_read_tokens_is_none_before_any_call() -> None:
    client = LLMClient("none", timeout_ms=7000)

    assert client.last_cache_read_tokens is None


# ---------------------------------------------------------------------------
# LLMClient.create — three-mode PII policy applied to user_text
# ---------------------------------------------------------------------------

_PII = "Jan de Vries, BSN 123456782"


@pytest.fixture(autouse=True)
def _reset_pii_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PII_REDACTION", raising=False)
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
    outbound_policy._warned_reasons.clear()


def test_create_cloud_only_redacts_for_public_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client("gemini", monkeypatch)

    client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp)

    content = calls[0]["messages"][1]["content"]
    assert "123456782" not in content
    assert "[BSN]" in content
    # system prompt is never touched
    assert calls[0]["messages"][0]["content"] == "s"


def test_create_cloud_only_passes_raw_to_local_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp, allow_local=True)

    assert calls[0]["messages"][1]["content"] == _PII


def test_create_always_mode_redacts_even_for_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_REDACTION", "always")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp, allow_local=True)

    content = calls[0]["messages"][1]["content"]
    assert "123456782" not in content
    assert "[BSN]" in content


def test_create_off_mode_passes_raw_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    client, calls = _capturing_client("gemini", monkeypatch)

    with caplog.at_level(logging.WARNING, logger="sales_copilot.core.outbound_policy"):
        client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp)
        client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp)

    # Raw PII passes through in off-mode.
    assert calls[0]["messages"][1]["content"] == _PII
    assert calls[1]["messages"][1]["content"] == _PII
    # The skip is logged — and only once per process.
    skip_warnings = [r for r in caplog.records if "SKIPPED" in r.getMessage()]
    assert len(skip_warnings) == 1
    assert "off" in skip_warnings[0].getMessage()


def test_off_mode_via_legacy_allow_raw_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward compat: ALLOW_RAW_LLM_PII truthy forces mode off."""
    monkeypatch.setenv("ALLOW_RAW_LLM_PII", "true")
    client, calls = _capturing_client("openai", monkeypatch)

    client.create(model="m", system_prompt="s", user_text=_PII, response_model=_Resp)

    assert calls[0]["messages"][1]["content"] == _PII


# ---------------------------------------------------------------------------
# Ollama num_ctx workaround — Ollama's runtime default (4096) starves thinking-capable
# local models of context, so structured calls route through a derived, num_ctx-baked model
# instead of the raw configured model. extra_body/top-level num_ctx are silently ignored by
# Ollama's OpenAI-compatible endpoint (verified against Ollama 0.31.1); only a Modelfile-baked
# model actually changes the loaded context window (confirmed via `ollama ps`).
# ---------------------------------------------------------------------------


def test_ollama_num_ctx_default_is_32768(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)

    assert llm_client_mod._ollama_num_ctx() == 32768


def test_ollama_num_ctx_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")

    assert llm_client_mod._ollama_num_ctx() == 8192


def test_ollama_num_ctx_invalid_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", "not-a-number")

    assert llm_client_mod._ollama_num_ctx() == 32768


def test_ollama_num_ctx_non_positive_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", "0")

    assert llm_client_mod._ollama_num_ctx() == 32768


def test_derived_ollama_model_name_flattens_colon() -> None:
    assert llm_client_mod._derived_ollama_model_name("gemma4:e4b", 32768) == "gemma4-e4b-numctx32768"


def test_ensure_ollama_num_ctx_model_posts_to_api_create_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def _fake_post(url, *, json, timeout):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeOllamaCreateResponse()

    monkeypatch.setattr(llm_client_mod.httpx, "post", _fake_post)

    name1 = llm_client_mod._ensure_ollama_num_ctx_model("http://localhost:11434/v1", "gemma4:e4b", 32768)
    name2 = llm_client_mod._ensure_ollama_num_ctx_model("http://localhost:11434/v1", "gemma4:e4b", 32768)

    assert name1 == name2 == "gemma4-e4b-numctx32768"
    assert len(calls) == 1  # second call is served from the process-level cache
    assert calls[0]["url"] == "http://localhost:11434/api/create"  # /v1 stripped: native surface
    assert calls[0]["json"] == {
        "model": "gemma4-e4b-numctx32768",
        "from": "gemma4:e4b",
        "parameters": {"num_ctx": 32768},
        "stream": False,
    }


def test_ensure_ollama_num_ctx_model_failure_falls_back_to_base_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(*a, **k):
        raise httpx.ConnectError("no ollama here")

    monkeypatch.setattr(llm_client_mod.httpx, "post", _raise)

    with caplog.at_level(logging.WARNING, logger="sales_copilot.core.llm_client"):
        result = llm_client_mod._ensure_ollama_num_ctx_model("http://localhost:11434/v1", "gemma4:e4b", 32768)

    assert result == "gemma4:e4b"  # unchanged base model — degraded, not failed
    assert any("num_ctx" in r.getMessage() for r in caplog.records)
    # Not cached on failure, so a later call (e.g. once Ollama comes back up) retries.
    assert ("http://localhost:11434/v1", "gemma4:e4b", 32768) not in llm_client_mod._ollama_ensured_models


def test_create_ollama_swaps_model_for_derived_num_ctx_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(model="gemma4:e4b", system_prompt="s", user_text="hallo", response_model=_Resp)

    assert calls[0]["model"] == "gemma4-e4b-numctx32768"


def test_create_ollama_respects_num_ctx_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", "8192")
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(model="gemma4:e4b", system_prompt="s", user_text="hallo", response_model=_Resp)

    assert calls[0]["model"] == "gemma4-e4b-numctx8192"


@pytest.mark.parametrize("provider", ["openai", "azure", "groq", "openrouter", "gemini", "vertex"])
def test_create_hosted_providers_keep_raw_model_and_never_call_ollama_create(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_calls: list = []
    monkeypatch.setattr(llm_client_mod.httpx, "post", lambda *a, **k: post_calls.append((a, k)))
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(model="some-model", system_prompt="s", user_text="hallo", response_model=_Resp)

    assert calls[0]["model"] == "some-model"
    assert post_calls == []


# ---------------------------------------------------------------------------
# LLMClient.create — thinking-policy threading (fase-B thinking-sweep)
# ---------------------------------------------------------------------------


def test_create_without_thinking_arg_omits_thinking_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (thinking=None) callers must see byte-identical behavior to before this feature."""
    client, calls = _capturing_client("gemini", monkeypatch)

    client.create(model="gemini-2.5-flash", system_prompt="s", user_text="hallo", response_model=_Resp)

    assert "thinking_config" not in calls[0]


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_create_genai_thinking_off_sets_budget_zero(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(
        model="m",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        thinking=ThinkingPolicy(name="no-think", thinking_on=False, reasoning_budget=None),
    )

    assert calls[0]["thinking_config"].thinking_budget == 0


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_create_genai_thinking_on_sets_the_requested_budget(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client(provider, monkeypatch)

    client.create(
        model="m",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        thinking=ThinkingPolicy(name="think-2048", thinking_on=True, reasoning_budget=2048),
    )

    assert calls[0]["thinking_config"].thinking_budget == 2048


def test_create_openrouter_thinking_merges_extra_body(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _capturing_client("openrouter", monkeypatch)

    client.create(
        model="qwen/qwen3.6-35b-a3b",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        thinking=ThinkingPolicy(name="think-512", thinking_on=True, reasoning_budget=512),
    )

    assert calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning": {"max_tokens": 512},
    }


def test_create_openai_thinking_policy_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """GPT-4o is not a thinking model: passing a policy anyway must not add any kwarg."""
    client, calls = _capturing_client("openai", monkeypatch)

    client.create(
        model="gpt-4o",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        thinking=ThinkingPolicy(name="think-2048", thinking_on=True, reasoning_budget=2048),
    )

    assert "thinking_config" not in calls[0]
    assert "extra_body" not in calls[0]


def test_create_ollama_default_uses_env_num_ctx_when_no_thinking_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(model="gemma4:e4b", system_prompt="s", user_text="hallo", response_model=_Resp)

    assert calls[0]["model"] == "gemma4-e4b-numctx32768"


def test_create_ollama_thinking_budget_overrides_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama has no request-level thinking kwarg -- the budget maps onto num_ctx instead."""
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")  # would be used if the policy override didn't win
    client, calls = _capturing_client("ollama", monkeypatch)

    client.create(
        model="gemma4:e4b",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        thinking=ThinkingPolicy(name="think-8192", thinking_on=True, reasoning_budget=8192),
    )

    assert calls[0]["model"] == "gemma4-e4b-numctx8192"
    assert "thinking_config" not in calls[0]
    assert "extra_body" not in calls[0]


# ---------------------------------------------------------------------------
# LLMClient.astream -- partial streaming + TTFT
# ---------------------------------------------------------------------------


def _capturing_stream_client(
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
    chunks: list,
    *,
    sleep_before_first_s: float = 0.0,
):
    """Build an LLMClient whose SDK construction is mocked and whose ``create_partial`` is a
    fake sync generator yielding ``chunks`` in order. Also stubs the non-streaming ``create``
    seam (returning the last chunk) so a gemini/vertex fallback in the same test is covered by
    the same helper. Returns ``(client, partial_calls, create_calls)``.
    """
    partial_calls: list[dict] = []
    create_calls: list[dict] = []

    def _fake_create(**kwargs):
        create_calls.append(kwargs)
        return chunks[-1]

    def _fake_create_partial(**kwargs):
        partial_calls.append(kwargs)
        if sleep_before_first_s:
            time.sleep(sleep_before_first_s)
        yield from chunks

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: _fake_create)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: _fake_create_partial)
    client = LLMClient(provider, timeout_ms=7000)
    return client, partial_calls, create_calls


@pytest.mark.asyncio
async def test_astream_yields_partials_in_order_and_final_object(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_Resp(value="W"), _Resp(value="Wa"), _Resp(value="Wat is de blokkade?")]
    client, partial_calls, _ = _capturing_stream_client("openrouter", monkeypatch, chunks)

    received = [
        item
        async for item in client.astream(
            model="anthropic/claude-haiku-4.5",
            system_prompt="s",
            user_text="hallo",
            response_model=_Resp,
        )
    ]

    assert received == chunks
    assert received[-1].value == "Wat is de blokkade?"
    assert len(partial_calls) == 1


@pytest.mark.asyncio
async def test_astream_ttft_is_recorded_and_less_than_full_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [_Resp(value="a"), _Resp(value="ab"), _Resp(value="abc")]
    client, _, _ = _capturing_stream_client(
        "openrouter", monkeypatch, chunks, sleep_before_first_s=0.05
    )

    start = time.perf_counter()
    async for _ in client.astream(
        model="m", system_prompt="s", user_text="hallo", response_model=_Resp
    ):
        pass
    total_ms = (time.perf_counter() - start) * 1000

    assert client.last_ttft_ms is not None
    assert client.last_ttft_ms > 0
    assert client.last_ttft_ms < total_ms


@pytest.mark.asyncio
async def test_astream_passes_max_tokens_for_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_Resp(value="ok")]
    client, partial_calls, _ = _capturing_stream_client("openrouter", monkeypatch, chunks)

    async for _ in client.astream(
        model="m",
        system_prompt="s",
        user_text="hallo",
        response_model=_Resp,
        max_tokens=512,
    ):
        pass

    assert partial_calls[0]["max_tokens"] == 512


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
@pytest.mark.asyncio
async def test_astream_gemini_vertex_falls_back_to_acreate_single_item(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gemini/vertex never build create_partial -- astream degrades to one acreate() yield."""
    chunks = [_Resp(value="final")]
    client, partial_calls, create_calls = _capturing_stream_client(provider, monkeypatch, chunks)

    received = [
        item
        async for item in client.astream(
            model="gemini-2.5-flash",
            system_prompt="s",
            user_text="hallo",
            response_model=_Resp,
            max_tokens=512,
        )
    ]

    assert received == [chunks[-1]]
    assert partial_calls == []  # create_partial is never built for genai providers
    assert len(create_calls) == 1
    # The existing genai max_tokens-skip rule (create()) still applies through the fallback.
    assert "max_tokens" not in create_calls[0]
    assert client.last_ttft_ms is not None


@pytest.mark.asyncio
async def test_astream_provider_none_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: None)
    client = LLMClient("none", timeout_ms=7000)

    received = [
        item
        async for item in client.astream(
            model="m", system_prompt="s", user_text="hallo", response_model=_Resp
        )
    ]

    assert received == []
    assert client.last_ttft_ms is None


@pytest.mark.asyncio
async def test_astream_propagates_producer_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_create_partial(**kwargs):
        if False:
            yield  # pragma: no cover -- makes this a generator function
        raise RuntimeError("stream blew up")

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: lambda **k: _Resp(value="unused"))
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: _fake_create_partial)
    client = LLMClient("openrouter", timeout_ms=7000)

    with pytest.raises(RuntimeError, match="stream blew up"):
        async for _ in client.astream(
            model="m", system_prompt="s", user_text="hallo", response_model=_Resp
        ):
            pass


@pytest.mark.asyncio
async def test_astream_builds_create_partial_lazily_and_caches_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_partial must not be built at LLMClient construction time (see build_create_partial
    docstring) -- only the first astream() call builds it, and a second call reuses it."""
    build_calls: list[str] = []

    def _fake_create_partial(**kwargs):
        yield _Resp(value="ok")

    def _fake_build_create_partial(c, p):
        build_calls.append(p)
        return _fake_create_partial

    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: lambda **k: _Resp(value="ok"))
    monkeypatch.setattr(llm_client_mod, "build_create_partial", _fake_build_create_partial)
    client = LLMClient("openrouter", timeout_ms=7000)

    assert build_calls == []  # not built at construction

    for _ in range(2):
        async for _item in client.astream(
            model="m", system_prompt="s", user_text="hallo", response_model=_Resp
        ):
            pass

    assert build_calls == ["openrouter"]  # built once, reused on the second call
