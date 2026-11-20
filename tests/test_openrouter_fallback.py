"""OpenRouter fallback models. The SDK seam is stubbed; one test drives the real OpenAI client and
instructor over an ``httpx.MockTransport``. Nothing touches the network."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_routing import TASKS, build_llm_client, resolve_llm, task_fallback_models_key
from sales_copilot.core.privacy_gate import PrivacyGateError


class _Answer(BaseModel):
    answer: str


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Error code: {status_code}")
        self.status_code = status_code


def _wrapped(status: int) -> Exception:
    outer = RuntimeError("retries exhausted")
    outer.__cause__ = _HttpError(status)
    return outer


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for task in TASKS:
        monkeypatch.delenv(task_fallback_models_key(task), raising=False)
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "b1,b2")


class _Scripted:
    """Per-model outcome: an exception to raise, a string to answer, or a chunk list to stream."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            outcome = self.outcomes[kwargs["model"]]
            if isinstance(outcome, BaseException):
                raise outcome
            return kwargs["response_model"](answer=outcome)

        return _create

    def build_create_partial(self, client: object, provider: str):
        def _partial(*, response_model: Any, **kwargs: Any):
            self.calls.append(kwargs)
            outcome = self.outcomes[kwargs["model"]]
            if isinstance(outcome, BaseException):
                raise outcome
            for chunk in outcome:
                if isinstance(chunk, BaseException):
                    raise chunk
                yield response_model(answer=chunk)

        return _partial

    @property
    def models(self) -> list[str]:
        return [call["model"] for call in self.calls]


@pytest.fixture()
def install(monkeypatch: pytest.MonkeyPatch):
    def _install(outcomes: dict[str, Any]) -> _Scripted:
        scripted = _Scripted(outcomes)
        monkeypatch.setattr(llm_client_mod, "build_client", lambda provider, *, timeout_ms, **_: object())
        monkeypatch.setattr(llm_client_mod, "build_create", scripted.build_create)
        monkeypatch.setattr(llm_client_mod, "build_create_partial", scripted.build_create_partial)
        return scripted

    return _install


def _client(provider: str = "openrouter", **kwargs: Any):
    config = DetectorConfig(llm_provider=provider, llm_model="primary", llm_timeout_ms=5000, **kwargs)
    return build_llm_client(resolve_llm("suggestions", config))


def _call(client: Any) -> Any:
    return client.create(model="primary", system_prompt="s", user_text="u", response_model=_Answer)


async def _stream(client: Any) -> list[str]:
    stream = client.astream(model="primary", system_prompt="s", user_text="u", response_model=_Answer)
    return [item.answer async for item in stream]


def test_settings_task_list_wins_then_parent_then_global(monkeypatch: pytest.MonkeyPatch) -> None:
    config = DetectorConfig(llm_provider="openrouter", llm_model="primary")
    assert resolve_llm("summary", config).fallback_models == ("b1", "b2")
    monkeypatch.setenv("REPORT_LLM_FALLBACK_MODELS", "r1")
    assert resolve_llm("report_terms", config).fallback_models == ("r1",)
    monkeypatch.setenv("SUMMARY_LLM_FALLBACK_MODELS", " t1 , ,t2,t1,primary ")
    assert resolve_llm("summary", config).fallback_models == ("t1", "t2")


def test_a_non_openrouter_provider_ignores_the_list(install) -> None:
    scripted = install({"primary": _HttpError(500), "b1": "from-b1"})
    assert resolve_llm("suggestions", DetectorConfig(llm_provider="groq", llm_model="primary")).fallback_models == ()
    for client in (_client("groq"), llm_client_mod.LLMClient("groq", timeout_ms=5000, fallback_models=("b1",))):
        with pytest.raises(_HttpError):
            _call(client)
    assert scripted.models == ["primary", "primary"]


