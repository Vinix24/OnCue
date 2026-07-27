"""Tests for the EVAL-ONLY resilience helpers in ``eval_shared.py``.

Covers retry-with-backoff, rate-limit-error detection, inter-call pacing, and the
``LLMClient``-instance wrapper used by ``cascade_eval.build_pipeline_for_spec``. None of
these are wired into ``core/llm_client.py`` or ``modules/detector/llm_confirm.py`` -- the
live copilot's 3s-budget path never runs through them.
"""

from __future__ import annotations

import inspect

import pytest

from sales_copilot.modules.detector.eval_shared import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PACE_MS,
    is_rate_limit_error,
    pace,
    resilience_header_lines,
    retry_with_backoff,
    wrap_llm_client_for_eval,
)


class _GoogleStyleError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _OpenAIStyleError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# is_rate_limit_error
# ---------------------------------------------------------------------------


def test_is_rate_limit_error_detects_google_429_resource_exhausted() -> None:
    exc = _GoogleStyleError(429, "429 RESOURCE_EXHAUSTED. Quota exceeded")
    assert is_rate_limit_error(exc) is True


def test_is_rate_limit_error_detects_openai_style_status_code() -> None:
    exc = _OpenAIStyleError(429, "Rate limit reached")
    assert is_rate_limit_error(exc) is True


def test_is_rate_limit_error_detects_message_only_rate_limit() -> None:
    exc = RuntimeError("openrouter: rate_limit_exceeded, please slow down")
    assert is_rate_limit_error(exc) is True


def test_is_rate_limit_error_false_for_unrelated_error() -> None:
    exc = ValueError("invalid schema")
    assert is_rate_limit_error(exc) is False


def test_is_rate_limit_error_false_for_non_429_status_code() -> None:
    exc = _OpenAIStyleError(500, "internal server error")
    assert is_rate_limit_error(exc) is False


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------


def test_retry_with_backoff_succeeds_after_transient_rate_limit_errors() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _OpenAIStyleError(429, "rate_limit_exceeded")
        return "ok"

    result = retry_with_backoff(
        flaky,
        max_retries=4,
        base_delay_s=1.0,
        sleep=sleeps.append,
        rand=lambda: 0.0,
    )

    assert result == "ok"
    assert attempts["n"] == 3
    # Two retries before success: delays 1.0s (2**0) then 2.0s (2**1), zero jitter.
    assert sleeps == [1.0, 2.0]


def test_retry_with_backoff_raises_after_exhausting_retries() -> None:
    attempts = {"n": 0}

    def always_fails() -> None:
        attempts["n"] += 1
        raise _OpenAIStyleError(429, "rate_limit_exceeded")

    with pytest.raises(_OpenAIStyleError):
        retry_with_backoff(always_fails, max_retries=2, base_delay_s=0.0, sleep=lambda s: None, rand=lambda: 0.0)

    # Initial attempt + 2 retries = 3 calls total.
    assert attempts["n"] == 3


def test_retry_with_backoff_does_not_retry_non_retryable_error() -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def raises_value_error() -> None:
        attempts["n"] += 1
        raise ValueError("schema mismatch")

    with pytest.raises(ValueError):
        retry_with_backoff(raises_value_error, sleep=sleeps.append, rand=lambda: 0.0)

    assert attempts["n"] == 1
    assert sleeps == []


def test_retry_with_backoff_returns_immediately_on_first_success() -> None:
    sleeps: list[float] = []

    result = retry_with_backoff(lambda: "first-try", sleep=sleeps.append)

    assert result == "first-try"
    assert sleeps == []


# ---------------------------------------------------------------------------
# pace
# ---------------------------------------------------------------------------


def test_pace_sleeps_configured_duration() -> None:
    sleeps: list[float] = []

    pace(150, sleep=sleeps.append)

    assert sleeps == [0.15]


def test_pace_zero_is_a_no_op() -> None:
    sleeps: list[float] = []

    pace(0, sleep=sleeps.append)

    assert sleeps == []


