from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.cascade_eval import (
    CascadeEvalHooks,
    ModelSpec,
    PricingTable,
    build_per_model_report,
    build_pipeline_for_spec,
    compute_quality_metrics,
    is_provider_available,
    load_eval_records,
    run_cascade_on_records,
)
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.llm_confirm import PainPointDetection
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import RouteMatch


@dataclass
class _FakeRouter:
    match: RouteMatch | None

    def classify(self, text: str) -> RouteMatch | None:
        return self.match


@dataclass
class _FakeLLM:
    confirmation: object | None = None
    called: int = 0

    def confirm(self, fragment: str) -> object | None:
        self.called += 1
        return self.confirmation


@pytest.fixture
def config() -> DetectorConfig:
    return DetectorConfig(
        llm_provider="none",
        llm_model="none",
        confidence_threshold_low=0.50,
        confidence_threshold_high=0.85,
    )


# ---------------------------------------------------------------------------
# Flow instrumentation
# ---------------------------------------------------------------------------


def test_high_confidence_uses_embedding_fast_path(config: DetectorConfig) -> None:
    hooks = CascadeEvalHooks()
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("offerteproces", 0.95, "high", "embedding")),
        llm_client=llm,
        debouncer=PainPointDebouncer(cooldown_seconds=0),
        hooks=hooks,
    )

    event = pipeline.process("we zitten uren aan offertes", speaker="prospect")

    assert event is not None
    assert event.category == "offerteproces"
    assert llm.called == 0
    assert hooks.last["match"] is not None
    assert hooks.last["match"].source == "embedding"
    assert hooks.last["embedding_latency_ms"] > 0


def test_middle_band_escalates_to_llm(config: DetectorConfig) -> None:
    hooks = CascadeEvalHooks()
    confirmation = PainPointDetection(
        category="offerteproces",
        confidence=0.72,
        trigger_phrase="offertes",
    )
    llm = _FakeLLM(confirmation)
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("offerteproces", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=PainPointDebouncer(cooldown_seconds=0),
        hooks=hooks,
    )

    event = pipeline.process("we zitten uren aan offertes", speaker="prospect")

    assert event is not None
    assert event.category == "offerteproces"
    assert llm.called == 1
    assert hooks.last["confirmation"] is confirmation
    assert hooks.last["llm_latency_ms"] is not None
    assert hooks.last["llm_latency_ms"] >= 0


def test_latency_is_measured_per_layer(config: DetectorConfig) -> None:
    hooks = CascadeEvalHooks()
    confirmation = PainPointDetection(category="kosten", confidence=0.7, trigger_phrase="duur")
    llm = _FakeLLM(confirmation)
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=PainPointDebouncer(cooldown_seconds=0),
        hooks=hooks,
    )

    pipeline.process("het is te duur", speaker="prospect")

    assert hooks.last["embedding_latency_ms"] > 0
    assert hooks.last["llm_latency_ms"] is not None
    assert hooks.last["llm_latency_ms"] > 0


# ---------------------------------------------------------------------------
# Provider availability / no-LLM-key path
# ---------------------------------------------------------------------------


def test_provider_none_is_always_available() -> None:
    ok, reason = is_provider_available("none")
    assert ok
    assert "disabled" in reason


def test_gemini_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ok, reason = is_provider_available("gemini")
    assert not ok
    assert "GEMINI_API_KEY" in reason


def test_openrouter_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ok, reason = is_provider_available("openrouter")
    assert not ok
    assert "OPENROUTER_API_KEY" in reason


def test_openrouter_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    ok, reason = is_provider_available("openrouter")
    assert ok
    assert "OPENROUTER_API_KEY" in reason


