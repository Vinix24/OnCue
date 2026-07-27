"""Cascade-flow evaluation harness for the pain-point detector.

This module instruments the real ``DetectionPipeline`` (see ``pipeline.py``) and
produces per-layer coverage, latency, quality and cost metrics. It is the shared
backend for ``scripts/eval_cascade.py`` and for the cascade-specific tests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sales_copilot.core.config import DetectorConfig, load_yaml
from sales_copilot.core.thinking_policy import ThinkingPolicy
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.eval_mining import load_jsonl_records, load_records
from sales_copilot.modules.detector.eval_shared import (
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PACE_MS,
    ModelSpec,
    PricingTable,
    is_provider_available,
    latency_stats,
    wrap_llm_client_for_eval,
)
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.detector.pipeline import DetectionHooks, DetectionPipeline
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook that records per-utterance cascade internals
# ---------------------------------------------------------------------------


@dataclass
class LayerRecord:
    """One eval example after running through the instrumented cascade."""

    text: str
    source: str
    ground_truth: str | None
    layer: str
    predicted_category: str | None
    predicted_confidence: float
    embedding_latency_ms: float
    llm_latency_ms: float | None
    llm_provider: str | None
    llm_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error: str | None = None


class CascadeEvalHooks(DetectionHooks):
    """Collect timing and routing decisions from the real pipeline."""

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}

    def reset(self) -> None:
        self.last = {
            "match": None,
            "embedding_latency_ms": 0.0,
            "llm_latency_ms": None,
            "confirmation": None,
            "llm_provider": None,
            "llm_model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

    def on_classify_start(self) -> None:
        self.reset()

    def on_classify_end(self, match: RouteMatch | None, latency_ms: float) -> None:
        self.last["match"] = match
        self.last["embedding_latency_ms"] = latency_ms

    def on_llm_start(self) -> None:
        return

    def on_llm_end(
        self,
        confirmation: object | None,
        latency_ms: float,
        tokens: dict[str, int] | None,
    ) -> None:
        self.last["confirmation"] = confirmation
        self.last["llm_latency_ms"] = latency_ms
        if tokens:
            self.last["input_tokens"] = tokens.get("input_tokens")
            self.last["output_tokens"] = tokens.get("output_tokens")
            self.last["total_tokens"] = tokens.get("total_tokens")

    def on_event(self, event: object | None) -> None:
        self.last["event"] = event


def _none_config(config: DetectorConfig) -> DetectorConfig:
    """Return a copy of ``config`` with the LLM provider disabled."""
    from dataclasses import replace

    return replace(config, llm_provider="none", llm_model="none")


# ---------------------------------------------------------------------------
# Pipeline construction per model
# ---------------------------------------------------------------------------


def build_pipeline_for_spec(
    config: DetectorConfig,
    router: PainPointRouter,
    debouncer: PainPointDebouncer,
    spec: ModelSpec,
    hooks: CascadeEvalHooks,
    thinking: ThinkingPolicy | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[DetectionPipeline, ModelSpec, bool, str]:
    """Build a DetectionPipeline for ``spec`` and report whether the LLM is available.

    If the requested provider is not available (missing key, Ollama down, ...)
    the pipeline is built with ``provider=none`` so the deterministic and
    embedding layers still run. The returned ``effective_spec`` reflects the
    provider that was actually wired into the pipeline; callers should use it
    when recording per-utterance layer decisions so unavailable providers are
    reported as ``llm_unavailable`` rather than ``llm_escalation``.

    ``thinking`` is optional and defaults to ``None`` (unchanged, provider-default
    thinking behavior). When given, it is forwarded to the ``LLMConfirmClient`` so
    every LLM-confirm call for this pipeline honors the requested thinking-mode x
    reasoning-budget policy -- used by the fase-B thinking-sweep.

    ``max_retries``/``base_delay_s``/``pace_ms`` configure EVAL-ONLY retry-with-backoff and
    inter-call pacing on the ``LLMConfirmClient``'s underlying ``LLMClient`` instance (see
    ``eval_shared.wrap_llm_client_for_eval``) -- the live copilot never goes through this
    function, so its 3s-budget path is unaffected regardless of these values.
    """
    available, reason = is_provider_available(spec.provider)
    effective_spec = spec
    if not available:
        logger.warning("%s unavailable, falling back to 'none' for fast-path run: %s", spec, reason)
        effective_spec = ModelSpec(provider="none", model="none", description=spec.description)

    cfg = config
    if cfg.llm_provider != effective_spec.provider or cfg.llm_model != effective_spec.model:
        from dataclasses import replace

        cfg = replace(
            cfg,
            llm_provider=effective_spec.provider,
            llm_model=effective_spec.model,
        )

    # Build the LLM client outside the pipeline so we can catch construction
    # errors (e.g. missing SDK) without crashing the harness.
    llm_client: LLMConfirmClient | None = None
    try:
        llm_client = LLMConfirmClient(cfg, thinking=thinking)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to build LLM client for %s: %s", effective_spec, exc)
        cfg = _none_config(cfg)
        llm_client = LLMConfirmClient(cfg, thinking=thinking)
        effective_spec = ModelSpec(provider="none", model="none", description=spec.description)
        available = False
        reason = f"LLM client construction failed: {exc}"

    wrap_llm_client_for_eval(
        llm_client._llm, max_retries=max_retries, base_delay_s=base_delay_s, pace_ms=pace_ms
    )

    pipeline = DetectionPipeline(
        config=cfg,
        router=router,
        llm_client=llm_client,
        debouncer=debouncer,
        hooks=hooks,
    )
    return pipeline, effective_spec, available, reason


# ---------------------------------------------------------------------------
# Eval-set loading
# ---------------------------------------------------------------------------


def _valid_categories(config: DetectorConfig) -> frozenset[str]:
    data = load_yaml(config.pain_points_config)
    return frozenset(
        route["name"]
        for route in data.get("routes", [])
        if isinstance(route, dict) and isinstance(route.get("name"), str)
    )


def _records_from_json(path: Path) -> list[dict[str, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [
            {
                "text": str(rec.get("text", "")),
                "label": str(rec.get("label", "review")),
                "source": str(rec.get("source", path.stem)),
            }
            for rec in raw
            if isinstance(rec, dict) and str(rec.get("text", "")).strip()
        ]
    if isinstance(raw, dict) and "events" in raw:
        records: list[dict[str, str]] = []
        for event in raw["events"]:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "transcript":
                continue
            text = str(event.get("text", "")).strip()
            if not text:
                continue
            speaker = str(event.get("speaker", "prospect")).strip().lower()
            if speaker == "self":
                continue
            records.append(
                {
                    "text": text,
                    "label": "review",
                    "source": path.stem,
                }
            )
        return records
    raise ValueError(f"Unsupported JSON eval-set structure in {path}")


def load_eval_records(path: Path) -> list[dict[str, str]]:
    """Load records from JSONL, JSON (list or transcript) or Fireflies markdown."""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return [
            {
                "text": str(rec.get("text", "")),
                "label": str(rec.get("label", "review")),
                "source": str(rec.get("source", path.stem)),
            }
            for rec in load_jsonl_records(path)
            if len(str(rec.get("text", "")).strip()) >= 10
        ]
    if suffix == ".json":
        return _records_from_json(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return load_records(path)
    raise ValueError(f"Unsupported eval-set format: {path}")


# ---------------------------------------------------------------------------
# Running the cascade on a eval set
# ---------------------------------------------------------------------------


def _classify_layer(
    match: RouteMatch | None,
    event: object | None,
    config: DetectorConfig,
    llm_provider: str,
) -> str:
    high = config.confidence_threshold_high
    low = config.confidence_threshold_low

    if match is None or match.confidence < low:
        return "deterministic_reject"
    if match.confidence >= high:
        if match.source == "keyword":
            return "deterministic_accept"
        return "embedding_fast_path"
    # Middle band.
    if llm_provider in ("", "none"):
        return "llm_unavailable"
    return "llm_escalation"


def run_cascade_on_records(
    pipeline: DetectionPipeline,
    records: list[dict[str, str]],
    config: DetectorConfig,
    spec: ModelSpec,
    hooks: CascadeEvalHooks,
    max_utterances: int | None = None,
) -> list[LayerRecord]:
    """Run every eval record through the instrumented pipeline."""
    valid_cats = _valid_categories(config)
    results: list[LayerRecord] = []
    subset = records if max_utterances is None else records[:max_utterances]

    for rec in subset:
        text = rec["text"]
        label = rec.get("label", "review") or "review"
        ground_truth = label if label in valid_cats else None
        source = rec.get("source", "")

        try:
            event = pipeline.process(text, speaker="prospect")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline failed on record from %s", source)
            results.append(
                LayerRecord(
                    text=text,
                    source=source,
                    ground_truth=ground_truth,
                    layer="error",
                    predicted_category=None,
                    predicted_confidence=0.0,
                    embedding_latency_ms=0.0,
                    llm_latency_ms=None,
                    llm_provider=spec.provider,
                    llm_model=spec.model,
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    error=str(exc),
                )
            )
            continue

        last = hooks.last
        match = last.get("match")
        confirmation = last.get("confirmation")
        layer = _classify_layer(match, event, config, spec.provider)

        predicted_category: str | None = None
        predicted_confidence = 0.0
        if event is not None:
            predicted_category = getattr(event, "category", None)
            predicted_confidence = getattr(event, "confidence", 0.0)
        elif confirmation is not None and layer == "llm_escalation":
            # LLM rejected the uncertain match.
            predicted_category = getattr(confirmation, "category", None)
            predicted_confidence = getattr(confirmation, "confidence", 0.0)
        elif match is not None and layer in ("deterministic_accept", "embedding_fast_path"):
            predicted_category = match.category
            predicted_confidence = match.confidence

        results.append(
            LayerRecord(
                text=text,
                source=source,
                ground_truth=ground_truth,
                layer=layer,
                predicted_category=predicted_category,
                predicted_confidence=predicted_confidence,
                embedding_latency_ms=last.get("embedding_latency_ms", 0.0),
                llm_latency_ms=last.get("llm_latency_ms"),
                llm_provider=spec.provider,
                llm_model=spec.model,
                input_tokens=last.get("input_tokens"),
                output_tokens=last.get("output_tokens"),
                total_tokens=last.get("total_tokens"),
                error=None,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Metrics and aggregation
# ---------------------------------------------------------------------------


def compute_quality_metrics(
    records: list[LayerRecord],
    valid_categories: frozenset[str],
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Precision/recall/hallucination over records that have ground-truth labels."""
    tp = fp = fn = hallucination = 0
    judged = 0
    for rec in records:
        pred = rec.predicted_category
        gold = rec.ground_truth
        has_gold = gold is not None
        if pred is not None and pred in valid_categories:
            if has_gold:
                judged += 1
                if pred == gold:
                    tp += 1
                else:
                    fp += 1
            else:
                fp += 1
                hallucination += 1
        elif has_gold:
            judged += 1
            fn += 1

    total_predictions = tp + fp
    precision = tp / total_predictions if total_predictions else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    hallucination_rate = hallucination / total_predictions if total_predictions else 0.0
    return {
        "judged": judged,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "predictions": total_predictions,
        "precision": precision,
        "recall": recall,
        "hallucination_rate": hallucination_rate,
    }


