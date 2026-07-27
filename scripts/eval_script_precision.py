#!/usr/bin/env python3
"""Measure ScriptRouter precision/recall using the negative-class eval pattern
already productionized for the ObjectionRouter.

The ScriptRouter fast-track matcher
(``src/sales_copilot/modules/coaching/script_tracker.py``) drives the
sales-prep live tick-off (Pro feature, see
``claudedocs/2026-07-22-salesprep-pro-design.md`` section 7 finding #5,
section 9 Phase 0). Its keyword matcher returns confidence 0.95 on any
>=2-token overlap with an example phrase and has no negative class — the same
cheap-overlap shape whose ObjectionRouter counterpart measured precision 0.28
(``project_objection_precision_finding.md``). This script reuses the exact
measurement approach already built for that finding
(``scripts/eval_objection_precision.py``): ``EvalRow`` rows with gold labels,
``compute_precision`` / ``compute_per_route_precision`` for the baseline
matcher, and a simulated negative route + ``compute_precision_with_extra_class``
for the structural-fix hypothesis — against
``config/scripts/negatives.yaml``, a near-miss/true-positive eval set modeled
on ``config/negatives.yaml`` but scoped to the 9 script points in
``config/scripts/default.yaml``.

Note on ``compute_precision_with_extra_class``: its ``overall_recall`` field
is computed against ``eval_utils.ALL_OBJECTION_ROUTES``, a constant hardcoded
to the 5 objection route names. That field is not domain-correct for script
points and is reported here for transparency only (raw pass-through, labelled
"objection-domain, not applicable"); this script computes its own
domain-correct post-negative-class precision/recall via the same generic
(route-name-agnostic) ``compute_precision`` helper the baseline uses.

Usage:

    .venv/bin/python scripts/eval_script_precision.py \\
        --threshold 0.50 \\
        --threshold-sweep
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

from sales_copilot.core.config import DetectorConfig, load_env, load_yaml  # noqa: E402
from sales_copilot.modules.coaching.script_tracker import ScriptPoint, ScriptRouter  # noqa: E402
from sales_copilot.modules.detector.eval_utils import (  # noqa: E402
    EvalRow,
    compute_per_route_precision,
    compute_precision,
    compute_precision_with_extra_class,
    compute_threshold_sweep,
    simulate_extra_route,
)
from sales_copilot.modules.detector.router import _normalize_model_name  # noqa: E402

_DEFAULT_SCRIPT_CONFIG = REPO_ROOT / "config" / "scripts" / "default.yaml"
_DEFAULT_NEGATIVES_CONFIG = REPO_ROOT / "config" / "scripts" / "negatives.yaml"
# Decoupled from _DEFAULT_NEGATIVES_CONFIG on purpose: the negative-route
# simulation corpus must not overlap with the eval rows it scores, or a
# negative eval row would trivially self-match its own training utterance.
# config/negatives.yaml is the already-productionized generic non-topic
# Dutch prospect-speech corpus (ObjectionRouter's negative class), which
# applies just as well here since both routers classify the same live
# transcript.
_DEFAULT_NEGATIVE_ROUTE_CONFIG = REPO_ROOT / "config" / "negatives.yaml"

_NEGATIVE_LABEL = "negative"


def _load_script_points(path: Path) -> list[ScriptPoint]:
    """Load script points the same way ``ScriptTracker._load_points`` does."""
    data = load_yaml(path)
    points: list[ScriptPoint] = []
    for entry in data.get("script", []):
        if not isinstance(entry, dict):
            continue
        point_id = entry.get("id")
        if not isinstance(point_id, str) or not point_id:
            continue
        points.append(
            ScriptPoint(
                id=point_id,
                title=str(entry.get("title", point_id)),
                phase=str(entry.get("phase", "discovery")).lower(),
                required=bool(entry.get("required", True)),
                example_phrases=[str(u) for u in entry.get("example_phrases", []) if u],
                keywords=[str(k) for k in entry.get("keywords", []) if k],
            )
        )
    if not points:
        raise ValueError(f"{path} bevat geen scriptpunten (verwacht top-level 'script:' lijst)")
    return points


def _build_eval_rows(points: list[ScriptPoint], negatives_path: Path) -> list[EvalRow]:
    """Build the eval set: per-point true positives + near-miss traps +
    example_phrases (sanity check) + generic negatives, all with gold labels."""
    point_ids = {point.id for point in points}
    data = load_yaml(negatives_path)
    rows: list[EvalRow] = []

    for entry in data.get("points", []):
        point_id = entry.get("id")
        if point_id not in point_ids:
            raise ValueError(
                f"{negatives_path}: point id '{point_id}' komt niet voor in "
                f"{_DEFAULT_SCRIPT_CONFIG.name} — houd beide bestanden in sync"
            )
        for text in entry.get("true_positives", []):
            rows.append(EvalRow(text=str(text), label=point_id, source="negatives.true_positive"))
        for text in entry.get("false_positive_traps", []):
            rows.append(EvalRow(text=str(text), label=_NEGATIVE_LABEL, source="negatives.trap"))

    for text in data.get("generic_negatives", []):
        rows.append(EvalRow(text=str(text), label=_NEGATIVE_LABEL, source="negatives.generic"))

    # Sanity-check rows straight from the shipped example_phrases: these are
    # the router's own training utterances, so recall on them should be ~1.0.
    # A drop here signals the matcher itself broke, not a precision problem.
    for point in points:
        for phrase in point.example_phrases:
            rows.append(EvalRow(text=phrase, label=point.id, source="default.example_phrase"))

    return rows


def _ensure_predictions(rows: list[EvalRow], router: ScriptRouter) -> list[EvalRow]:
    """Re-run every row through the live router so the eval reflects the current matcher."""
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


def _load_negative_route_utterances(path: Path) -> list[str]:
    data = load_yaml(path)
    utterances: list[str] = []
    for route in data.get("routes", []):
        if not isinstance(route, dict):
            continue
        for utterance in route.get("utterances", []):
            if isinstance(utterance, str) and utterance.strip():
                utterances.append(utterance.strip())
    if not utterances:
        raise ValueError(f"{path} bevat geen negative-route utterances")
    return utterances


def _build_single_route_router(name: str, utterances: list[str], embedding_model: str) -> SemanticRouter:
    """Build a temporary router with one simulated route.

    Mirrors ``scripts/eval_objection_precision.py``'s
    ``_build_single_route_router`` so both harnesses measure the negative-class
    hypothesis the same way.
    """
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


def _apply_extra_class(
    rows: list[EvalRow],
    extra_scores: dict[str, float],
    extra_label: str,
    threshold: float,
) -> list[EvalRow]:
    """Return ``rows`` with ``extra_label`` winning wherever it outranks the
    existing prediction and passes ``threshold``.

    Reimplements ``eval_utils._row_with_extra_class`` locally (it is a private
    helper, not part of the module's public surface) so the domain-correct
    precision/recall after the negative-class simulation can be computed with
    the generic, route-name-agnostic ``compute_precision`` — avoiding
    ``compute_precision_with_extra_class``'s ``overall_recall`` field, which is
    scoped to ``eval_utils.ALL_OBJECTION_ROUTES`` and not valid for script
    points.
    """
    modified: list[EvalRow] = []
    for row in rows:
        existing_score = row.predicted_confidence if row.predicted_category else 0.0
        extra_score = extra_scores.get(row.text, 0.0)
        if extra_score >= threshold and extra_score > existing_score:
            modified.append(
                EvalRow(
                    text=row.text,
                    label=row.label,
                    predicted_category=extra_label,
                    predicted_confidence=extra_score,
                    source=row.source,
                    extra=row.extra,
                )
            )
        else:
            modified.append(row)
    return modified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--script-config",
        type=Path,
        default=_DEFAULT_SCRIPT_CONFIG,
        help=f"Path to the script-point definitions (default: {_DEFAULT_SCRIPT_CONFIG}).",
    )
    parser.add_argument(
        "--negatives",
        type=Path,
        default=_DEFAULT_NEGATIVES_CONFIG,
        help=f"Path to the near-miss/true-positive eval set (default: {_DEFAULT_NEGATIVES_CONFIG}).",
    )
    parser.add_argument(
        "--negative-route-config",
        type=Path,
        default=_DEFAULT_NEGATIVE_ROUTE_CONFIG,
        help=(
            "Path to the generic negative-route corpus used for the negative-class "
            f"simulation (default: {_DEFAULT_NEGATIVE_ROUTE_CONFIG})."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.50,
        help=(
            "Confidence threshold for counting a prediction (default: 0.50, matches "
            "DetectorConfig.confidence_threshold_low's dataclass default and "
            "eval_objection_precision.py's default)."
        ),
    )
    parser.add_argument(
        "--threshold-sweep",
        action="store_true",
        help="Run a threshold sweep and report precision/recall per threshold.",
    )
    parser.add_argument("--sweep-start", type=float, default=0.30)
    parser.add_argument("--sweep-end", type=float, default=0.95)
    parser.add_argument("--sweep-step", type=float, default=0.05)
    parser.add_argument(
        "--skip-negative-route-simulation",
        action="store_true",
        help="Skip the simulated negative-route experiment (faster, no extra model load).",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON file to write the report to.")
    args = parser.parse_args(argv)

    if not args.script_config.exists():
        print(f"FATAL: script-config niet gevonden: {args.script_config}", file=sys.stderr)
        return 1
    if not args.negatives.exists():
        print(f"FATAL: negatives-set niet gevonden: {args.negatives}", file=sys.stderr)
        return 1

    load_env()
    config = DetectorConfig.from_env()

    points = _load_script_points(args.script_config)
    rows = _build_eval_rows(points, args.negatives)
    print(f"Geladen: {len(points)} scriptpunten, {len(rows)} eval-rijen uit {args.negatives}\n")

    print("ScriptRouter voorspellingen berekenen...")
    router = ScriptRouter(config, points)
    rows = _ensure_predictions(rows, router)

    point_ids = frozenset(point.id for point in points)
    overall = compute_precision(rows, threshold=args.threshold)
    per_point = compute_per_route_precision(rows, threshold=args.threshold, routes=point_ids)
    false_positive_rows = [
        {
            "text": r.text,
            "predicted": r.predicted_category,
            "gold": r.label,
            "confidence": round(r.predicted_confidence, 3),
        }
        for r in rows
        if r.predicted_category in point_ids and r.predicted_category != r.label
    ]

    report: dict[str, Any] = {
        "threshold": args.threshold,
        "script_config": str(args.script_config),
        "negatives_config": str(args.negatives),
        "embedding_model": config.embedding_model,
        "n_points": len(points),
        "n_rows": len(rows),
        "overall": overall,
        "per_point": per_point,
        "false_positives": false_positive_rows,
    }

    if args.threshold_sweep:
        print("Threshold sweep uitvoeren...")
        report["threshold_sweep"] = compute_threshold_sweep(
            rows, start=args.sweep_start, end=args.sweep_end, step=args.sweep_step
        )

    if not args.skip_negative_route_simulation:
        print(f"Simulated negative route scores berekenen (corpus: {args.negative_route_config})...")
        negative_texts = _load_negative_route_utterances(args.negative_route_config)
        negative_router = _build_single_route_router(_NEGATIVE_LABEL, negative_texts, config.embedding_model)
        negative_scores = _score_with_router([r.text for r in rows], negative_router)

        report["negative_experiment"] = simulate_extra_route(
            rows, negative_scores, _NEGATIVE_LABEL, threshold=args.threshold
        )
        # Raw pass-through of the productionized ObjectionRouter helper, kept
        # for transparency/reuse-compliance. Its 'overall_recall' field is
        # scoped to eval_utils.ALL_OBJECTION_ROUTES (objection domain) and is
        # not applicable to script points — see module docstring.
        report["negative_class_metrics_raw_eval_utils"] = compute_precision_with_extra_class(
            rows, negative_scores, _NEGATIVE_LABEL, threshold=args.threshold
        )
        report["negative_class_metrics_raw_eval_utils"]["overall_recall_caveat"] = (
            "objection-domain (eval_utils.ALL_OBJECTION_ROUTES), not applicable to script "
            "points — see negative_class_metrics_corrected for the domain-correct figure."
        )

        modified_rows = _apply_extra_class(rows, negative_scores, _NEGATIVE_LABEL, args.threshold)
        report["negative_class_metrics_corrected"] = compute_precision(modified_rows, threshold=args.threshold)

    print(json.dumps(report, indent=2, ensure_ascii=False))

    print("\n=== Samenvatting ===")
    print(
        f"Aggregate precision: {overall['precision']:.3f} "
        f"({overall['true_positives']} TP / {overall['false_positives']} FP / "
        f"{overall['predictions']} predictions)"
    )
    print(f"Aggregate recall:    {overall['recall']:.3f} ({overall['false_negatives']} FN)")
    print("\nPer-point precision/recall:")
    for point_id in sorted(per_point):
        metrics = per_point[point_id]
        print(
            f"  {point_id:<20} precision={metrics['precision']:.3f}  "
            f"recall={metrics['recall']:.3f}  "
            f"TP={metrics['true_positives']} FP={metrics['false_positives']} FN={metrics['false_negatives']}"
        )
    if false_positive_rows:
        print(f"\nFalse positives ({len(false_positive_rows)}):")
        for fp in false_positive_rows:
            print(f"  predicted={fp['predicted']:<20} gold={fp['gold']:<20} conf={fp['confidence']}  {fp['text']!r}")

    if not args.skip_negative_route_simulation:
        corrected = report["negative_class_metrics_corrected"]
        print(
            "\nAfter hypothetical negative-class fix (structural, not shipped): "
            f"precision={corrected['precision']:.3f}  recall={corrected['recall']:.3f}  "
            f"FP prevented={report['negative_experiment']['prevented_false_positives']}"
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\nReport written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