def test_runs_without_llm_key_for_uncertain_match(config: DetectorConfig) -> None:
    hooks = CascadeEvalHooks()
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
        llm_client=_FakeLLM(None),
        debouncer=PainPointDebouncer(cooldown_seconds=0),
        hooks=hooks,
    )

    event = pipeline.process("het is te duur", speaker="prospect")

    assert event is None
    assert hooks.last["confirmation"] is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_quality_metrics_on_synthetic_set(config: DetectorConfig) -> None:
    from sales_copilot.modules.detector.cascade_eval import LayerRecord

    valid = frozenset({"offerteproces", "kosten"})
    records = [
        LayerRecord(
            text="t1", source="", ground_truth="offerteproces",
            layer="embedding_fast_path", predicted_category="offerteproces",
            predicted_confidence=0.9, embedding_latency_ms=1.0, llm_latency_ms=None,
            llm_provider=None, llm_model=None, input_tokens=None, output_tokens=None, total_tokens=None,
        ),
        LayerRecord(
            text="t2", source="", ground_truth="kosten",
            layer="embedding_fast_path", predicted_category="kosten",
            predicted_confidence=0.9, embedding_latency_ms=1.0, llm_latency_ms=None,
            llm_provider=None, llm_model=None, input_tokens=None, output_tokens=None, total_tokens=None,
        ),
        LayerRecord(
            text="t3", source="", ground_truth="offerteproces",
            layer="deterministic_reject", predicted_category=None,
            predicted_confidence=0.0, embedding_latency_ms=1.0, llm_latency_ms=None,
            llm_provider=None, llm_model=None, input_tokens=None, output_tokens=None, total_tokens=None,
        ),
        LayerRecord(
            text="t4", source="", ground_truth=None,
            layer="embedding_fast_path", predicted_category="kosten",
            predicted_confidence=0.9, embedding_latency_ms=1.0, llm_latency_ms=None,
            llm_provider=None, llm_model=None, input_tokens=None, output_tokens=None, total_tokens=None,
        ),
    ]
    metrics = compute_quality_metrics(records, valid)

    assert metrics["true_positives"] == 2
    assert metrics["false_positives"] == 1  # hallucination on the unlabeled record
    assert metrics["false_negatives"] == 1
    assert metrics["hallucination_rate"] == 1 / 3  # 1 hallucination / 3 predictions


# ---------------------------------------------------------------------------
# Eval-set loading
# ---------------------------------------------------------------------------


def test_load_eval_records_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "eval.jsonl"
    path.write_text(
        '{"text": "het is te duur", "label": "kosten", "source": "test"}\n'
        '{"text": "oke", "label": "review", "source": "test"}\n',
        encoding="utf-8",
    )
    records = load_eval_records(path)
    assert len(records) == 1
    assert records[0]["text"] == "het is te duur"
    assert records[0]["label"] == "kosten"


def test_load_eval_records_json_transcript(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        '{"events": [{"type": "transcript", "speaker": "prospect", "text": "het is te duur"}, '
        '{"type": "transcript", "speaker": "self", "text": "begrijpelijk"}]}',
        encoding="utf-8",
    )
    records = load_eval_records(path)
    assert len(records) == 1
    assert records[0]["text"] == "het is te duur"
    assert records[0]["label"] == "review"


# ---------------------------------------------------------------------------
# Model-matrix logic
# ---------------------------------------------------------------------------


