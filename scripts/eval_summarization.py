#!/usr/bin/env python3
"""Summarization / action-item shootout (track-1) voor de LLM reframe-laag.

Evalueert de kwaliteit van gestructureerde actiepunten-extractie over een set
transcripties, over meerdere modellen (lokaal + cloud), in hetzelfde model-matrix
format als ``scripts/eval_cascade.py``.

Voorbeelden:

    # Default: één model uit .env
    .venv/bin/python scripts/eval_summarization.py --eval-set data/fireflies

    # Model-matrix: lokaal + cloud
    .venv/bin/python scripts/eval_summarization.py \
        --eval-set data/fireflies \
        --models ollama:gemma3:4b,gemini:gemini-2.5-flash

    # Model-register via YAML
    .venv/bin/python scripts/eval_summarization.py \
        --eval-set data/fireflies \
        --models-yaml config/eval_model_register.yaml

Zonder LLM-key worden modellen gemarkeerd als niet-beschikbaar en wordt de
schema-eval overgeslagen; de matrix blijft wel volledig.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    ModelSpec,
    PricingTable,
    load_model_register,
    parse_model_specs,
)
from sales_copilot.modules.detector.summarization_eval import (  # noqa: E402
    build_per_model_report,
    format_markdown_report,
    load_reference_set,
    load_transcript_records,
    records_to_ndjson,
    run_summarization_eval,
)

_DEFAULT_EVAL_SET = REPO_ROOT / "data" / "fireflies"
_DEFAULT_OUTPUT = REPO_ROOT / "data" / "summarization_eval" / "run.ndjson"
_DEFAULT_REPORT = REPO_ROOT / "data" / "summarization_eval" / "report.md"
_DEFAULT_PRICES = REPO_ROOT / "config" / "eval_model_prices.yaml"

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _merge_references(
    records: list[dict[str, Any]],
    reference_path: Path | None,
) -> list[dict[str, Any]]:
    if reference_path is None or not reference_path.exists():
        return records
    ref_set = load_reference_set(reference_path)
    for rec in records:
        if rec["source"] in ref_set:
            rec["reference"] = ref_set[rec["source"]]
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=_DEFAULT_EVAL_SET,
        help=(
            "Path to eval set: a directory with */transcript_ours.txt files, "
            f"or a single transcript file. Default: {_DEFAULT_EVAL_SET}"
        ),
    )
    parser.add_argument(
        "--reference-set",
        type=Path,
        help="Optional JSON mapping source-name -> ground-truth action items.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"NDJSON output path. Default: {_DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=_DEFAULT_REPORT,
        help=f"Markdown report path. Default: {_DEFAULT_REPORT}",
    )
    parser.add_argument(
        "--models",
        type=str,
        help="Comma-separated model specs, e.g. ollama:gemma3:4b,gemini:gemini-2.5-flash",
    )
    parser.add_argument(
        "--models-yaml",
        type=Path,
        help="YAML model register (list of {provider, model, description})",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=_DEFAULT_PRICES,
        help=f"Pricing table YAML. Default: {_DEFAULT_PRICES}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    if not args.eval_set.exists():
        logger.error("Eval-set niet gevonden: %s", args.eval_set)
        print(
            f"FATAL: eval set niet gevonden: {args.eval_set}\n"
            "Plaats transcripties onder data/fireflies/<call>/transcript_ours.txt "
            "of geef een ander pad op.",
            file=sys.stderr,
        )
        return 1

    records = load_transcript_records(args.eval_set)
    records = _merge_references(records, args.reference_set)
    if not records:
        logger.error("Geen transcripties gevonden in %s", args.eval_set)
        return 1

    config = DetectorConfig.from_env()

    specs: list[ModelSpec]
    if args.models:
        specs = parse_model_specs(args.models)
    elif args.models_yaml:
        specs = load_model_register(args.models_yaml)
    else:
        specs = [ModelSpec(provider=config.llm_provider, model=config.llm_model)]

    pricing = PricingTable.from_path(args.prices)
    all_reports: list[dict[str, Any]] = []
    all_records: list[Any] = []

    for spec in specs:
        logger.info("Evalueren van model: %s", spec)
        out, available, reason = run_summarization_eval(records, config, spec)
        report = build_per_model_report(out, pricing, spec, available, reason)
        all_reports.append(report)
        all_records.extend(out)
        logger.info(
            "%s -> precision %.2f, recall %.2f, schema-compliance %.1f%%",
            spec,
            report["precision"],
            report["recall"],
            report["schema_compliance_rate"] * 100,
        )

    records_to_ndjson(all_records, args.output)
    report_md = format_markdown_report(all_reports, args.eval_set)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")

    print(report_md)
    print(f"\nNDJSON audit-log: {args.output}")
    print(f"Markdown rapport: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