def aggregate_layer_stats(records: list[LayerRecord]) -> dict[str, Any]:
    total = len(records)
    layer_counts: dict[str, int] = {}
    embedding_latencies: list[float] = []
    llm_latencies: list[float] = []
    total_llm_input_tokens = 0
    total_llm_output_tokens = 0

    for rec in records:
        layer_counts[rec.layer] = layer_counts.get(rec.layer, 0) + 1
        embedding_latencies.append(rec.embedding_latency_ms)
        if rec.llm_latency_ms is not None:
            llm_latencies.append(rec.llm_latency_ms)
        if rec.input_tokens:
            total_llm_input_tokens += rec.input_tokens
        if rec.output_tokens:
            total_llm_output_tokens += rec.output_tokens

    coverage = {layer: (count / total if total else 0.0) for layer, count in layer_counts.items()}
    escalation_count = layer_counts.get("llm_escalation", 0)
    escalation_rate = escalation_count / total if total else 0.0

    return {
        "total": total,
        "layer_counts": layer_counts,
        "coverage": coverage,
        "escalation_count": escalation_count,
        "escalation_rate": escalation_rate,
        "embedding_latency_ms": latency_stats(embedding_latencies),
        "llm_latency_ms": latency_stats(llm_latencies),
        "total_llm_input_tokens": total_llm_input_tokens,
        "total_llm_output_tokens": total_llm_output_tokens,
    }


