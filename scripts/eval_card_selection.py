#!/usr/bin/env python3
"""Shootout-evaluatie voor kaart-/slide-selectie (Track 3).

Vergelijkt de bestaande embedding-router + case-DB baseline met een
constrained LLM-selector die uit de bestaande kaart-IDs mag kiezen.

Voorbeelden:

    # Default: baseline + één model uit .env
    .venv/bin/python scripts/eval_card_selection.py

    # Model-matrix: lokaal + cloud
    .venv/bin/python scripts/eval_card_selection.py \
        --models ollama:gemma3:4b,gemini:gemini-2.5-flash

    # Model-register via YAML
    .venv/bin/python scripts/eval_card_selection.py \
        --models-yaml config/eval_model_register.yaml

Zonder LLM-key wordt alleen de embedding-router-baseline gemeten;
de LLM-rijen komen in het rapport als 'niet beschikbaar'.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.modules.detector.card_selection_eval import (  # noqa: E402
    DEFAULT_CASES_PATH,
    DEFAULT_EVAL_SET_PATH,
    DEFAULT_PRICES_PATH,
    build_per_model_report,
    format_markdown_report,
    load_cases,
    load_eval_records,
    records_to_ndjson,
    run_embedding_baseline,
    run_llm_selector,
)
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    ModelSpec,
    PricingTable,
)
from sales_copilot.modules.detector.router import PainPointRouter  # noqa: E402
from sales_copilot.modules.slides.case_db import SQLiteCaseDB  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT = REPO_ROOT / "data" / "card_selection_eval" / "run.ndjson"
_DEFAULT_REPORT = REPO_ROOT / "data" / "card_selection_eval" / "report.md"


def _parse_model_specs(raw: str | None) -> list[ModelSpec] | None:
    if not raw:
        return None
    specs: list[ModelSpec] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Model specificatie moet 'provider:model' zijn: {part!r}")
        provider, model = part.split(":", 1)
        specs.append(ModelSpec(provider=provider.strip(), model=model.strip()))
    return specs


def _load_model_register(path: Path) -> list[ModelSpec]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    specs: list[ModelSpec] = []
    for entry in data.get("models", []):
        specs.append(
            ModelSpec(
                provider=str(entry["provider"]),
                model=str(entry["model"]),
                description=str(entry.get("description", "")),
            )
        )
    return specs


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=DEFAULT_EVAL_SET_PATH,
        help=f"Path to eval set (JSONL). Default: {DEFAULT_EVAL_SET_PATH}",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help=f"Path to case fixture JSON. Default: {DEFAULT_CASES_PATH}",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to seed the SQLite case DB. Default: data/card_selection_eval/cases.db",
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
        default=DEFAULT_PRICES_PATH,
        help=f"Pricing table YAML. Default: {DEFAULT_PRICES_PATH}",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        help="Limit eval to first N records (useful for quick smoke tests).",
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
        print(f"FATAL: eval set niet gevonden: {args.eval_set}", file=sys.stderr)
        return 1
    if not args.cases.exists():
        logger.error("Case-fixture niet gevonden: %s", args.cases)
        print(f"FATAL: case fixture niet gevonden: {args.cases}", file=sys.stderr)
        return 1

    records = load_eval_records(args.eval_set)
    if not records:
        logger.error("Geen records gevonden in %s", args.eval_set)
        return 1
    if args.max_records:
        records = records[: args.max_records]

    cases = load_cases(args.cases)
    db_path = args.db_path or (REPO_ROOT / "data" / "card_selection_eval" / "cases.db")
    case_db = SQLiteCaseDB(db_path)
    await case_db.initialize()
    await case_db.upsert_cases(cases)

    config = DetectorConfig.from_env()
    router = PainPointRouter(config)
    pricing = PricingTable.from_path(args.prices)

    # Build model specs: CLI list > YAML register > single spec from config.
    specs: list[ModelSpec]
    if args.models:
        specs = _parse_model_specs(args.models)
    elif args.models_yaml:
        specs = _load_model_register(args.models_yaml)
    else:
        specs = [ModelSpec(provider=config.llm_provider, model=config.llm_model)]

    all_results: list[Any] = []
    reports: list[dict[str, Any]] = []

    # Always run the embedding-router baseline first.
    logger.info("Evalueren van embedding-router baseline")
    baseline_results = await run_embedding_baseline(records, router, case_db)
    baseline_report = build_per_model_report(
        baseline_results,
        pricing,
        ModelSpec(provider="embedding_router", model="semantic_router"),
        available=True,
        reason="local semantic router + case DB",
    )
    reports.append(baseline_report)
    all_results.extend(baseline_results)
    logger.info(
        "Embedding baseline -> accuracy %.2f%%, p50 %.1f ms",
        baseline_report["accuracy"] * 100 if baseline_report["accuracy"] is not None else 0.0,
        baseline_report["latency_ms"]["p50"],
    )

    for spec in specs:
        logger.info("Evalueren van LLM-selector: %s", spec)
        results, available, reason = await run_llm_selector(
            records, router, case_db, spec, timeout_ms=config.llm_timeout_ms
        )
        report = build_per_model_report(results, pricing, spec, available, reason)
        reports.append(report)
        all_results.extend(results)
        if available:
            logger.info(
                "%s -> accuracy %.2f%%, invalid_id_rate %.2f%%, p50 %.1f ms",
                spec,
                report["accuracy"] * 100 if report["accuracy"] is not None else 0.0,
                report["invalid_id_rate"] * 100,
                report["latency_ms"]["p50"],
            )
        else:
            logger.warning("%s niet beschikbaar: %s", spec, reason)

    records_to_ndjson(all_results, args.output)
    report_md = format_markdown_report(reports, args.eval_set, args.cases)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")

    print(report_md)
    print(f"\nNDJSON audit-log: {args.output}")
    print(f"Markdown rapport: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
