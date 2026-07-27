from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.thinking_policy import ThinkingPolicy
from sales_copilot.modules.detector.eval_shared import ModelSpec, PricingTable
from sales_copilot.modules.detector.summarization_eval import (
    OUTPUT_TOKENS_FLOOR,
    ActionItem,
    ActionItems,
    SummaryEvalRecord,
    _summary_max_tokens,
    build_per_model_report,
    compute_action_item_metrics,
    extract_action_items,
    format_markdown_report,
    load_reference_set,
    load_transcript_records,
    run_summarization_eval,
)

# ---------------------------------------------------------------------------
# Metrics (deterministic)
# ---------------------------------------------------------------------------


def test_compute_action_item_metrics_perfect_match() -> None:
    predicted = [{"description": "Stuur de offerte voor vrijdag"}]
    reference = [{"description": "Stuur de offerte voor vrijdag"}]
    metrics = compute_action_item_metrics(predicted, reference)

    assert metrics["precision"] == pytest.approx(1.0)
    assert metrics["recall"] == pytest.approx(1.0)
    assert metrics["hallucination_rate"] == pytest.approx(0.0)
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 0
    assert metrics["false_negatives"] == 0


def test_compute_action_item_metrics_fuzzy_match() -> None:
    predicted = [{"description": "offerte sturen"}]
    reference = [{"description": "Stuur de offerte voor vrijdag"}]
    metrics = compute_action_item_metrics(predicted, reference)

    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 0
    assert metrics["false_negatives"] == 0


def test_compute_action_item_metrics_hallucination() -> None:
    predicted = [
        {"description": "Stuur de offerte"},
        {"description": "Bel volgende week terug"},
    ]
    reference = [{"description": "Stuur de offerte"}]
    metrics = compute_action_item_metrics(predicted, reference)

    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 1
    assert metrics["false_negatives"] == 0
    assert metrics["hallucination_rate"] == pytest.approx(0.5)


def test_compute_action_item_metrics_missing_prediction() -> None:
    predicted = None
    reference = [{"description": "Stuur de offerte"}]
    metrics = compute_action_item_metrics(predicted, reference)

    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
    assert metrics["false_positives"] == 0
    assert metrics["false_negatives"] == 1


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------


def test_load_transcript_records_from_directory(tmp_path: Path) -> None:
    call_dir = tmp_path / "call-001"
    call_dir.mkdir()
    transcript = call_dir / "transcript_ours.txt"
    transcript.write_text("Sales: Goedemorgen.\nProspect: We willen een demo.\n", encoding="utf-8")
    gt = call_dir / "action_items.json"
    gt.write_text('[{"description": "Plan een demo"}]', encoding="utf-8")

    records = load_transcript_records(tmp_path)

    assert len(records) == 1
    assert records[0]["source"] == "call-001"
    assert "Plan een demo" in [r["description"] for r in records[0]["reference"]]


def test_load_transcript_records_from_single_file(tmp_path: Path) -> None:
    transcript = tmp_path / "call.txt"
    transcript.write_text("Prospect: Stuur een offerte.\n", encoding="utf-8")

    records = load_transcript_records(transcript)

    assert len(records) == 1
    assert records[0]["source"] == "call"
    assert records[0]["reference"] == []


def test_load_reference_set(tmp_path: Path) -> None:
    ref_path = tmp_path / "ground_truth.json"
    ref_path.write_text(
        '{"call-001": [{"description": "Plan een demo"}], "call-002": []}',
        encoding="utf-8",
    )
    data = load_reference_set(ref_path)

    assert data["call-001"] == [{"description": "Plan een demo"}]
    assert data["call-002"] == []


# ---------------------------------------------------------------------------
# Schema compliance + extraction
# ---------------------------------------------------------------------------


def test_extract_action_items_returns_schema_compliant_result() -> None:
    expected = ActionItems(
        items=[
            ActionItem(
                description="Stuur offerte",
                owner="sales",
                priority="high",
                context_quote="kunt u een offerte sturen",
            )
        ]
    )
    fake_create = MagicMock(return_value=expected)
    fake_client = MagicMock()
    fake_client.create = fake_create
    fake_client.last_usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }

    config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
    spec = ModelSpec(provider="gemini", model="gemini-2.5-flash")

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        result, meta = extract_action_items(["Prospect: kunt u een offerte sturen?"], config, spec)

    assert result is not None
    assert len(result.items) == 1
    assert meta["schema_compliant"] is True
    assert meta["error"] is None
    assert meta["usage"]["input_tokens"] == 100


