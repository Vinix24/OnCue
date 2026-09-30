#!/usr/bin/env python3
"""Measure PainPointRouter precision/recall for the keyword fast path and the
embedding path SEPARATELY, and simulate the negative-class structural fix.

Mirrors ``scripts/eval_objection_precision.py``, which measured and fixed the
same class of over-match on ObjectionRouter (precision ~0.28, fixed with a
real negative class rather than threshold tuning). This script extends that
measurement to PainPointRouter, whose ``_keyword_match`` had the same two
structural defects:

1. A single overlapping token (even a long/rare one) could clear the old
   ``score >= 2`` bar on its own and return a hardcoded ``confidence=0.95,
   tier="high"`` -- confirmed live on 2026-09-05: "een maand of vijf, zes"
   classified as pain point ``rapportage`` at 0.95.
2. The keyword-match confidence was a constant, not a measurement, so the
   existing ``confidence_threshold_low``/``_high`` machinery could never
   filter it and the LLM confirm path was never reached.

Both are now fixed directly in ``PainPointRouter._keyword_match`` /
``classify()`` (see ``src/sales_copilot/modules/detector/router.py``): a
single-token overlap can no longer produce a match, confidence scales with
how much of the training utterance's content is actually present, and a
shared negative class (``config/negatives.yaml``, ``INCLUDE_NEGATIVES``) is
now wired into PainPointRouter the same way it already is for ObjectionRouter.

By default this uses the built-in seed eval set
(``eval_mining.pain_point_seed_records()`` -- the canonical pain_points.yaml
utterances plus representative near-miss/negative traps), which requires no
private mined dataset. Pass ``--eval-set`` to use a real mined JSONL instead
(same schema as ``scripts/eval_objection_precision.py``'s eval sets).

Usage:

    .venv/bin/python scripts/eval_pain_point_precision.py --threshold-sweep
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.modules.detector.eval_mining import pain_point_seed_records  # noqa: E402
from sales_copilot.modules.detector.eval_utils import (  # noqa: E402
    EvalRow,
    build_rows_from_records,
    compute_per_route_precision,
    compute_precision,
    compute_threshold_sweep,
)
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch  # noqa: E402

# The 12 canonical pain-point route names (config/pain_points.yaml). Passed to
# compute_per_route_precision explicitly -- eval_utils.ALL_OBJECTION_ROUTES is
# scoped to the 5 objection routes and is not domain-correct here (see
# scripts/eval_script_precision.py for the same caveat on ScriptRouter).
_PAIN_POINT_ROUTE_NAMES = frozenset(
    {
        "offerteproces",
        "capaciteit",
        "cross_sell",
        "lead_generation",
        "kennisontsluiting",
        "klantenservice",
        "handmatig_werk",
        "data_kwaliteit",
        "rapportage",
        "onboarding",
        "compliance",
        "kosten",
    }
)


def _load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _keyword_only_rows(rows: list[EvalRow], router: PainPointRouter) -> list[EvalRow]:
    """Isolate the keyword fast path: call ``_keyword_match`` directly.

    This measures the mechanism on its own, independent of whether the
    embedding fallback or the negative-class filter would also have caught
    the same row -- the question the dispatch asks is "how many keyword
    matches are wrong", not "how many end-to-end classifications are wrong".
    """
    updated: list[EvalRow] = []
    for row in rows:
        cleaned = row.text.strip().lower()
        match: RouteMatch | None = router._keyword_match(cleaned) if cleaned else None  # noqa: SLF001
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


def _embedding_only_rows(rows: list[EvalRow], router: PainPointRouter) -> list[EvalRow]:
    """Isolate the embedding path: score every row through the semantic
    router directly, bypassing the keyword fast path entirely (even for rows
    the keyword matcher would also have caught)."""
    embedding_router = router._ensure_router()  # noqa: SLF001
    updated: list[EvalRow] = []
    for row in rows:
        cleaned = row.text.strip()
        if not cleaned:
            updated.append(
                EvalRow(row.text, row.label, None, 0.0, row.source, row.extra)
            )
            continue
        choice = embedding_router(cleaned)
        if isinstance(choice, list):
            choice = choice[0] if choice else None
        if choice is None or choice.name is None or choice.similarity_score is None:
            updated.append(
                EvalRow(row.text, row.label, None, 0.0, row.source, row.extra)
            )
            continue
        score = float(choice.similarity_score)
        if score < router.config.confidence_threshold_low:
            updated.append(
                EvalRow(row.text, row.label, None, 0.0, row.source, row.extra)
            )
            continue
        updated.append(
            EvalRow(row.text, row.label, choice.name, score, row.source, row.extra)
        )
    return updated


def _end_to_end_rows(rows: list[EvalRow], router: PainPointRouter) -> list[EvalRow]:
    """Re-run every row through the live classify() (keyword -> embedding ->
    negative filter), the same path production traffic takes."""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=None,
        help="Optional path to a private mined JSONL eval set. Defaults to the built-in seed set.",
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
        help="Run a threshold sweep (0.30-0.70 by default) on the end-to-end result.",
    )
    parser.add_argument(
        "--no-negatives",
        dest="include_negatives",
        action="store_false",
        default=True,
        help="Disable the negative class -- use for the BEFORE-fix baseline.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON file to write the report to.",
    )
    args = parser.parse_args(argv)

    load_env()
    config = DetectorConfig()

    if args.eval_set:
        if not args.eval_set.exists():
            print(f"FATAL: eval set niet gevonden: {args.eval_set}", file=sys.stderr)
            return 1
        records = _load_jsonl_records(args.eval_set)
    else:
        records = pain_point_seed_records()
    rows = build_rows_from_records(records)
    if not rows:
        print("FATAL: eval set bevat geen rijen.", file=sys.stderr)
        return 1

    print(f"Geladen eval set: {len(rows)} rijen "
          f"({'--eval-set' if args.eval_set else 'pain_point_seed_records()'})\n")

    router = PainPointRouter(config, include_negatives=args.include_negatives)

    print("Keyword fast path meten (isolated, _keyword_match direct)...")
    keyword_rows = _keyword_only_rows(rows, router)
    print("Embedding path meten (isolated, embedding router direct)...")
    embedding_rows = _embedding_only_rows(rows, router)
    print("End-to-end classify() meten (keyword -> embedding -> negative filter)...\n")
    end_to_end_rows = _end_to_end_rows(rows, router)

    report: dict[str, Any] = {
        "threshold": args.threshold,
        "eval_set": str(args.eval_set) if args.eval_set else "pain_point_seed_records()",
        "include_negatives": router._include_negatives,  # noqa: SLF001
        "negative_categories": sorted(router._negative_categories),  # noqa: SLF001
        "n_rows": len(rows),
        "keyword_path": {
            "overall": compute_precision(keyword_rows, threshold=args.threshold),
            "n_predictions": sum(1 for r in keyword_rows if r.predicted_category is not None),
            "per_route": compute_per_route_precision(
                keyword_rows, threshold=args.threshold, routes=_PAIN_POINT_ROUTE_NAMES
            ),
        },
        "embedding_path": {
            "overall": compute_precision(embedding_rows, threshold=args.threshold),
            "n_predictions": sum(1 for r in embedding_rows if r.predicted_category is not None),
            "per_route": compute_per_route_precision(
                embedding_rows, threshold=args.threshold, routes=_PAIN_POINT_ROUTE_NAMES
            ),
        },
        "end_to_end": {
            "overall": compute_precision(end_to_end_rows, threshold=args.threshold),
            "n_predictions": sum(1 for r in end_to_end_rows if r.predicted_category is not None),
            "per_route": compute_per_route_precision(
                end_to_end_rows, threshold=args.threshold, routes=_PAIN_POINT_ROUTE_NAMES
            ),
        },
    }

    # Explicit regression case named in the dispatch.
    regression_text = "een maand of vijf, zes"
    regression_match = router.classify(regression_text)
    report["regression_case"] = {
        "text": regression_text,
        "predicted_category": regression_match.category if regression_match else None,
        "predicted_confidence": regression_match.confidence if regression_match else 0.0,
        "classified_as_rapportage": bool(regression_match and regression_match.category == "rapportage"),
    }

    if args.threshold_sweep:
        print("Threshold sweep uitvoeren (end-to-end)...")
        report["threshold_sweep"] = compute_threshold_sweep(end_to_end_rows, start=0.30, end=0.95, step=0.05)

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        print(f"\nReport written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
