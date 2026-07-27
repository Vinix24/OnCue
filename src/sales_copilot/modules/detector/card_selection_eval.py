"""Card/slide selection evaluation harness (Track 3).

Compares two selectors that decide which case-slide to inject for a detected
pain point:

1. ``EmbeddingCardSelector`` — the existing semantic-router baseline: classify
   the utterance to a pain-point category, then pick the highest-priority case
   for that category from the case DB.
2. ``LLMCardSelector`` — a constrained LLM selector that receives the candidate
   cases for the detected category and must return one of their ``card_id``
   values via an instructor-structured enum.

The module reuses the shared eval infrastructure from
``sales_copilot.modules.detector.eval_shared`` for model specs, pricing,
provider availability, latency aggregation, NDJSON logging and report formatting.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError, create_model

from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.modules.detector.eval_shared import (
    ModelSpec,
    PricingTable,
    is_provider_available,
)
from sales_copilot.modules.detector.eval_shared import (
    latency_stats as _latency_stats,
)
from sales_copilot.modules.detector.router import PainPointRouter
from sales_copilot.modules.slides.case_db import Case, CaseDB

logger = logging.getLogger(__name__)

DEFAULT_PRICES_PATH = resolve_app_path("config/eval_model_prices.yaml")
DEFAULT_CASES_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "card_selection_eval_cases.json"
DEFAULT_EVAL_SET_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "card_selection_eval.jsonl"

_SYSTEM_PROMPT = """Je bent een sales-copilot die bepaalt welke case-slide het beste past bij een gedetecteerd pijnpunt.

Je krijgt een gespreksfragment en een lijst met candidate cases. Kies exact één case ID uit de lijst.
Gebruik de titel, industrie en beschrijving om de meest relevante case te selecteren.
Geef alleen de JSON-velden terug die gevraagd worden."""


@dataclass
class CardSelectionResult:
    """One eval example after running through a card selector."""

    text: str
    source: str
    pain_point: str | None
    industry: str | None
    expected_case_id: str | None
    predicted_case_id: str | None
    selector: str
    embedding_latency_ms: float
    llm_latency_ms: float | None
    latency_ms: float
    llm_provider: str | None
    llm_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    valid_card_ids: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Eval-set / case loading
# ---------------------------------------------------------------------------


def load_eval_records(path: Path) -> list[dict[str, Any]]:
    """Load JSONL records for card-selection eval.

    Expected fields per record:
        text: str
        source: str (optional)
        pain_point: str | None (ground-truth category)
        industry: str | None
        expected_case_id: str | None
        notes: str (optional)
    """
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def load_cases(path: Path) -> list[Case]:
    """Load case definitions from a JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Case fixture must contain a JSON list: {path}")
    return [Case(**item) for item in raw]


# ---------------------------------------------------------------------------
# Constrained LLM response model
# ---------------------------------------------------------------------------


def build_card_selection_response_model(card_ids: list[str]) -> type[BaseModel]:
    """Return a Pydantic model whose ``card_id`` field is constrained to ``card_ids``.

    The field is modelled as a dynamic ``Enum`` so instructor can emit a JSON-schema
    enum that the LLM must honour. Instantiating the model with an ID outside the
    set raises a ``ValidationError``.
    """
    if not card_ids:
        raise ValueError("Cannot build a constrained card_id model without card IDs")

    enum_members = {cid.replace("-", "_"): cid for cid in card_ids}
    card_id_enum = Enum("CardIdEnum", enum_members, type=str)  # type: ignore[misc]

    return create_model(
        "CardSelection",
        card_id=(card_id_enum, ...),
        confidence=(float, 0.0),
        reason=(str, ""),
    )


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


class EmbeddingCardSelector:
    """Baseline selector: semantic router + highest-priority case lookup."""

    def __init__(self, router: PainPointRouter, case_db: CaseDB) -> None:
        self.router = router
        self.case_db = case_db

    async def select(self, record: dict[str, Any]) -> CardSelectionResult:
        text = str(record.get("text", ""))
        industry = record.get("industry") or None
        source = str(record.get("source", ""))
        expected = record.get("expected_case_id") or None

        start = time.perf_counter()
        match = self.router.classify(text)
        embedding_latency_ms = (time.perf_counter() - start) * 1000

        predicted_case_id: str | None = None
        valid_card_ids: list[str] = []
        pain_point = match.category if match else None

        if match is not None:
            case = await self.case_db.find_case(match.category, industry)
            if case is not None:
                predicted_case_id = case.id
                valid_card_ids = [case.id]

        return CardSelectionResult(
            text=text,
            source=source,
            pain_point=pain_point,
            industry=industry,
            expected_case_id=expected,
            predicted_case_id=predicted_case_id,
            selector="embedding_router",
            embedding_latency_ms=embedding_latency_ms,
            llm_latency_ms=None,
            latency_ms=embedding_latency_ms,
            llm_provider=None,
            llm_model=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            valid_card_ids=valid_card_ids,
            error=None,
        )