def test_extract_action_items_marks_failure_as_non_compliant() -> None:
    fake_client = MagicMock()
    fake_client.create = MagicMock(side_effect=RuntimeError("ollama timeout"))

    config = DetectorConfig(llm_provider="ollama", llm_model="gemma3:4b")
    spec = ModelSpec(provider="ollama", model="gemma3:4b")

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        result, meta = extract_action_items(["Prospect: test"], config, spec)

    assert result is None
    assert meta["schema_compliant"] is False
    assert "ollama timeout" in meta["error"]


# ---------------------------------------------------------------------------
# Thinking-aware output-token budget (dispatch D-6ad4091a item 3: avoid
# finish_reason='length' truncating a thinking model's reasoning trace)
# ---------------------------------------------------------------------------


def test_summary_max_tokens_floor_without_thinking_policy() -> None:
    assert _summary_max_tokens(None) == OUTPUT_TOKENS_FLOOR


def test_summary_max_tokens_scales_with_large_reasoning_budget() -> None:
    policy = ThinkingPolicy(name="think-8192", thinking_on=True, reasoning_budget=8192)
    assert _summary_max_tokens(policy) == 8192 + 1024


def test_summary_max_tokens_floor_for_small_reasoning_budget() -> None:
    """A small budget still gets the generous floor, not a value below it."""
    policy = ThinkingPolicy(name="think-512", thinking_on=True, reasoning_budget=512)
    assert _summary_max_tokens(policy) == OUTPUT_TOKENS_FLOOR


def test_summary_max_tokens_floor_when_thinking_off() -> None:
    policy = ThinkingPolicy(name="no-think", thinking_on=False, reasoning_budget=None)
    assert _summary_max_tokens(policy) == OUTPUT_TOKENS_FLOOR


def test_extract_action_items_passes_thinking_aware_max_tokens() -> None:
    fake_create = MagicMock(return_value=ActionItems(items=[]))
    fake_client = MagicMock()
    fake_client.create = fake_create
    fake_client.last_usage = {}

    config = DetectorConfig(llm_provider="openrouter", llm_model="qwen/qwen3.6-35b-a3b")
    spec = ModelSpec(provider="openrouter", model="qwen/qwen3.6-35b-a3b")
    policy = ThinkingPolicy(name="think-8192", thinking_on=True, reasoning_budget=8192)

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        extract_action_items(["Prospect: test"], config, spec, thinking=policy, pace_ms=0)

    assert fake_create.call_args.kwargs["max_tokens"] == 8192 + 1024


# ---------------------------------------------------------------------------
# EVAL-ONLY retry-with-backoff + pacing (dispatch D-6ad4091a items 2/4)
# ---------------------------------------------------------------------------


def test_extract_action_items_retries_on_rate_limit_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    expected = ActionItems(items=[ActionItem(description="Stuur offerte")])
    attempts = {"n": 0}

    def flaky_create(**kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return expected

    fake_client = MagicMock()
    fake_client.create = MagicMock(side_effect=flaky_create)
    fake_client.last_usage = {}

    config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
    spec = ModelSpec(provider="gemini", model="gemini-2.5-flash")

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        result, meta = extract_action_items(
            ["Prospect: test"], config, spec, max_retries=3, base_delay_s=0.0, pace_ms=0
        )

    assert result is expected
    assert meta["error"] is None
    assert attempts["n"] == 3
    assert len(sleeps) == 2


def test_extract_action_items_paces_before_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    fake_client = MagicMock()
    fake_client.create = MagicMock(return_value=ActionItems(items=[]))
    fake_client.last_usage = {}

    config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
    spec = ModelSpec(provider="gemini", model="gemini-2.5-flash")

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        extract_action_items(["Prospect: test"], config, spec, pace_ms=250)

    assert sleeps == [0.25]


# ---------------------------------------------------------------------------
# End-to-end matrix aggregation (mocked provider response)
# ---------------------------------------------------------------------------


def test_matrix_aggregation_is_deterministic_with_mocked_response() -> None:
    predicted = ActionItems(
        items=[
            ActionItem(description="Stuur offerte", owner="sales", priority="high"),
            ActionItem(description="Bel volgende week", owner="sales", priority="medium"),
        ]
    )
    fake_create = MagicMock(return_value=predicted)
    fake_client = MagicMock()
    fake_client.create = fake_create
    fake_client.last_usage = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "total_tokens": 1200,
    }

    config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")

    records = [
        {
            "source": "call-001",
            "transcript": "t1",
            "transcript_lines": ["Prospect: kunt u een offerte sturen?"],
            "reference": [
                {"description": "Stuur offerte"},
                {"description": "Bel volgende week"},
            ],
        },
    ]

    pricing = PricingTable(
        entries=[
            {"provider": "gemini", "model": "gemini-2.5-flash", "input_per_1m": 0.15, "output_per_1m": 0.60},
        ]
    )

    with patch("sales_copilot.modules.detector.summarization_eval.LLMClient") as mock_cls:
        mock_cls.return_value = fake_client
        out, available, reason = run_summarization_eval(records, config, ModelSpec("gemini", "gemini-2.5-flash"))

    assert available is True
    report = build_per_model_report(out, pricing, ModelSpec("gemini", "gemini-2.5-flash"), available, reason)

    assert report["total"] == 1
    assert report["schema_compliant_count"] == 1
    assert report["schema_compliance_rate"] == 1.0
    assert report["true_positives"] == 2
    assert report["false_positives"] == 0
    assert report["false_negatives"] == 0
    assert report["precision"] == pytest.approx(1.0)
    assert report["recall"] == pytest.approx(1.0)
    assert report["total_input_tokens"] == 1000
    assert report["total_output_tokens"] == 200
    assert report["estimated_cost_usd"] is not None
    assert report["estimated_cost_usd"] > 0


