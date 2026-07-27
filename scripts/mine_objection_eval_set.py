#!/usr/bin/env python3
"""Mine objection candidates and near-misses from real transcripts.

Produces a private JSONL eval set in ``data/objection_eval/``. The rows contain
both the predicted objection category/confidence and a human-reviewable label.

Usage with a real transcript:

    .venv/bin/python scripts/mine_objection_eval_set.py \
        --transcript data/fireflies/acme-industries-2026-04-09/fireflies-ca7cd311.md \
        --output data/objection_eval/mined.jsonl

Usage to seed a representative synthetic eval set (useful when no transcripts
are available yet):

    .venv/bin/python scripts/mine_objection_eval_set.py --seed --output data/objection_eval/mined.jsonl

The output file is gitignored by ``data/`` in ``.gitignore`` and must never be
committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.modules.detector.eval_mining import (  # noqa: E402
    classify_records,
    load_records,
    seed_records,
)
from sales_copilot.modules.detector.objection_detector import ObjectionRouter  # noqa: E402

_DEFAULT_OUTPUT = REPO_ROOT / "data" / "objection_eval" / "mined.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript",
        type=Path,
        help="Path to a Fireflies markdown transcript or JSONL transcript.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output JSONL path (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Generate a synthetic seed eval set instead of mining a transcript.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.50,
        help="Confidence threshold used during mining (default: 0.50).",
    )
    parser.add_argument(
        "--include-opportunities",
        action="store_true",
        help="Load real opportunity/readiness routes into the ObjectionRouter.",
    )
    parser.add_argument(
        "--exclude-speakers",
        type=str,
        default=None,
        help=(
            "Comma-separated sales rep names to exclude from prospect mining "
            "(default: Vincent van Deth, Lucas Hendriks, Theun Dingemans)."
        ),
    )
    parser.add_argument(
        "--prospect-name",
        type=str,
        default=None,
        help="If set, only mine utterances from this specific speaker as the prospect.",
    )
    args = parser.parse_args(argv)

    if not args.seed and args.transcript is None:
        parser.error("Pass --transcript PATH or use --seed to generate a seed set.")

    load_env()

    if args.seed:
        records = seed_records()
        source_desc = "synthetic seed"
    else:
        if not args.transcript.exists():
            print(f"FATAL: transcript niet gevonden: {args.transcript}", file=sys.stderr)
            return 1
        exclude_speakers = (
            [name.strip() for name in args.exclude_speakers.split(",") if name.strip()]
            if args.exclude_speakers
            else None
        )
        records = load_records(
            args.transcript,
            exclude_speakers=exclude_speakers,
            prospect_name=args.prospect_name,
        )
        source_desc = args.transcript.name

    if not records:
        print("FATAL: geen bruikbare uitingen gevonden.", file=sys.stderr)
        return 1

    print(f"Classificeren van {len(records)} uitingen uit {source_desc} ...")
    config = DetectorConfig.from_env()
    router = ObjectionRouter(
        config, language="nl", include_opportunities=args.include_opportunities
    )
    enriched = classify_records(records, router, threshold=args.threshold)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in enriched:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    predictions = sum(1 for r in enriched if r["predicted_category"])
    print(f"Wrote {len(enriched)} rows to {args.output} ({predictions} predictions).")
    print("Waarschuwing: deze set is bedoeld voor menselijke review; labels zijn indicatief.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