class LLMCardSelector:
    """LLM selector with a constrained enum of valid card IDs."""

    def __init__(
        self,
        router: PainPointRouter,
        case_db: CaseDB,
        client: LLMClient,
        model: str,
        provider: str,
    ) -> None:
        self.router = router
        self.case_db = case_db
        self.client = client
        self.model = model
        self.provider = provider

    async def select(self, record: dict[str, Any]) -> CardSelectionResult:
        text = str(record.get("text", ""))
        industry = record.get("industry") or None
        source = str(record.get("source", ""))
        expected = record.get("expected_case_id") or None

        start = time.perf_counter()
        match = self.router.classify(text)
        embedding_latency_ms = (time.perf_counter() - start) * 1000

        predicted_case_id: str | None = None
        valid_card_ids: list[str] = []
        llm_latency_ms: float | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None
        error: str | None = None
        pain_point = match.category if match else None

        if match is None:
            latency_ms = embedding_latency_ms
        else:
            all_cases = await self.case_db.list_cases()
            candidates = [c for c in all_cases if c.pain_point == match.category]
            valid_card_ids = [c.id for c in candidates]

            if not candidates:
                latency_ms = embedding_latency_ms
            else:
                try:
                    response_model = build_card_selection_response_model(valid_card_ids)
                    user_prompt = self._build_prompt(text, industry, candidates)
                    llm_start = time.perf_counter()
                    response = self.client.create(
                        model=self.model,
                        system_prompt=_SYSTEM_PROMPT,
                        user_text=user_prompt,
                        response_model=response_model,
                        temperature=0.0,
                    )
                    llm_latency_ms = (time.perf_counter() - llm_start) * 1000
                    latency_ms = embedding_latency_ms + llm_latency_ms

                    if response is None:
                        error = "LLM returned no response"
                    else:
                        raw_card_id = (
                            response.card_id.value
                            if isinstance(response.card_id, Enum)
                            else str(response.card_id)
                        )
                        if raw_card_id not in valid_card_ids:
                            error = f"Invalid card_id {raw_card_id!r} (not in {valid_card_ids})"
                        else:
                            predicted_case_id = raw_card_id
                        usage = self.client.last_usage
                        if usage:
                            input_tokens = usage.get("input_tokens")
                            output_tokens = usage.get("output_tokens")
                            total_tokens = usage.get("total_tokens")
                except ValidationError as exc:
                    latency_ms = embedding_latency_ms
                    error = f"LLM returned invalid structured output: {exc}"
                except Exception as exc:  # noqa: BLE001
                    latency_ms = embedding_latency_ms
                    error = f"LLM call failed: {exc}"

        return CardSelectionResult(
            text=text,
            source=source,
            pain_point=pain_point,
            industry=industry,
            expected_case_id=expected,
            predicted_case_id=predicted_case_id,
            selector=f"llm:{self.provider}:{self.model}",
            embedding_latency_ms=embedding_latency_ms,
            llm_latency_ms=llm_latency_ms,
            latency_ms=latency_ms,
            llm_provider=self.provider,
            llm_model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            valid_card_ids=valid_card_ids,
            error=error,
        )

    @staticmethod
    def _build_prompt(text: str, industry: str | None, candidates: list[Case]) -> str:
        lines = [
            f"Gespreksfragment: {text}",
            f"Industrie prospect: {industry or 'onbekend'}",
            "",
            "Beschikbare cases (kies erexact één):",
        ]
        for case in candidates:
            lines.append(
                f"- {case.id}: {case.title} | industrie={case.industry or 'onbekend'} | "
                f"beschrijving={case.description or 'geen'}"
            )
        lines.append("")
        lines.append("Welke case_id past het beste?")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Running the eval
# ---------------------------------------------------------------------------


async def run_embedding_baseline(
    records: list[dict[str, Any]],
    router: PainPointRouter,
    case_db: CaseDB,
) -> list[CardSelectionResult]:
    """Run the embedding-router + case-DB baseline over ``records``."""
    selector = EmbeddingCardSelector(router, case_db)
    return [await selector.select(rec) for rec in records]