def test_model_matrix_aggregates_costs_with_mocked_llm(
    monkeypatch: pytest.MonkeyPatch,
    config: DetectorConfig,
) -> None:
    """The matrix-aggregation and cost estimate must be deterministic."""
    records = [
        {"text": "het is te duur", "label": "kosten", "source": "test"},
        {"text": "we hebben geen budget", "label": "kosten", "source": "test"},
    ]

    def fake_call_model(self, fragment: str) -> PainPointDetection:
        # Force a middle-band match first, then LLM confirmation.
        self._llm._last_usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        return PainPointDetection(category="kosten", confidence=0.72, trigger_phrase=fragment)

    monkeypatch.setattr(
        "sales_copilot.modules.detector.llm_confirm.LLMConfirmClient._call_model",
        fake_call_model,
    )

    pricing = PricingTable(
        entries=[
            {"provider": "gemini", "model": "flash", "input_per_1m": 0.15, "output_per_1m": 0.60},
            {"provider": "ollama", "model": "*", "input_per_1m": 0.0, "output_per_1m": 0.0},
        ]
    )

    specs = [
        ModelSpec(provider="gemini", model="flash"),
        ModelSpec(provider="ollama", model="gemma3:4b"),
    ]
    reports: list[dict] = []
    for spec in specs:
        hooks = CascadeEvalHooks()
        pipeline, effective_spec, available, reason = build_pipeline_for_spec(
            config, _FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
            PainPointDebouncer(cooldown_seconds=0), spec, hooks
        )
        out = run_cascade_on_records(pipeline, records, config, effective_spec, hooks)
        reports.append(build_per_model_report(out, config, pricing, spec, available, reason))

    assert len(reports) == 2
    gemini_report = next(r for r in reports if r["provider"] == "gemini")
    ollama_report = next(r for r in reports if r["provider"] == "ollama")

    assert gemini_report["total_llm_input_tokens"] == 200
    assert gemini_report["total_llm_output_tokens"] == 100
    assert gemini_report["estimated_cost_usd"] is not None
    assert gemini_report["estimated_cost_usd"] > 0

    assert ollama_report["estimated_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# EVAL-ONLY resilience: retry-with-backoff + pacing wrap on the cascade track
#
# ``build_pipeline_for_spec`` wraps the ``LLMConfirmClient``'s underlying ``LLMClient``
# instance in place (see ``eval_shared.wrap_llm_client_for_eval``) -- it never modifies
# ``modules/detector/llm_confirm.py``, so the live copilot's confirm_async path is untouched.
# These tests exercise that wrap end-to-end through ``pipeline.process`` (the sync path the
# cascade eval harness drives), mocking ``time.sleep`` so no test actually waits.
# ---------------------------------------------------------------------------


def test_build_pipeline_for_spec_retries_on_rate_limit_and_recovers(
    monkeypatch: pytest.MonkeyPatch, config: DetectorConfig
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    hooks = CascadeEvalHooks()
    spec = ModelSpec(provider="openai", model="gpt-4o-mini")
    pipeline, effective_spec, available, reason = build_pipeline_for_spec(
        config,
        _FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
        PainPointDebouncer(cooldown_seconds=0),
        spec,
        hooks,
        max_retries=3,
        base_delay_s=0.0,
        pace_ms=0,
    )
    assert available is True

    attempts = {"n": 0}

    def flaky_sdk_call(**kwargs: object) -> PainPointDetection:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return PainPointDetection(category="kosten", confidence=0.8, trigger_phrase="te duur")

    pipeline.llm_client._llm._create = flaky_sdk_call

    event = pipeline.process("het is echt te duur voor ons", speaker="prospect")

    assert event is not None
    assert event.category == "kosten"
    assert attempts["n"] == 3  # 2 failures + 1 success
    assert len(sleeps) == 2  # one backoff sleep per retried failure, no pace (pace_ms=0)


def test_build_pipeline_for_spec_gives_up_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch, config: DetectorConfig
) -> None:
    """A rate-limit error that never clears still ends up as ``None`` -- unchanged failure
    mode, just after a few backed-off attempts instead of none."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    hooks = CascadeEvalHooks()
    spec = ModelSpec(provider="openai", model="gpt-4o-mini")
    pipeline, _, available, _ = build_pipeline_for_spec(
        config,
        _FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
        PainPointDebouncer(cooldown_seconds=0),
        spec,
        hooks,
        max_retries=2,
        base_delay_s=0.0,
        pace_ms=0,
    )
    assert available is True

    attempts = {"n": 0}

    def always_rate_limited(**kwargs: object) -> None:
        attempts["n"] += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    pipeline.llm_client._llm._create = always_rate_limited

    event = pipeline.process("het is echt te duur voor ons", speaker="prospect")

    assert event is None  # LLMConfirmClient.confirm() still swallows the (now-exhausted) error
    assert attempts["n"] == 3  # initial attempt + 2 retries


def test_build_pipeline_for_spec_paces_before_each_llm_call(
    monkeypatch: pytest.MonkeyPatch, config: DetectorConfig
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    hooks = CascadeEvalHooks()
    spec = ModelSpec(provider="openai", model="gpt-4o-mini")
    pipeline, _, available, _ = build_pipeline_for_spec(
        config,
        _FakeRouter(RouteMatch("kosten", 0.65, "uncertain", "embedding")),
        PainPointDebouncer(cooldown_seconds=0),
        spec,
        hooks,
        pace_ms=250,
    )
    assert available is True

    pipeline.llm_client._llm._create = lambda **kwargs: PainPointDetection(
        category="kosten", confidence=0.8, trigger_phrase="te duur"
    )

    pipeline.process("het is echt te duur voor ons", speaker="prospect")

    assert sleeps == [0.25]


def test_pricing_table_lookup() -> None:
    pricing = PricingTable(
        entries=[
            {"provider": "gemini", "model": "flash", "input_per_1m": 0.1, "output_per_1m": 0.2},
            {"provider": "ollama", "model": "*", "input_per_1m": 0.0, "output_per_1m": 0.0},
        ]
    )
    assert pricing.lookup("gemini", "flash") == {"input_per_1m": 0.1, "output_per_1m": 0.2}
    assert pricing.lookup("ollama", "gemma3:4b") == {"input_per_1m": 0.0, "output_per_1m": 0.0}
    assert pricing.lookup("openai", "gpt-4") is None
