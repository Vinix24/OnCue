"""Summarization / action-item shootout harness for the LLM reframe layer.

This module evaluates how well a model extracts structured action items from a
full B2B conversation transcript. It uses the same model-spec / pricing / latency
infrastructure as ``cascade_eval.py`` so that the two shootouts can be compared
side-by-side.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.thinking_policy import ThinkingPolicy
from sales_copilot.modules.detector.eval_shared import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PACE_MS,
    ModelSpec,
    PricingTable,
    is_provider_available,
    latency_stats,
    pace,
    retry_with_backoff,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


class ActionItem(BaseModel):
    """One concrete action item extracted from a sales conversation."""

    description: str = Field(
        ...,
        description="Concrete, beknopte actiebeschrijving in het Nederlands.",
    )
    owner: str | None = Field(
        None,
        description=(
            "Wie de actie uitvoert: 'prospect', 'sales', 'beide', of een naam "
            "als die expliciet wordt genoemd."
        ),
    )
    due_hint: str | None = Field(
        None,
        description="Deadline of tijdsindicatie zoals genoemd in het gesprek.",
    )
    priority: Literal["low", "medium", "high"] = Field(
        "medium",
        description="Prioriteit afgeleid uit de urgentie in de gesprekscontext.",
    )
    context_quote: str | None = Field(
        None,
        description="Letterlijke, korte quote uit het transcript die de actie onderbouwt.",
    )


class ActionItems(BaseModel):
    """Strict schema returned by the LLM via instructor."""

    items: list[ActionItem] = Field(
        default_factory=list,
        description="Lijst van concrete actiepunten geëxtraheerd uit het gesprek.",
    )


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass
class SummaryEvalRecord:
    """One transcript after running through the action-item extractor."""

    source: str
    transcript: str
    reference: list[dict[str, Any]] = field(default_factory=list)
    predicted: list[dict[str, Any]] | None = None
    latency_ms: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    schema_compliant: bool = False
    error: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "transcript": self.transcript,
            "reference": self.reference,
            "predicted": self.predicted,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "schema_compliant": self.schema_compliant,
            "error": self.error,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
        }


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------


def _transcript_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def load_transcript_records(path: Path) -> list[dict[str, Any]]:
    """Load transcripts from a directory tree or a single file.

    When ``path`` is a directory, every ``transcript_ours.txt`` found recursively
    becomes one record. A sibling ``action_items.json`` is used as ground truth
    when present. When ``path`` is a file, it becomes a single record without
    ground truth.
    """
    records: list[dict[str, Any]] = []
    if path.is_dir():
        for transcript_file in sorted(path.rglob("transcript_ours.txt")):
            source = transcript_file.parent.name
            text = transcript_file.read_text(encoding="utf-8")
            reference = _load_ground_truth(transcript_file.parent / "action_items.json")
            records.append(
                {
                    "source": source,
                    "transcript": text,
                    "transcript_lines": _transcript_lines(text),
                    "reference": reference,
                }
            )
    elif path.is_file():
        text = path.read_text(encoding="utf-8")
        reference = _load_ground_truth(path.with_suffix(".action_items.json"))
        records.append(
            {
                "source": path.stem,
                "transcript": text,
                "transcript_lines": _transcript_lines(text),
                "reference": reference,
            }
        )
    return records


def _load_ground_truth(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Could not parse ground-truth file %s", path)
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items", [])
    return []


def load_reference_set(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load a single JSON mapping source-name -> list of action items.

    This supports a frontier / multi-pass reference produced earlier, e.g.
    ``data/fireflies/ground_truth.json``.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k): v if isinstance(v, list) else [] for k, v in data.items()}
    return {}


# ---------------------------------------------------------------------------
# Action-item extraction
# ---------------------------------------------------------------------------


_ACTION_ITEM_SYSTEM_PROMPT = (
    "Je bent een zakelijke assistent voor B2B-verkoopgesprekken. Lees het volgende "
    "Nederlandse gesprekstranscript en extraheer alle concrete actiepunten. "
    "Gebruik het gestructureerde JSON-schema. Elk actiepunt heeft een duidelijke "
    "beschrijving, optioneel een owner, optioneel een due_hint, een priority "
    "(low/medium/high) en een korte context_quote uit het transcript. "
    "Als er geen actiepunten zijn, retourneer een lege items-lijst."
)

# Generous output-token floor for the summarization call (EVAL-ONLY -- see
# ``eval_shared.py``'s resilience-helpers module note). The fase-B model panel includes
# thinking-by-default models (e.g. Qwen3.6 via OpenRouter); with no explicit ``max_tokens``,
# the reasoning trace can exhaust the provider default before the final JSON is emitted,
# which comes back as ``finish_reason='length'`` and a schema-validation failure. This floor
# is applied unconditionally rather than gated on a per-model "is this a thinking model"
# check: ``LLMClient.create`` already skips ``max_tokens`` for gemini/vertex (see its
# docstring), and a higher ceiling is a no-op for a non-thinking OpenAI-compatible model --
# actual completions stay far under it, so it costs nothing to be generous everywhere.
OUTPUT_TOKENS_FLOOR = 4096


def _summary_max_tokens(thinking: ThinkingPolicy | None) -> int:
    """Output-token budget for one summarization call: the floor, or bigger for a large budget."""
    if thinking is not None and thinking.thinking_on and thinking.reasoning_budget:
        # Headroom past the reasoning budget itself for the final JSON answer.
        return max(OUTPUT_TOKENS_FLOOR, thinking.reasoning_budget + 1024)
    return OUTPUT_TOKENS_FLOOR


def _build_eval_config(config: DetectorConfig, spec: ModelSpec) -> DetectorConfig:
    """Return a config copy wired to the requested model spec."""
    if config.llm_provider == spec.provider and config.llm_model == spec.model:
        return config
    from dataclasses import replace

    return replace(
        config,
        llm_provider=spec.provider,
        llm_model=spec.model,
    )


def extract_action_items(
    transcript_lines: list[str],
    config: DetectorConfig,
    spec: ModelSpec,
    thinking: ThinkingPolicy | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[ActionItems | None, dict[str, Any]]:
    """Run one structured action-item extraction call and return timing/token metadata.

    ``thinking`` is optional and defaults to ``None`` (unchanged, provider-default
    thinking behavior) -- used by a future thinking-sweep over the summarization track.

    ``max_retries``/``base_delay_s``/``pace_ms`` configure EVAL-ONLY retry-with-backoff and
    inter-call pacing on this one call (see ``eval_shared.retry_with_backoff``/``pace``) --
    this module has no live-path caller, so the live copilot's latency budget is unaffected.
    """
    cfg = _build_eval_config(config, spec)
    client = LLMClient(spec.provider, timeout_ms=cfg.llm_timeout_ms)

    user_text = "Gesprekstranscript:\n" + "\n".join(f"- {line}" for line in transcript_lines)
    max_tokens = _summary_max_tokens(thinking)

    start = time.perf_counter()
    try:
        pace(pace_ms)
        response = retry_with_backoff(
            lambda: client.create(
                model=cfg.llm_model,
                system_prompt=_ACTION_ITEM_SYSTEM_PROMPT,
                user_text=user_text,
                response_model=ActionItems,
                temperature=cfg.llm_temperature,
                allow_local=True,
                thinking=thinking,
                max_tokens=max_tokens,
            ),
            max_retries=max_retries,
            base_delay_s=base_delay_s,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - start) * 1000
        logger.warning("Action-item extraction failed for %s: %s", spec, exc)
        return None, {
            "latency_ms": latency_ms,
            "usage": {},
            "schema_compliant": False,
            "error": str(exc),
        }

    latency_ms = (time.perf_counter() - start) * 1000
    usage = client.last_usage or {}

    if response is None:
        return None, {
            "latency_ms": latency_ms,
            "usage": usage,
            "schema_compliant": False,
            "error": "LLM returned None (provider disabled or timeout)",
        }

    # response is validated by instructor against ActionItems, so it is schema-compliant.
    return response, {
        "latency_ms": latency_ms,
        "usage": usage,
        "schema_compliant": True,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for fuzzy matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _meaningful_tokens(text: str) -> set[str]:
    """Return the set of normalized, non-trivial words."""
    return {token for token in _normalize(text).split() if len(token) > 2}


def _descriptions_match(pred_desc: str, ref_desc: str) -> bool:
    """Return True when two descriptions refer to the same action item.

    Matching uses exact normalized equality, substring containment, or a token
    overlap coefficient of at least 0.5 (half of the shorter description's
    meaningful words appear in the longer one).
    """
    if pred_desc == ref_desc:
        return True
    if pred_desc in ref_desc or ref_desc in pred_desc:
        return True

    pred_tokens = _meaningful_tokens(pred_desc)
    ref_tokens = _meaningful_tokens(ref_desc)
    if not pred_tokens or not ref_tokens:
        return False

    intersection = pred_tokens & ref_tokens
    overlap = len(intersection) / min(len(pred_tokens), len(ref_tokens))
    return overlap >= 0.5


def _match_action_items(
    predicted: list[dict[str, Any]],
    reference: list[dict[str, Any]],
) -> tuple[int, int, int]:
    """Return (tp, fp, fn) using fuzzy description matching.

    Each reference item can only be matched once.
    """
    matched_ref: set[int] = set()
    tp = fp = 0

    for pred in predicted:
        pred_desc = _normalize(str(pred.get("description", "")))
        if not pred_desc:
            continue
        found = False
        for idx, ref in enumerate(reference):
            if idx in matched_ref:
                continue
            ref_desc = _normalize(str(ref.get("description", "")))
            if not ref_desc:
                continue
            if _descriptions_match(pred_desc, ref_desc):
                matched_ref.add(idx)
                found = True
                break
        if found:
            tp += 1
        else:
            fp += 1

    fn = len(reference) - len(matched_ref)
    return tp, fp, fn


def compute_action_item_metrics(
    predicted: list[dict[str, Any]] | None,
    reference: list[dict[str, Any]],
) -> dict[str, Any]:
    """Precision/recall/hallucination for one transcript's action items."""
    if predicted is None:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "hallucination_rate": 0.0,
            "true_positives": 0,
            "false_positives": 0,
            "false_negatives": len(reference),
        }

    tp, fp, fn = _match_action_items(predicted, reference)
    total_predictions = tp + fp
    total_reference = len(reference)

    precision = tp / total_predictions if total_predictions else 0.0
    recall = tp / total_reference if total_reference else 0.0
    hallucination_rate = fp / total_predictions if total_predictions else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "hallucination_rate": hallucination_rate,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