async def run_llm_selector(
    records: list[dict[str, Any]],
    router: PainPointRouter,
    case_db: CaseDB,
    spec: ModelSpec,
    *,
    client: LLMClient | None = None,
    timeout_ms: int = 7000,
) -> tuple[list[CardSelectionResult], bool, str]:
    """Run the constrained LLM selector for ``spec``.

    Returns ``(results, available, reason)``. If the provider is unavailable or
    the client cannot be constructed, ``results`` is empty and ``available`` is
    ``False`` so the matrix can still report the row.

    An optional pre-built ``client`` can be passed for testing/mocking.
    """
    available, reason = is_provider_available(spec.provider)
    if not available:
        return [], False, reason

    if client is None:
        try:
            client = LLMClient(spec.provider, timeout_ms=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return [], False, f"LLM client construction failed: {exc}"

    selector = LLMCardSelector(
        router=router,
        case_db=case_db,
        client=client,
        model=spec.model,
        provider=spec.provider,
    )
    return [await selector.select(rec) for rec in records], True, reason


# ---------------------------------------------------------------------------
# Metrics and aggregation
# ---------------------------------------------------------------------------


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def build_per_model_report(
    results: list[CardSelectionResult],
    pricing: PricingTable,
    spec: ModelSpec,
    available: bool,
    reason: str,
) -> dict[str, Any]:
    """Aggregate card-selection results into a report dict."""
    total = len(results)
    correct = sum(
        1 for r in results if r.predicted_case_id == r.expected_case_id
    )
    accuracy = _safe_div(correct, total) if total else None

    predictions = [r.predicted_case_id for r in results if r.predicted_case_id is not None]
    invalid = sum(
        1
        for r in results
        if r.predicted_case_id is not None and r.predicted_case_id not in r.valid_card_ids
    )
    invalid_id_rate = _safe_div(invalid, len(predictions)) if predictions else 0.0

    latencies = [r.latency_ms for r in results if r.error is None]
    latency_stats = _latency_stats(latencies)

    input_tokens = sum(r.input_tokens or 0 for r in results)
    output_tokens = sum(r.output_tokens or 0 for r in results)
    total_tokens = input_tokens + output_tokens

    if spec.provider == "embedding_router":
        cost = 0.0
    else:
        cost = pricing.estimate_cost(spec.provider, spec.model, input_tokens, output_tokens)

    return {
        "model": str(spec),
        "provider": spec.provider,
        "model_name": spec.model,
        "available": available,
        "availability_reason": reason,
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "invalid_id_count": invalid,
        "invalid_id_rate": invalid_id_rate,
        "latency_ms": latency_stats,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_latency(stats: dict[str, float]) -> str:
    if not stats.get("n"):
        return "—"
    return f"p50 {stats['p50']:.1f} ms / p95 {stats['p95']:.1f} ms / mean {stats['mean']:.1f} ms"


def format_markdown_report(
    reports: list[dict[str, Any]],
    eval_set_path: Path,
    cases_path: Path,
) -> str:
    """Render a human-readable markdown summary with a side-by-side matrix."""
    lines: list[str] = [
        "# Kaart-/slide-selectie evaluatie-rapport",
        "",
        f"**Eval-set:** {eval_set_path}",
        f"**Cases:** {cases_path}",
        "",
    ]

    for report in reports:
        lines.append(f"## Selector: {report['model']}")
        lines.append("")
        avail = "✅ beschikbaar" if report["available"] else f"⚠️  niet beschikbaar ({report['availability_reason']})"
        lines.append(f"**Beschikbaarheid:** {avail}")
        lines.append(f"**Totaal geëvalueerd:** {report['total']} records")
        if report["accuracy"] is None:
            lines.append("**Accuracy:** n/a")
        else:
            lines.append(f"**Accuracy:** {report['accuracy']:.2%} ({report['correct']}/{report['total']})")
        lines.append(
            f"**Invalid ID rate:** {report['invalid_id_rate']:.2%} "
            f"({report['invalid_id_count']} voorspellingen)"
        )
        lines.append(f"**Latency:** {_format_latency(report['latency_ms'])}")
        if report["total_tokens"]:
            lines.append(
                f"**LLM tokens:** input={report['input_tokens']}, "
                f"output={report['output_tokens']}, total={report['total_tokens']}"
            )
            if report["estimated_cost_usd"] is not None:
                lines.append(f"**Geschatte kosten:** ${report['estimated_cost_usd']:.6f}")
        lines.append("")

    # Side-by-side model matrix.
    if len(reports) > 1:
        lines.append("## Model-matrix (shootout)")
        lines.append("")
        lines.append(
            "| Model | Beschikbaar | Accuracy | Invalid ID | Latency p50/p95 | "
            "Tokens | Kosten ($) |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for report in reports:
            lat = report["latency_ms"]
            avail = "ja" if report["available"] else "nee"
            acc = f"{report['accuracy']:.2%}" if report["accuracy"] is not None else "n/a"
            inv = f"{report['invalid_id_rate']:.2%}"
            lat_str = f"{lat['p50']:.0f}/{lat['p95']:.0f} ms" if lat.get("n") else "n/a"
            tokens = str(report["total_tokens"]) if report["total_tokens"] else "0"
            cost = f"${report['estimated_cost_usd']:.6f}" if report["estimated_cost_usd"] is not None else "n/a"
            lines.append(
                f"| {report['model']} | {avail} | {acc} | {inv} | {lat_str} | {tokens} | {cost} |"
            )
        lines.append("")

    # Verdict.
    lines.append("## Verdict")
    lines.append("")
    baseline_report = next((r for r in reports if r["provider"] == "embedding_router"), None)
    llm_reports = [r for r in reports if r["provider"] != "embedding_router" and r["available"]]

    if baseline_report is None:
        lines.append("- Geen embedding-router baseline gevonden in de rapporten.")
    else:
        baseline_acc = baseline_report["accuracy"]
        baseline_acc_str = f"{baseline_acc:.2%}" if baseline_acc is not None else "n/a"
        lines.append(
            f"- **Embedding-router baseline:** accuracy={baseline_acc_str}, "
            f"latency {_format_latency(baseline_report['latency_ms'])}, kosten $0."
        )

    if not llm_reports:
        lines.append(
            "- **Geen LLM-selector beschikbaar** in deze run (ontbreekt API key of lokale server). "
            "De embedding-router baseline is de enige bruikbare optie zonder externe afhankelijkheden."
        )
    else:
        best = max(
            llm_reports,
            key=lambda r: (
                r["accuracy"] if r["accuracy"] is not None else -1.0,
                -(r["latency_ms"]["p95"] if r["latency_ms"]["n"] else float("inf")),
            ),
        )
        best_acc = best["accuracy"]
        baseline_acc = baseline_report["accuracy"] if baseline_report else None
        improvement = (best_acc - baseline_acc) if best_acc is not None and baseline_acc is not None else None

        best_acc_str = f"{best_acc:.2%}" if best_acc is not None else "n/a"
        best_cost = best["estimated_cost_usd"]
        best_cost_str = f"${best_cost:.6f}" if best_cost is not None else "n/a"
        lines.append(
            f"- **Beste beschikbare LLM-selector:** {best['model']} "
            f"(accuracy={best_acc_str}, "
            f"latency {_format_latency(best['latency_ms'])}, "
            f"kosten {best_cost_str})."
        )
        if improvement is None:
            lines.append(
                "- **Conclusie:** kan niet worden vergeleken omdat de baseline of LLM geen scores produceerde."
            )
        elif improvement <= 0.0:
            lines.append(
                f"- **Conclusie:** de LLM-selector verbetert de baseline niet (Δ={improvement:+.2%}). "
                "De embedding-router is voldoende; geen cloud-kosten of -latency nodig."
            )
        elif improvement < 0.05:
            lines.append(
                f"- **Conclusie:** marginale verbetering (Δ={improvement:+.2%}) rechtvaardigt "
                "de extra latency/kosten van een LLM-selector niet op deze set."
            )
        else:
            lines.append(
                f"- **Conclusie:** de LLM-selector levert een relevante verbetering op ambigue gevallen "
                f"(Δ={improvement:+.2%}). {best['model']} is de beste geteste tier voor deze use-case."
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# NDJSON serialization
# ---------------------------------------------------------------------------


def _result_to_dict(result: CardSelectionResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "source": result.source,
        "pain_point": result.pain_point,
        "industry": result.industry,
        "expected_case_id": result.expected_case_id,
        "predicted_case_id": result.predicted_case_id,
        "selector": result.selector,
        "embedding_latency_ms": result.embedding_latency_ms,
        "llm_latency_ms": result.llm_latency_ms,
        "latency_ms": result.latency_ms,
        "llm_provider": result.llm_provider,
        "llm_model": result.llm_model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "valid_card_ids": result.valid_card_ids,
        "error": result.error,
    }


def records_to_ndjson(results: list[CardSelectionResult], path: Path) -> None:
    """Write ``results`` as one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(_result_to_dict(result), ensure_ascii=False) + "\n")
