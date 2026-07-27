#!/usr/bin/env python3
"""Measure ObjectionRouter precision and simulate negative / readiness routes.

Reads a private JSONL eval set (e.g. produced by ``mine_objection_eval_set.py``)
and prints a JSON report. The report includes:

- overall precision at the configured threshold
- per-route precision
- a threshold sweep showing precision/recall across a range of thresholds
- simulated negative-route and readiness/kans-route experiments
- structural-fix metrics that treat the extra routes as real prediction classes

Usage:

    .venv/bin/python scripts/eval_objection_precision.py \
        --eval-set data/objection_eval/mined.jsonl \
        --threshold 0.50 \
        --threshold-sweep

The eval set is gitignored and must never be committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from semantic_router import Route, SemanticRouter
from semantic_router.encoders import HuggingFaceEncoder

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.modules.detector.eval_utils import (  # noqa: E402
    EvalRow,
    build_rows_from_records,
    compute_per_route_precision,
    compute_precision,
    compute_precision_with_extra_class,
    compute_threshold_sweep,
    simulate_extra_route,
)
from sales_copilot.modules.detector.objection_detector import ObjectionRouter  # noqa: E402
from sales_copilot.modules.detector.router import _normalize_model_name  # noqa: E402

_DEFAULT_EVAL_SET = REPO_ROOT / "data" / "objection_eval" / "mined.jsonl"

# Simulated negative route: generic non-objection prospect speech. The hypothesis
# is that a real negative class would catch these and prevent over-matching.
_NEGATIVE_ROUTE_UTTERANCES = [
    "kunt u dat uitleggen",
    "ik begrijp het",
    "wat zijn de volgende stappen",
    "hoe werkt dat precies",
    "vertel me meer over de implementatie",
    "waarom zou ik nu moeten veranderen",
    "we zijn nog aan het verkennen",
    "dat is interessant",
    "kunnen we eerst een gesprek hebben",
    "ik moet dit intern bespreken",
]

# Simulated readiness / kans route: positive buying signals that are currently
# forced into an objection category by the lack of a positive class.
_READINESS_ROUTE_UTTERANCES = [
    "kunnen we een demo inplannen",
    "dit klinkt als een goede oplossing",
    "wanneer kunnen we starten",
    "stuur me een offerte",
    "dit past goed bij onze plannen",
    "we willen dit graag verder bespreken",
    "kun je me een voorstel sturen",
]


def _load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _ensure_predictions(rows: list[EvalRow], router: ObjectionRouter) -> list[EvalRow]:
    """Re-run every row through the live router so the eval reflects the current routes."""
    updated: list[EvalRow] = []
    for row in rows:
        match = router.classify(row.text)
        updated.append(
            EvalRow(
                text=row.text,
                label=row.label,
                predicted_category=match.category if match else None,
                predicted_confidence=match.confidence if match else 0.0,
                source=row.source,
                extra=row.extra,
            )
        )
    return updated


def _build_single_route_router(name: str, utterances: list[str], embedding_model: str) -> SemanticRouter:
    """Build a temporary router with one simulated route."""
    encoder = HuggingFaceEncoder(name=_normalize_model_name(embedding_model))
    route = Route(name=name, utterances=utterances)
    router = SemanticRouter(encoder=encoder, routes=[], aggregation="max")
    router.add([route])
    return router


def _score_with_router(texts: list[str], router: SemanticRouter) -> dict[str, float]:
    """Return the top similarity score for each text using ``router``."""
    scores: dict[str, float] = {}
    for text in texts:
        choice = router(text)
        if isinstance(choice, list):
            choice = choice[0] if choice else None
        scores[text] = float(choice.similarity_score) if choice and choice.similarity_score else 0.0
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=_DEFAULT_EVAL_SET,
        help=f"Path to the private JSONL eval set (default: {_DEFAULT_EVAL_SET}).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.50,
        help="Confidence threshold for counting a prediction (default: 0.50).",
    )
    parser.add_argument(
        "--threshold-sweep",
        action="store_true",
        help="Run a threshold sweep (0.30-0.70 by default) and report precision/recall per threshold.",
    )
    parser.add_argument(
        "--sweep-start",
        type=float,
        default=0.30,
        help="Start of the threshold sweep (default: 0.30).",
    )
    parser.add_argument(
        "--sweep-end",
        type=float,
        default=0.70,
        help="End of the threshold sweep (default: 0.70).",
    )
    parser.add_argument(
        "--sweep-step",
        type=float,
        default=0.05,
        help="Step size of the threshold sweep (default: 0.05).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file to write the report to.",
    )
    parser.add_argument(
        "--skip-extra-routes",
        action="store_true",
        help="Skip the negative and readiness route simulations (faster, no extra model load).",
    )
    parser.add_argument(
        "--include-opportunities",
        action="store_true",
        help="Load real opportunity/readiness routes into the ObjectionRouter (structural fix).",
    )
    parser.add_argument(
        "--include-negatives",
        dest="include_negatives",
        action="store_true",
        default=None,
        help=(
            "Load the real negative/'none' class into the ObjectionRouter "
            "(default: DetectorConfig.include_negatives, true unless overridden)."
        ),
    )
    parser.add_argument(
        "--no-negatives",
        dest="include_negatives",
        action="store_false",
        help="Disable the negative class regardless of config — use for a BEFORE precision baseline.",
    )
    args = parser.parse_args(argv)

    if not args.eval_set.exists():
        print(
            f"FATAL: eval set niet gevonden: {args.eval_set}\n"
            "Genereer hem eerst met scripts/mine_objection_eval_set.py --seed",
            file=sys.stderr,
        )
        return 1

    load_env()
    config = DetectorConfig.from_env()

    records = _load_jsonl_records(args.eval_set)
    rows = build_rows_from_records(records)
    if not rows:
        print("FATAL: eval set bevat geen rijen.", file=sys.stderr)
        return 1

    print(f"Geladen eval set: {len(rows)} rijen uit {args.eval_set}\n")

    # Ensure every row has a prediction from the current ObjectionRouter.
    print("ObjectionRouter voorspellingen berekenen...")
    router = ObjectionRouter(
        config,
        language="nl",
        include_opportunities=args.include_opportunities,
        include_negatives=args.include_negatives,
    )
    rows = _ensure_predictions(rows, router)

    overall = compute_precision(rows, threshold=args.threshold)
    per_route = compute_per_route_precision(rows, threshold=args.threshold)

    report: dict[str, Any] = {
        "threshold": args.threshold,
        "eval_set": str(args.eval_set),
        "include_opportunities": args.include_opportunities,
        "include_negatives": router._include_negatives,
        "negative_categories": sorted(router._negative_categories),
        "opportunity_categories": sorted(router._opportunity_categories),
        "n_rows": len(rows),
        "overall": overall,
        "per_route": per_route,
    }

    if args.threshold_sweep:
        print("Threshold sweep uitvoeren...")
        report["threshold_sweep"] = compute_threshold_sweep(
            rows,
            start=args.sweep_start,
            end=args.sweep_end,
            step=args.sweep_step,
        )

    if not args.skip_extra_routes:
        print("Simulated negative route scores berekenen...")
        negative_router = _build_single_route_router(
            "negative", _NEGATIVE_ROUTE_UTTERANCES, config.embedding_model
        )
        negative_scores = _score_with_router([r.text for r in rows], negative_router)
        report["negative_experiment"] = simulate_extra_route(
            rows, negative_scores, "negative", threshold=args.threshold
        )

        print("Simulated readiness route scores berekenen...")
        readiness_router = _build_single_route_router(
            "kans", _READINESS_ROUTE_UTTERANCES, config.embedding_model
        )
        readiness_scores = _score_with_router([r.text for r in rows], readiness_router)
        report["readiness_experiment"] = simulate_extra_route(
            rows, readiness_scores, "kans", threshold=args.threshold
        )

        # Structural-fix metrics: treat the extra route as a real class and
        # measure precision/recall as if the router could predict it. This
        # directly answers whether an extra class solves over-matching where
        # threshold tuning does not.
        report["negative_class_metrics"] = compute_precision_with_extra_class(
            rows, negative_scores, "negative", threshold=args.threshold
        )
        report["readiness_class_metrics"] = compute_precision_with_extra_class(
            rows, readiness_scores, "kans", threshold=args.threshold
        )

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\nReport written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