def build_per_model_report(
    records: list[LayerRecord],
    config: DetectorConfig,
    pricing: PricingTable,
    spec: ModelSpec,
    available: bool,
    reason: str,
) -> dict[str, Any]:
    valid_cats = _valid_categories(config)
    layer_stats = aggregate_layer_stats(records)
    fast_path_records = [
        r for r in records if r.layer in ("deterministic_accept", "embedding_fast_path")
    ]
    fast_path_metrics = compute_quality_metrics(fast_path_records, valid_cats)
    full_metrics = compute_quality_metrics(records, valid_cats)

    cost = pricing.estimate_cost(
        spec.provider,
        spec.model,
        layer_stats["total_llm_input_tokens"],
        layer_stats["total_llm_output_tokens"],
    )

    return {
        "model": str(spec),
        "provider": spec.provider,
        "model_name": spec.model,
        "available": available,
        "availability_reason": reason,
        "total": layer_stats["total"],
        "layer_counts": layer_stats["layer_counts"],
        "coverage": layer_stats["coverage"],
        "escalation_rate": layer_stats["escalation_rate"],
        "embedding_latency_ms": layer_stats["embedding_latency_ms"],
        "llm_latency_ms": layer_stats["llm_latency_ms"],
        "fast_path_metrics": fast_path_metrics,
        "full_metrics": full_metrics,
        "total_llm_input_tokens": layer_stats["total_llm_input_tokens"],
        "total_llm_output_tokens": layer_stats["total_llm_output_tokens"],
        "estimated_cost_usd": cost,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_latency(stats: dict[str, float]) -> str:
    if not stats.get("n"):
        return "—"
    return f"p50 {stats['p50']:.1f} ms / p95 {stats['p95']:.1f} ms / mean {stats['mean']:.1f} ms"


def _format_metrics(metrics: dict[str, Any]) -> str:
    if metrics.get("judged", 0) == 0:
        return "Geen gelabelde ground-truth beschikbaar."
    return (
        f"precision={metrics['precision']:.2f}, recall={metrics['recall']:.2f}, "
        f"hallucination={metrics['hallucination_rate']:.2f} "
        f"(TP={metrics['true_positives']}, FP={metrics['false_positives']}, FN={metrics['false_negatives']})"
    )


def _layer_table(records: list[LayerRecord]) -> str:
    from collections import Counter

    counts = Counter(r.layer for r in records)
    total = len(records)
    lines = ["| Laag | Aantal | Coverage |", "|---|---|---|"]
    for layer in (
        "deterministic_accept",
        "embedding_fast_path",
        "deterministic_reject",
        "llm_escalation",
        "llm_unavailable",
        "error",
    ):
        if layer not in counts:
            continue
        count = counts[layer]
        lines.append(f"| {layer} | {count} | {count / total * 100:.1f}% |")
    return "\n".join(lines)


def format_markdown_report(
    reports: list[dict[str, Any]],
    eval_set_path: Path,
    config: DetectorConfig,
) -> str:
    """Render a human-readable markdown summary of one or more model runs."""
    lines: list[str] = [
        "# Cascade-flow evaluatie-rapport",
        "",
        f"**Eval-set:** {eval_set_path}",
        f"**Embedding model:** {config.embedding_model}",
        f"**Thresholds:** low={config.confidence_threshold_low}, high={config.confidence_threshold_high}",
        "",
    ]

    for report in reports:
        lines.append(f"## Model: {report['model']}")
        lines.append("")
        avail = "✅ beschikbaar" if report["available"] else f"⚠️  niet beschikbaar ({report['availability_reason']})"
        lines.append(f"**Beschikbaarheid:** {avail}")
        lines.append("")
        lines.append(f"**Totaal geëvalueerd:** {report['total']} utterances")
        lines.append("")
        lines.append("### Per-laag coverage")
        lines.append("")
        # Reconstruct layer table from layer_counts/coverage.
        lines.append("| Laag | Aantal | Coverage |")
        lines.append("|---|---|---|")
        for layer, count in report["layer_counts"].items():
            lines.append(f"| {layer} | {count} | {report['coverage'][layer] * 100:.1f}% |")
        lines.append("")
        lines.append(
            f"**Escalatie-rate naar LLM:** {report['escalation_rate'] * 100:.1f}% "
            f"({report['layer_counts'].get('llm_escalation', 0)} utterances)"
        )
        lines.append("")
        lines.append("### Latency")
        lines.append("")
        lines.append(f"- **Embedding-classify:** {_format_latency(report['embedding_latency_ms'])}")
        lines.append(f"- **LLM-confirm:** {_format_latency(report['llm_latency_ms'])}")
        lines.append("")
        lines.append("### Kwaliteit")
        lines.append("")
        lines.append(f"- **Alleen fast-path:** {_format_metrics(report['fast_path_metrics'])}")
        lines.append(f"- **Volledige cascade:** {_format_metrics(report['full_metrics'])}")
        if report["total_llm_input_tokens"] or report["total_llm_output_tokens"]:
            lines.append("")
            lines.append(
                f"**LLM tokens:** input={report['total_llm_input_tokens']}, "
                f"output={report['total_llm_output_tokens']}"
            )
            if report["estimated_cost_usd"] is not None:
                lines.append(f"**Geschatte kosten:** ${report['estimated_cost_usd']:.6f}")
        lines.append("")

    # Side-by-side model matrix.
    if len(reports) > 1:
        lines.append("## Model-matrix (shootout)")
        lines.append("")
        lines.append(
            "| Model | Beschikbaar | Precision | Recall | Hallucinatie | Embedding p50/p95 | "
            "LLM p50/p95 | Escalatie | Kosten ($) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for report in reports:
            fm = report["full_metrics"]
            el = report["embedding_latency_ms"]
            ll = report["llm_latency_ms"]
            avail = "ja" if report["available"] else "nee"
            precision = f"{fm['precision']:.2f}" if fm.get("judged") else "n/a"
            recall = f"{fm['recall']:.2f}" if fm.get("judged") else "n/a"
            hall = f"{fm['hallucination_rate']:.2f}" if fm.get("judged") else "n/a"
            emb = f"{el['p50']:.0f}/{el['p95']:.0f} ms" if el.get("n") else "n/a"
            llm = f"{ll['p50']:.0f}/{ll['p95']:.0f} ms" if ll.get("n") else "n/a"
            cost = f"${report['estimated_cost_usd']:.6f}" if report["estimated_cost_usd"] is not None else "n/a"
            esc = f"{report['escalation_rate'] * 100:.1f}%"
            lines.append(
                f"| {report['model']} | {avail} | {precision} | {recall} | {hall} | {emb} | {llm} | {esc} | {cost} |"
            )
        lines.append("")

    lines.append("## Waar voegt de LLM waarde toe?")
    lines.append("")
    if len(reports) == 1:
        report = reports[0]
        lines.append(
            f"- **Fast-path dekking:** {(1 - report['escalation_rate']) * 100:.1f}% van de utterances "
            "wordt lokaal (deterministisch + embedding) afgehandeld, zonder cloud-kosten of -latency."
        )
        rate = report['escalation_rate'] * 100
        lines.append(
            f"- **LLM escalatie:** {rate:.1f}% van de utterances zit in de onzekere middenband."
        )
        if report["available"]:
            lines.append(
                "- De LLM-laag voegt waarde toe door deze middenband te beoordelen. "
                "Vergelijk de fast-path metrics met de full-cascade metrics om het effect te zien."
            )
        else:
            lines.append(
                "- De LLM-laag was niet beschikbaar in deze run; de middenband is gemarkeerd als 'llm_unavailable'."
            )
    else:
        available = [r for r in reports if r["available"]]
        if available:
            def _llm_p50(r: dict) -> float:
                return r["llm_latency_ms"]["p50"] if r["llm_latency_ms"]["n"] else float("inf")

            def _cost(r: dict) -> float:
                return r["estimated_cost_usd"] if r["estimated_cost_usd"] is not None else float("inf")

            fastest = min(available, key=_llm_p50)
            cheapest = min(available, key=_cost)
            lines.append(
                f"- **Snelste LLM:** {fastest['model']} ({_format_latency(fastest['llm_latency_ms'])})."
            )
            lines.append(
                f"- **Goedkoopste LLM:** {cheapest['model']} (${cheapest['estimated_cost_usd']:.6f})."
            )
        local = [r for r in reports if r["provider"] == "ollama"]
        if local:
            lines.append(
                f"- **Lokaal (Ollama) optie:** {local[0]['model']} is gratis, maar meestal langzamer dan cloud."
            )
        lines.append(
            "- **Aanbeveling:** kies een lokaal model voor privacy-gevoelige/low-volume scenario's; "
            "kies BYO-cloud (Vertex/Azure in eigen tenant) voor lagere latency en betere kwaliteit op de middenband."
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# NDJSON serialization
# ---------------------------------------------------------------------------


def records_to_ndjson(records: list[LayerRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(_record_to_dict(rec), ensure_ascii=False) + "\n")


def _record_to_dict(rec: LayerRecord) -> dict[str, Any]:
    return {
        "text": rec.text,
        "source": rec.source,
        "ground_truth": rec.ground_truth,
        "layer": rec.layer,
        "predicted_category": rec.predicted_category,
        "predicted_confidence": rec.predicted_confidence,
        "embedding_latency_ms": rec.embedding_latency_ms,
        "llm_latency_ms": rec.llm_latency_ms,
        "llm_provider": rec.llm_provider,
        "llm_model": rec.llm_model,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "total_tokens": rec.total_tokens,
        "error": rec.error,
    }