def test_first_model_500_and_the_second_answers(install, caplog: pytest.LogCaptureFixture) -> None:
    scripted = install({"primary": _HttpError(500), "b1": "from-b1"})
    caplog.set_level(logging.WARNING, logger=llm_client_mod.logger.name)
    assert _call(_client()).answer == "from-b1"
    assert scripted.models == ["primary", "b1"]
    message = next(r.getMessage() for r in caplog.records if "fallback" in r.getMessage())
    for part in ("task=suggestions", "failed_model=primary", "status=HTTP 500", "next_model=b1"):
        assert part in message


@pytest.mark.parametrize(
    "error",
    [_HttpError(429), _HttpError(503), _wrapped(502), httpx.ConnectError("x"), httpx.ReadTimeout("x"), TimeoutError()],
)
def test_retryable_errors_fall_back(error: Exception, install) -> None:
    scripted = install({"primary": error, "b1": "from-b1"})
    assert _call(_client()).answer == "from-b1"
    assert scripted.models == ["primary", "b1"]


@pytest.mark.parametrize(
    "error", [_HttpError(400), _HttpError(402), _HttpError(404), _wrapped(401), ValueError("x"), Exception()]
)
def test_other_errors_do_not_fall_back(error: Exception, install) -> None:
    scripted = install({"primary": error, "b1": "from-b1"})
    with pytest.raises(type(error)):
        _call(_client())
    assert scripted.models == ["primary"]


def test_all_models_fail_and_the_last_error_surfaces(install) -> None:
    scripted = install({"primary": _HttpError(500), "b1": _HttpError(429), "b2": _HttpError(503)})
    with pytest.raises(_HttpError) as raised:
        _call(_client())
    assert raised.value.status_code == 503
    assert scripted.models == ["primary", "b1", "b2"]


def test_a_400_in_a_fallback_stops_the_chain_and_no_list_means_no_fallback(
    install, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted = install({"primary": _HttpError(500), "b1": _HttpError(400), "b2": "never"})
    with pytest.raises(_HttpError, match="400"):
        _call(_client())
    assert scripted.models == ["primary", "b1"]
    monkeypatch.delenv("LLM_FALLBACK_MODELS")
    with pytest.raises(_HttpError, match="500"):
        _call(_client())
    assert scripted.models == ["primary", "b1", "primary"]


def test_every_attempt_passes_the_privacy_ceiling(install) -> None:
    scripted = install({"primary": _HttpError(500), "b1": "from-b1"})
    client = _client(privacy="public")
    checks: list[int] = []
    real = client._enforce_ceiling

    def _counting() -> None:
        checks.append(1)
        real()
        if len(checks) == 3:  # entry, first attempt, then the ceiling drops before the fallback
            raise PrivacyGateError("ceiling lowered")

    client._enforce_ceiling = _counting  # type: ignore[method-assign]
    with pytest.raises(PrivacyGateError):
        _call(client)
    assert scripted.models == ["primary"] and len(checks) == 3


async def test_astream_falls_back_before_the_first_chunk(install) -> None:
    scripted = install({"primary": _HttpError(500), "b1": ["a", "ab"]})
    assert await _stream(_client()) == ["a", "ab"]
    assert scripted.models == ["primary", "b1"]
    scripted.outcomes["b1"] = "whole"
    result = await _client().acreate(model="primary", system_prompt="s", user_text="u", response_model=_Answer)
    assert result.answer == "whole" and scripted.models[2:] == ["primary", "b1"]


async def test_astream_does_not_fall_back_after_the_first_chunk(install) -> None:
    scripted = install({"primary": ["a", _HttpError(500)], "b1": ["never"]})
    seen: list[str] = []
    client = _client()
    with pytest.raises(_HttpError):
        async for item in client.astream(model="primary", system_prompt="s", user_text="u", response_model=_Answer):
            seen.append(item.answer)
    assert seen == ["a"] and scripted.models == ["primary"]


def test_the_real_sdk_and_instructor_fall_back_over_a_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import OpenAI

    requested: list[str] = []
    call = {"id": "c", "type": "function", "function": {"name": "_Answer", "arguments": '{"answer": "from-b1"}'}}
    body = {
        "id": "g1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
    }  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["model"])
        if requested[-1] == "primary":
            return httpx.Response(500, json={"error": {"message": "down"}})
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        llm_client_mod, "build_client",
        lambda provider, *, timeout_ms, **_: OpenAI(base_url="https://x.test", api_key="k", http_client=http, max_retries=0),  # noqa: E501
    )  # fmt: skip
    assert _call(_client()).answer == "from-b1"
    # instructor may retry the failing model itself; what matters is the order and the end.
    assert requested[-1] == "b1" and set(requested[:-1]) == {"primary"}