# ---------------------------------------------------------------------------
# wrap_llm_client_for_eval
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """Stand-in for ``core.llm_client.LLMClient`` -- only ``.create`` is exercised."""

    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> str:
        self.calls += 1
        return "ok"


def test_wrap_llm_client_for_eval_paces_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeLLMClient()
    attempts = {"n": 0}
    original_create = fake.create

    def flaky_create(**kwargs: object) -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _OpenAIStyleError(429, "rate_limit_exceeded")
        return original_create(**kwargs)

    fake.create = flaky_create  # type: ignore[method-assign]

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    wrap_llm_client_for_eval(fake, max_retries=2, base_delay_s=0.0, pace_ms=150)
    result = fake.create(model="x")

    assert result == "ok"
    assert attempts["n"] == 2
    # One pace sleep (0.15s) before the call, then one backoff sleep before the retry.
    assert sleeps[0] == pytest.approx(0.15)
    assert len(sleeps) == 2


def test_wrap_llm_client_for_eval_only_touches_this_instance() -> None:
    """Wrapping one instance must not leak onto the class or any other instance."""
    wrapped = _FakeLLMClient()
    untouched = _FakeLLMClient()

    wrap_llm_client_for_eval(wrapped, pace_ms=0)

    assert "create" in wrapped.__dict__  # wrapped: instance-level override present
    assert "create" not in untouched.__dict__  # sibling instance: still the unmutated class method


# ---------------------------------------------------------------------------
# resilience_header_lines
# ---------------------------------------------------------------------------


def test_resilience_header_lines_reports_effective_config() -> None:
    lines = resilience_header_lines(effective_timeout_ms=45000, pace_ms=200, thinking_output_tokens=4096)
    joined = "\n".join(lines)

    assert "45000 ms" in joined
    assert "200 ms" in joined
    assert "4096 tokens" in joined
    assert f"{DEFAULT_MAX_RETRIES} retries" in joined


def test_resilience_header_lines_omits_thinking_budget_when_not_given() -> None:
    lines = resilience_header_lines(effective_timeout_ms=7000)
    joined = "\n".join(lines)

    assert "thinking output-token" not in joined.lower()


def test_default_constants_match_dispatch_spec() -> None:
    """4 retries, 1s base delay (1/2/4/8s schedule), 150ms default pace."""
    assert DEFAULT_MAX_RETRIES == 4
    assert DEFAULT_BASE_DELAY_S == 1.0
    assert DEFAULT_PACE_MS == 150


# ---------------------------------------------------------------------------
# Live-path isolation: the retry/backoff machinery must stay eval-scoped
# ---------------------------------------------------------------------------


def test_live_llm_confirm_module_has_no_eval_resilience_wiring() -> None:
    """``modules/detector/llm_confirm.py`` -- on the live copilot's 3s-budget path -- must
    never import or reference the eval-only retry/backoff/pacing helpers. A multi-second
    exponential backoff there would blow the live coaching latency budget."""
    import sales_copilot.modules.detector.llm_confirm as llm_confirm_mod

    source = inspect.getsource(llm_confirm_mod)

    assert "retry_with_backoff" not in source
    assert "wrap_llm_client_for_eval" not in source
    assert "eval_shared" not in source


def test_live_core_llm_client_create_has_no_retry_loop() -> None:
    """``core/llm_client.py``'s shared ``LLMClient.create`` -- used by every live LLM call
    (confirm_async, summary, suggestions, phase_detector, window_classifier) -- must never
    gain a retry/backoff loop of its own. Eval callers wrap their OWN instances instead (see
    ``wrap_llm_client_for_eval``); the shared seam itself must stay a single-shot call."""
    from sales_copilot.core import llm_client as llm_client_mod

    source = inspect.getsource(llm_client_mod.LLMClient.create)

    assert "retry_with_backoff" not in source
    assert "time.sleep" not in source
