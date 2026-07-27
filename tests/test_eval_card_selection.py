from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sales_copilot.modules.detector.card_selection_eval import (
    CardSelectionResult,
    build_card_selection_response_model,
    build_per_model_report,
    load_cases,
    load_eval_records,
    run_embedding_baseline,
    run_llm_selector,
)
from sales_copilot.modules.detector.cascade_eval import ModelSpec, PricingTable
from sales_copilot.modules.detector.router import RouteMatch
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB


@dataclass
class _FakeRouter:
    """Deterministic router for unit tests (no embedding model load)."""

    category: str | None
    confidence: float = 0.9

    def classify(self, text: str) -> RouteMatch | None:
        if self.category is None:
            return None
        return RouteMatch(category=self.category, confidence=self.confidence, tier="high", source="keyword")


class _StubLLMClient:
    """Stub LLMClient that returns a fixed card_id without real provider calls."""

    def __init__(self, card_id: str) -> None:
        self.card_id = card_id
        self.calls: list[dict[str, Any]] = []
        self.provider = "gemini"

    def create(self, *, response_model: type[Any], **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return response_model(card_id=self.card_id, confidence=0.9, reason="stub")

    @property
    def last_usage(self) -> dict[str, int] | None:
        return {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


@pytest.fixture
async def seeded_case_db(tmp_path: Path) -> SQLiteCaseDB:
    db = SQLiteCaseDB(tmp_path / "cases.db")
    await db.initialize()
    cases = load_cases(Path(__file__).parent / "fixtures" / "card_selection_eval_cases.json")
    await db.upsert_cases(cases)
    return db


def test_load_eval_records(tmp_path: Path) -> None:
    path = tmp_path / "eval.jsonl"
    path.write_text(
        '{"text": "t1", "expected_case_id": "case-001"}\n'
        '{"text": "t2", "expected_case_id": null}\n',
        encoding="utf-8",
    )
    records = load_eval_records(path)
    assert len(records) == 2
    assert records[0]["expected_case_id"] == "case-001"
    assert records[1]["expected_case_id"] is None


def test_constrained_enum_rejects_invalid_card_id() -> None:
    """The dynamic response model must only accept IDs from the valid set."""
    model = build_card_selection_response_model(["case-001", "case-002"])

    valid = model(card_id="case-001", confidence=0.8)
    assert valid.card_id.value == "case-001"

    with pytest.raises(ValidationError):
        model(card_id="case-999", confidence=0.8)


def test_selection_accuracy_and_invalid_id_rate() -> None:
    """build_per_model_report must compute accuracy and invalid-id rate correctly."""
    pricing = PricingTable(
        entries=[{"provider": "gemini", "model": "flash", "input_per_1m": 0.15, "output_per_1m": 0.60}]
    )
    results = [
        CardSelectionResult(
            text="t1",
            source="test",
            pain_point="offerteproces",
            industry=None,
            expected_case_id="case-001",
            predicted_case_id="case-001",
            selector="llm:gemini:flash",
            embedding_latency_ms=1.0,
            llm_latency_ms=10.0,
            latency_ms=11.0,
            llm_provider="gemini",
            llm_model="flash",
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            valid_card_ids=["case-001", "case-002"],
        ),
        CardSelectionResult(
            text="t2",
            source="test",
            pain_point="offerteproces",
            industry=None,
            expected_case_id="case-002",
            predicted_case_id="case-001",
            selector="llm:gemini:flash",
            embedding_latency_ms=1.0,
            llm_latency_ms=10.0,
            latency_ms=11.0,
            llm_provider="gemini",
            llm_model="flash",
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            valid_card_ids=["case-001", "case-002"],
        ),
        CardSelectionResult(
            text="t3",
            source="test",
            pain_point=None,
            industry=None,
            expected_case_id=None,
            predicted_case_id=None,
            selector="llm:gemini:flash",
            embedding_latency_ms=1.0,
            llm_latency_ms=None,
            latency_ms=1.0,
            llm_provider="gemini",
            llm_model="flash",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            valid_card_ids=[],
        ),
        CardSelectionResult(
            text="t4",
            source="test",
            pain_point="handmatig_werk",
            industry=None,
            expected_case_id="case-005",
            predicted_case_id="case-999",
            selector="llm:gemini:flash",
            embedding_latency_ms=1.0,
            llm_latency_ms=10.0,
            latency_ms=11.0,
            llm_provider="gemini",
            llm_model="flash",
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            valid_card_ids=["case-005"],
            error="invalid id",
        ),
    ]

    report = build_per_model_report(
        results, pricing, ModelSpec(provider="gemini", model="flash"), available=True, reason="stub"
    )

    assert report["total"] == 4
    assert report["correct"] == 2
    assert report["accuracy"] == 0.5
    assert report["invalid_id_count"] == 1
    # Three predictions (two case-001, one invalid), so invalid rate = 1/3.
    assert report["invalid_id_rate"] == pytest.approx(1 / 3)
    assert report["input_tokens"] == 300
    assert report["output_tokens"] == 60
    assert report["estimated_cost_usd"] is not None
    assert report["estimated_cost_usd"] > 0


@pytest.mark.asyncio
async def test_embedding_baseline_uses_case_db_priority(tmp_path: Path) -> None:
    """The baseline must map a detected pain point to the highest-priority case."""
    db = SQLiteCaseDB(tmp_path / "cases.db")
    await db.initialize()
    await db.upsert_cases(
        [
            Case(
                id="case-a",
                title="A",
                industry=None,
                pain_point="offerteproces",
                description="high priority",
                slide_html="<section></section>",
                metrics=None,
                priority=10,
            ),
            Case(
                id="case-b",
                title="B",
                industry=None,
                pain_point="offerteproces",
                description="low priority",
                slide_html="<section></section>",
                metrics=None,
                priority=5,
            ),
        ]
    )

    records = [{"text": "offerteproces", "expected_case_id": "case-a", "source": "test"}]
    router = _FakeRouter("offerteproces")
    results = await run_embedding_baseline(records, router, db)

    assert len(results) == 1
    assert results[0].predicted_case_id == "case-a"
    assert results[0].selector == "embedding_router"


@pytest.mark.asyncio
async def test_matrix_aggregation_deterministic_with_mocked_llm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The full selector run + aggregation must be deterministic when the LLM is stubbed."""
    db = SQLiteCaseDB(tmp_path / "cases.db")
    await db.initialize()
    await db.upsert_cases(
        [
            Case(
                id="case-001",
                title="A",
                industry=None,
                pain_point="offerteproces",
                description="d1",
                slide_html="<section></section>",
                metrics=None,
                priority=10,
            ),
            Case(
                id="case-002",
                title="B",
                industry=None,
                pain_point="offerteproces",
                description="d2",
                slide_html="<section></section>",
                metrics=None,
                priority=5,
            ),
        ]
    )

    records = [
        {"text": "offerteproces variant a", "expected_case_id": "case-001", "source": "test"},
        {"text": "offerteproces variant b", "expected_case_id": "case-002", "source": "test"},
    ]
    router = _FakeRouter("offerteproces")
    client = _StubLLMClient("case-001")

    results, available, reason = await run_llm_selector(
        records,
        router,
        db,
        ModelSpec(provider="gemini", model="flash"),
        client=client,
    )

    assert available
    assert len(results) == 2
    assert all(r.predicted_case_id == "case-001" for r in results)
    assert all(r.error is None for r in results)
    assert all(r.predicted_case_id in r.valid_card_ids for r in results)

    pricing = PricingTable(
        entries=[{"provider": "gemini", "model": "flash", "input_per_1m": 0.15, "output_per_1m": 0.60}]
    )
    report = build_per_model_report(
        results, pricing, ModelSpec(provider="gemini", model="flash"), available=True, reason="stub"
    )

    assert report["total"] == 2
    assert report["correct"] == 1
    assert report["accuracy"] == 0.5
    assert report["invalid_id_count"] == 0
    assert report["invalid_id_rate"] == 0.0
    assert report["input_tokens"] == 200
    assert report["output_tokens"] == 40
    assert report["estimated_cost_usd"] == pytest.approx(0.000054, abs=1e-9)


def test_baseline_report_has_zero_cost() -> None:
    """The embedding-router baseline must report zero cost, not n/a."""
    pricing = PricingTable(entries=[])
    results = [
        CardSelectionResult(
            text="t1",
            source="test",
            pain_point="offerteproces",
            industry=None,
            expected_case_id="case-001",
            predicted_case_id="case-001",
            selector="embedding_router",
            embedding_latency_ms=1.0,
            llm_latency_ms=None,
            latency_ms=1.0,
            llm_provider=None,
            llm_model=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            valid_card_ids=["case-001"],
        ),
    ]
    report = build_per_model_report(
        results,
        pricing,
        ModelSpec(provider="embedding_router", model="semantic_router"),
        available=True,
        reason="local",
    )
    assert report["estimated_cost_usd"] == 0.0
