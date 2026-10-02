"""Fallback for models that reject a forced tool choice (e.g. Sonnet 5.5 via OpenRouter)."""

from __future__ import annotations

import logging

import httpx
import openai
import pytest
from pydantic import BaseModel

from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.llm_client import LLMClient

_MODEL = "anthropic/claude-sonnet-5.5"
_FORCED_TOOL_MSG = 'tool_choice: type "tool" and "any" are not supported for this model.'


class _Resp(BaseModel):
    value: str = ""


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": {"message": message}})
    return openai.BadRequestError(f"Error code: 400 - {message}", response=response, body=None)


def _wrapped(exc: Exception) -> Exception:
    """Mimic instructor, which re-raises the SDK error inside its own exception."""
    try:
        raise RuntimeError("InstructorRetryException") from exc
    except RuntimeError as wrapped:
        return wrapped


@pytest.fixture(autouse=True)
def _reset_registry():
    llm_client_mod._no_forced_tool_models.clear()
    yield
    llm_client_mod._no_forced_tool_models.clear()


def _client(monkeypatch: pytest.MonkeyPatch, primary, fallback, partial=None, partial_fallback=None):
    monkeypatch.setattr(llm_client_mod, "build_client", lambda p, *, timeout_ms: object())
    monkeypatch.setattr(llm_client_mod, "build_create", lambda c, p: primary)
    monkeypatch.setattr(llm_client_mod, "build_create_partial", lambda c, p: partial)
    monkeypatch.setattr(llm_client_mod, "build_create_no_forced_tool", lambda c: fallback)
    monkeypatch.setattr(llm_client_mod, "build_create_partial_no_forced_tool", lambda c: partial_fallback)
    return LLMClient("openrouter", timeout_ms=7000)


def _call(client: LLMClient, **extra):
    return client.create(
        model=_MODEL, system_prompt="sys", user_text="hallo", response_model=_Resp, **extra
    )


def test_forced_tool_choice_400_retries_once_then_remembers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    primary_calls: list[dict] = []
    fallback_calls: list[dict] = []

    def primary(**kwargs):
        primary_calls.append(kwargs)
        raise _wrapped(_bad_request(_FORCED_TOOL_MSG))

    def fallback(**kwargs):
        fallback_calls.append(kwargs)
        return _Resp(value="ok")

    client = _client(monkeypatch, primary, fallback)
    with caplog.at_level(logging.INFO, logger=llm_client_mod.logger.name):
        assert _call(client).value == "ok"
        assert _call(client).value == "ok"
        assert _call(client).value == "ok"

    assert len(primary_calls) == 1
    assert len(fallback_calls) == 3
    assert sum("rejects a forced tool choice" in r.getMessage() for r in caplog.records) == 1