# ---------------------------------------------------------------------------
# Run + aggregate
# ---------------------------------------------------------------------------


def run_summarization_eval(
    records: list[dict[str, Any]],
    config: DetectorConfig,
    spec: ModelSpec,
    thinking: ThinkingPolicy | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[list[SummaryEvalRecord], bool, str]:
    """Extract action items for every transcript using ``spec``.

    Returns the per-transcript records, plus availability info. If the provider
    is not available the records still contain availability metadata so the
    matrix can report ``available=False`` without crashing. ``thinking`` is
    forwarded to every ``extract_action_items`` call unchanged. ``max_retries``/
    ``base_delay_s``/``pace_ms`` are EVAL-ONLY resilience knobs, also forwarded unchanged.
    """
    available, reason = is_provider_available(spec.provider)
    effective_spec = spec
    if not available:
        logger.warning(
            "%s unavailable, marking records as unavailable: %s", spec, reason
        )
        effective_spec = ModelSpec(provider="none", model="none", description=spec.description)

    results: list[SummaryEvalRecord] = []
    for rec in records:
        if effective_spec.provider == "none":
            results.append(
                SummaryEvalRecord(
                    source=rec["source"],
                    transcript=rec["transcript"],
                    reference=rec.get("reference", []),
                    predicted=None,
                    latency_ms=0.0,
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    schema_compliant=False,
                    error=f"provider unavailable: {reason}",
                    llm_provider=effective_spec.provider,
                    llm_model=effective_spec.model,
                )
            )
            continue

        predicted, meta = extract_action_items(
            rec["transcript_lines"],
            config,
            effective_spec,
            thinking=thinking,
            max_retries=max_retries,
            base_delay_s=base_delay_s,
            pace_ms=pace_ms,
        )
        predicted_list = [item.model_dump() for item in predicted.items] if predicted else None
        usage = meta.get("usage", {})
        results.append(
            SummaryEvalRecord(
                source=rec["source"],
                transcript=rec["transcript"],
                reference=rec.get("reference", []),
                predicted=predicted_list,
                latency_ms=meta["latency_ms"],
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
                schema_compliant=meta.get("schema_compliant", False),
                error=meta.get("error"),
                llm_provider=effective_spec.provider,
                llm_model=effective_spec.model,
            )
        )

    return results, available, reason


def build_per_model_report(
    records: list[SummaryEvalRecord],
    pricing: PricingTable,
    spec: ModelSpec,
    available: bool,
    reason: str,
) -> dict[str, Any]:
    """Aggregate per-transcript records into a single model report."""
    total = len(records)
    schema_compliant_count = sum(1 for rec in records if rec.schema_compliant)
    schema_compliance_rate = schema_compliant_count / total if total else 0.0

    all_metrics = [compute_action_item_metrics(rec.predicted, rec.reference) for rec in records]
    tp = sum(m["true_positives"] for m in all_metrics)
    fp = sum(m["false_positives"] for m in all_metrics)
    fn = sum(m["false_negatives"] for m in all_metrics)
    total_predictions = tp + fp
    total_reference = tp + fn

    precision = tp / total_predictions if total_predictions else 0.0
    recall = tp / total_reference if total_reference else 0.0
    hallucination_rate = fp / total_predictions if total_predictions else 0.0

    latencies = [rec.latency_ms for rec in records]
    total_input_tokens = sum(rec.input_tokens or 0 for rec in records)
    total_output_tokens = sum(rec.output_tokens or 0 for rec in records)

    cost = pricing.estimate_cost(spec.provider, spec.model, total_input_tokens, total_output_tokens)

    return {
        "model": str(spec),
        "provider": spec.provider,
        "model_name": spec.model,
        "available": available,
        "availability_reason": reason,
        "total": total,
        "schema_compliant_count": schema_compliant_count,
        "schema_compliance_rate": schema_compliance_rate,
        "precision": precision,
        "recall": recall,
        "hallucination_rate": hallucination_rate,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "latency_ms": latency_stats(latencies),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "estimated_cost_usd": cost,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_latency(stats: dict[str, float]) -> str:
    if not stats.get("n"):
        return "—"
    return f"p50 {stats['p50']:.1f} ms / p95 {stats['p95']:.1f} ms / mean {stats['mean']:.1f} ms"


def _format_cost(cost: float | None) -> str:
    if cost is None:
        return "n/a"
    if cost == 0.0:
        return "$0.00"
    return f"${cost:.6f}"


def format_markdown_report(
    reports: list[dict[str, Any]],
    eval_set_path: Path,
) -> str:
    """Render a side-by-side summarization shootout report."""
    lines: list[str] = [
        "# Samenvattings / actiepunten evaluatie-rapport",
        "",
        f"**Eval-set:** {eval_set_path}",
        "",
    ]

    for report in reports:
        lines.append(f"## Model: {report['model']}")
        lines.append("")
        avail = "✅ beschikbaar" if report["available"] else f"⚠️  niet beschikbaar ({report['availability_reason']})"
        lines.append(f"**Beschikbaarheid:** {avail}")
        lines.append("")
        lines.append(f"**Totaal geëvalueerd:** {report['total']} transcripties")
        lines.append("")
        lines.append("### Kwaliteit")
        lines.append("")
        lines.append(
            f"- **Precision:** {report['precision']:.2f} "
            f"(TP={report['true_positives']}, FP={report['false_positives']})"
        )
        lines.append(
            f"- **Recall:** {report['recall']:.2f} "
            f"(TP={report['true_positives']}, FN={report['false_negatives']})"
        )
        lines.append(f"- **Hallucinatie-rate:** {report['hallucination_rate']:.2f}")
        lines.append(
            f"- **Schema-compliance:** {report['schema_compliance_rate'] * 100:.1f}% "
            f"({report['schema_compliant_count']}/{report['total']} transcripties)"
        )
        lines.append("")
        lines.append("### Latency")
        lines.append("")
        lines.append(f"- **Actiepunt-extractie:** {_format_latency(report['latency_ms'])}")
        lines.append("")
        if report["total_input_tokens"] or report["total_output_tokens"]:
            lines.append(
                f"**LLM tokens:** input={report['total_input_tokens']}, "
                f"output={report['total_output_tokens']}"
            )
            lines.append(f"**Geschatte kosten:** {_format_cost(report['estimated_cost_usd'])}")
        lines.append("")

    # Side-by-side model matrix.
    if len(reports) > 1:
        lines.append("## Model-matrix (shootout)")
        lines.append("")
        lines.append(
            "| Model | Beschikbaar | Precision | Recall | Hallucinatie | Schema-compliance | "
            "Latency p50/p95 | Kosten ($) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for report in reports:
            lat = report["latency_ms"]
            lat_str = f"{lat['p50']:.0f}/{lat['p95']:.0f} ms" if lat.get("n") else "n/a"
            avail = "ja" if report["available"] else "nee"
            lines.append(
                f"| {report['model']} | {avail} | "
                f"{report['precision']:.2f} | {report['recall']:.2f} | "
                f"{report['hallucination_rate']:.2f} | "
                f"{report['schema_compliance_rate'] * 100:.1f}% | "
                f"{lat_str} | {_format_cost(report['estimated_cost_usd'])} |"
            )
        lines.append("")

    # Verdict section.
    lines.append("## Tier-verdict")
    lines.append("")
    available = [r for r in reports if r["available"]]
    if not available:
        lines.append(
            "Geen enkel model was beschikbaar in deze run. Configureer een LLM-key "
            "of start Ollama om de schema-eval uit te voeren."
        )
    else:
        def _score(r: dict[str, Any]) -> float:
            # Simple ranking: reward precision/recall and compliance, penalize hallucinations.
            return (
                r["precision"]
                + r["recall"]
                + r["schema_compliance_rate"]
                - r["hallucination_rate"]
            )

        best = max(available, key=_score)
        lines.append(
            f"- **Beste model voor summarization-laag:** {best['model']} "
            f"(precision={best['precision']:.2f}, recall={best['recall']:.2f}, "
            f"schema-compliance={best['schema_compliance_rate'] * 100:.1f}%)."
        )
        local = [r for r in available if r["provider"] == "ollama"]
        cloud = [r for r in available if r["provider"] != "ollama"]
        if local:
            lines.append(
                f"- **Lokaal alternatief:** {local[0]['model']} — kostenefficiënt, "
                "maar controleer latency en schema-compliance."
            )
        if cloud:
            fastest = min(cloud, key=lambda r: r["latency_ms"]["p50"] or float("inf"))
            lines.append(
                f"- **Snelste cloud-optie:** {fastest['model']} "
                f"({_format_latency(fastest['latency_ms'])})."
            )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# NDJSON serialization
# ---------------------------------------------------------------------------


def records_to_ndjson(records: list[SummaryEvalRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