def _real_client(monkeypatch: pytest.MonkeyPatch, handler: Any, fallback: str | None, timeout_ms: int = 5000):
    """The real build_client (OpenAI SDK + instructor) over a MockTransport; no network."""
    import openai

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", fallback or "")
    http = httpx.Client(transport=httpx.MockTransport(handler))

    class _Mocked(openai.OpenAI):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**{**kw, "http_client": http, "base_url": "https://x.test"})

    monkeypatch.setattr(openai, "OpenAI", _Mocked)
    config = DetectorConfig(llm_provider="openrouter", llm_model="primary", llm_timeout_ms=timeout_ms)
    return build_llm_client(resolve_llm("suggestions", config))


def _answer_body(text: str) -> dict[str, Any]:
    call = {"id": "c", "type": "function", "function": {"name": "_Answer", "arguments": json.dumps({"answer": text})}}
    return {
        "id": "g1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
    }  # fmt: skip


def test_with_fallback_a_500_costs_exactly_one_request_to_the_first_model(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["model"])
        if requested[-1] == "primary":
            return httpx.Response(500, json={"error": {"message": "down"}})
        return httpx.Response(200, json=_answer_body("from-b1"))

    assert _call(_real_client(monkeypatch, handler, "b1")).answer == "from-b1"
    assert requested == ["primary", "b1"]


def test_without_fallback_the_sdk_default_retries_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=_answer_body("ok")) if len(requested) == 3 else httpx.Response(429, json={})

    monkeypatch.setattr("time.sleep", lambda _s: None)  # the SDK backoff, not under test
    assert _call(_real_client(monkeypatch, handler, None)).answer == "ok"
    assert requested == ["primary"] * 3


def test_each_attempt_gets_an_equal_share_and_a_hanging_first_model_leaves_the_next_its_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["model"])
        if requested[-1] == "primary":
            # A hang longer than the share: MockTransport enforces no timeout, so mimic httpx.
            share = request.extensions["timeout"]["read"]
            time.sleep(share)
            raise httpx.ReadTimeout("hang", request=request)
        return httpx.Response(200, json=_answer_body("from-b1"))

    client = _real_client(monkeypatch, handler, "b1", timeout_ms=1000)
    started = time.monotonic()
    assert _call(client).answer == "from-b1"
    assert time.monotonic() - started < client.live_timeout_s
    assert requested == ["primary", "b1"]


def test_the_share_is_passed_per_attempt_only_with_fallback(install) -> None:
    scripted = install({"primary": _HttpError(500), "b1": "x"})
    _call(_client())
    assert [c["timeout"] for c in scripted.calls] == pytest.approx([5 / 3, 5 / 3])
    assert all("max_retries" in c for c in scripted.calls)  # 5 s over primary, b1, b2 (the fixture's list)
    monkeypatch_calls = install({"primary": "x"})
    llm_client_mod.LLMClient("openrouter", timeout_ms=5000).create(
        model="primary", system_prompt="s", user_text="u", response_model=_Answer
    )
    assert "timeout" not in monkeypatch_calls.calls[0] and "max_retries" not in monkeypatch_calls.calls[0]