def test_matrix_handles_unavailable_provider_without_key() -> None:
    config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
    records = [
        {
            "source": "call-001",
            "transcript": "t1",
            "transcript_lines": ["Prospect: test"],
            "reference": [{"description": "Stuur offerte"}],
        },
    ]

    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
        out, available, reason = run_summarization_eval(records, config, ModelSpec("gemini", "gemini-2.5-flash"))

    assert available is False
    assert "GEMINI_API_KEY" in reason
    assert all(rec.error is not None for rec in out)
    assert all(not rec.schema_compliant for rec in out)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_markdown_report_contains_matrix_and_verdict() -> None:
    reports = [
        {
            "model": "gemini:gemini-2.5-flash",
            "provider": "gemini",
            "model_name": "gemini-2.5-flash",
            "available": True,
            "availability_reason": "GEMINI_API_KEY present",
            "total": 2,
            "schema_compliant_count": 2,
            "schema_compliance_rate": 1.0,
            "precision": 0.8,
            "recall": 0.75,
            "hallucination_rate": 0.1,
            "true_positives": 4,
            "false_positives": 1,
            "false_negatives": 2,
            "latency_ms": {"n": 2, "p50": 123.0, "p95": 456.0, "mean": 289.5},
            "total_input_tokens": 2000,
            "total_output_tokens": 400,
            "estimated_cost_usd": 0.00054,
        },
        {
            "model": "ollama:gemma3:4b",
            "provider": "ollama",
            "model_name": "gemma3:4b",
            "available": True,
            "availability_reason": "Ollama reachable",
            "total": 2,
            "schema_compliant_count": 1,
            "schema_compliance_rate": 0.5,
            "precision": 0.5,
            "recall": 0.5,
            "hallucination_rate": 0.25,
            "true_positives": 2,
            "false_positives": 2,
            "false_negatives": 2,
            "latency_ms": {"n": 2, "p50": 2000.0, "p95": 3000.0, "mean": 2500.0},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
    ]
    md = format_markdown_report(reports, Path("data/fireflies"))

    assert "Model-matrix (shootout)" in md
    assert "gemini:gemini-2.5-flash" in md
    assert "ollama:gemma3:4b" in md
    assert "Tier-verdict" in md
    assert "Beste model voor summarization-laag" in md
    assert "123/456 ms" in md
    assert "$0.000540" in md


def test_format_markdown_report_notes_missing_provider() -> None:
    reports = [
        {
            "model": "gemini:gemini-2.5-flash",
            "provider": "gemini",
            "model_name": "gemini-2.5-flash",
            "available": False,
            "availability_reason": "GEMINI_API_KEY is empty",
            "total": 1,
            "schema_compliant_count": 0,
            "schema_compliance_rate": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "hallucination_rate": 0.0,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": 1,
            "latency_ms": {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0},
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "estimated_cost_usd": None,
        },
    ]
    md = format_markdown_report(reports, Path("data/fireflies"))

    assert "niet beschikbaar" in md
    assert "Geen enkel model was beschikbaar" in md


# ---------------------------------------------------------------------------
# NDJSON serialization
# ---------------------------------------------------------------------------


def test_summary_eval_record_serializes_to_dict() -> None:
    record = SummaryEvalRecord(
        source="call-001",
        transcript="Prospect: test",
        reference=[{"description": "Stuur offerte"}],
        predicted=[{"description": "Stuur offerte"}],
        latency_ms=150.0,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        schema_compliant=True,
        error=None,
        llm_provider="gemini",
        llm_model="gemini-2.5-flash",
    )
    data = record.to_dict()

    assert data["source"] == "call-001"
    assert data["schema_compliant"] is True
    assert data["predicted"][0]["description"] == "Stuur offerte"