def test_fallback_keeps_extra_body_and_cache_control(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_copilot.core.thinking_policy import ThinkingPolicy

    fallback_calls: list[dict] = []

    def primary(**kwargs):
        raise _wrapped(_bad_request(_FORCED_TOOL_MSG))

    def fallback(**kwargs):
        fallback_calls.append(kwargs)
        return _Resp(value="ok")

    client = _client(monkeypatch, primary, fallback)
    _call(client, thinking=ThinkingPolicy(name="t", thinking_on=True, reasoning_budget=512))

    kwargs = fallback_calls[0]
    assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 512}
    system = kwargs["messages"][0]["content"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_other_400_does_not_trigger_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback_calls: list[dict] = []

    def primary(**kwargs):
        raise _wrapped(_bad_request("max_tokens is too large"))

    client = _client(monkeypatch, primary, lambda **kw: fallback_calls.append(kw))
    with pytest.raises(RuntimeError):
        _call(client)

    assert fallback_calls == []
    assert llm_client_mod._no_forced_tool_models == {}


def test_tool_choice_error_that_is_not_a_400_does_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_calls: list[dict] = []

    def primary(**kwargs):
        raise ValueError(_FORCED_TOOL_MSG)

    client = _client(monkeypatch, primary, lambda **kw: fallback_calls.append(kw))
    with pytest.raises(ValueError):
        _call(client)

    assert fallback_calls == []


def test_fallback_is_per_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    primary_calls: list[str] = []

    def primary(**kwargs):
        primary_calls.append(kwargs["model"])
        if kwargs["model"] == _MODEL:
            raise _wrapped(_bad_request(_FORCED_TOOL_MSG))
        return _Resp(value="primary")

    client = _client(monkeypatch, primary, lambda **kw: _Resp(value="fallback"))
    assert _call(client).value == "fallback"
    other = client.create(
        model="anthropic/claude-haiku-4.5", system_prompt="s", user_text="x", response_model=_Resp
    )
    assert other.value == "primary"


@pytest.mark.asyncio
async def test_acreate_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    primary_calls: list[dict] = []

    def primary(**kwargs):
        primary_calls.append(kwargs)
        raise _wrapped(_bad_request(_FORCED_TOOL_MSG))

    client = _client(monkeypatch, primary, lambda **kw: _Resp(value="ok"))
    kwargs = {"model": _MODEL, "system_prompt": "s", "user_text": "x", "response_model": _Resp}
    assert (await client.acreate(**kwargs)).value == "ok"
    assert (await client.acreate(**kwargs)).value == "ok"
    assert len(primary_calls) == 1


async def _collect(client: LLMClient) -> list[_Resp]:
    return [
        item
        async for item in client.astream(
            model=_MODEL, system_prompt="s", user_text="x", response_model=_Resp
        )
    ]


@pytest.mark.asyncio
async def test_astream_retries_once_then_remembers(monkeypatch: pytest.MonkeyPatch) -> None:
    primary_calls: list[dict] = []
    fallback_calls: list[dict] = []

    def partial(**kwargs):
        primary_calls.append(kwargs)
        raise _wrapped(_bad_request(_FORCED_TOOL_MSG))
        yield  # pragma: no cover - makes this a generator

    def partial_fallback(**kwargs):
        fallback_calls.append(kwargs)
        yield _Resp(value="a")
        yield _Resp(value="ab")

    client = _client(monkeypatch, lambda **kw: None, lambda **kw: None, partial, partial_fallback)
    assert [r.value for r in await _collect(client)] == ["a", "ab"]
    assert [r.value for r in await _collect(client)] == ["a", "ab"]

    assert len(primary_calls) == 1
    assert len(fallback_calls) == 2


@pytest.mark.asyncio
async def test_astream_other_400_does_not_trigger_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback_calls: list[dict] = []

    def partial(**kwargs):
        raise _wrapped(_bad_request("max_tokens is too large"))
        yield  # pragma: no cover

    def partial_fallback(**kwargs):
        fallback_calls.append(kwargs)
        yield _Resp(value="x")

    client = _client(monkeypatch, lambda **kw: None, lambda **kw: None, partial, partial_fallback)
    with pytest.raises(RuntimeError):
        await _collect(client)
    assert fallback_calls == []


@pytest.mark.asyncio
async def test_astream_error_after_first_item_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback_calls: list[dict] = []

    def partial(**kwargs):
        yield _Resp(value="a")
        raise _wrapped(_bad_request(_FORCED_TOOL_MSG))

    def partial_fallback(**kwargs):
        fallback_calls.append(kwargs)
        yield _Resp(value="x")

    client = _client(monkeypatch, lambda **kw: None, lambda **kw: None, partial, partial_fallback)
    with pytest.raises(RuntimeError):
        await _collect(client)
    assert fallback_calls == []


def test_no_forced_tool_builders_use_openrouter_structured_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    fake_instructor = MagicMock()
    fake_instructor.Mode.OPENROUTER_STRUCTURED_OUTPUTS = "OSO"
    monkeypatch.setattr(llm_client_mod, "instructor", fake_instructor)
    client = object()

    llm_client_mod.build_create_no_forced_tool(client)
    llm_client_mod.build_create_partial_no_forced_tool(client)

    assert fake_instructor.from_openai.call_args_list[0].kwargs == {"mode": "OSO"}
    assert fake_instructor.from_openai.call_args_list[1].kwargs == {"mode": "OSO"}
