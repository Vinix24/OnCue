#!/usr/bin/env python3
"""Cascade-flow evaluatie-harness (shootout-harness) voor de detector.

Draait elke utterance uit een eval-set door de echte DetectionPipeline en
produceert per-laag metrics plus een side-by-side model-matrix.

Voorbeelden:

    # Default: één model uit .env
    .venv/bin/python scripts/eval_cascade.py --eval-set data/samples/sample-aha.json

    # Model-matrix: lokaal + cloud
    .venv/bin/python scripts/eval_cascade.py \
        --eval-set data/objection_eval/mined.jsonl \
        --models ollama:gemma3:4b,gemini:gemini-2.5-flash

    # Model-register via YAML
    .venv/bin/python scripts/eval_cascade.py \
        --eval-set data/objection_eval/mined.jsonl \
        --models-yaml config/eval_model_register.yaml
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
from sales_copilot.modules.detector.cascade_eval import (  # noqa: E402
    CascadeEvalHooks,
    build_per_model_report,
    build_pipeline_for_spec,
    format_markdown_report,
    load_eval_records,
    records_to_ndjson,
    run_cascade_on_records,
)
from sales_copilot.modules.detector.debouncer import PainPointDebouncer  # noqa: E402
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    ModelSpec,
    PricingTable,
    load_model_register,
    parse_model_specs,
)
from sales_copilot.modules.detector.router import PainPointRouter  # noqa: E402

_DEFAULT_EVAL_SET = REPO_ROOT / "data" / "objection_eval" / "mined.jsonl"
_DEFAULT_OUTPUT = REPO_ROOT / "data" / "cascade_eval" / "run.ndjson"
_DEFAULT_REPORT = REPO_ROOT / "data" / "cascade_eval" / "report.md"
_DEFAULT_PRICES = REPO_ROOT / "config" / "eval_model_prices.yaml"

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=_DEFAULT_EVAL_SET,
        help=f"Path to eval set (JSON/JSONL/MD). Default: {_DEFAULT_EVAL_SET}",
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
        "--max-utterances",
        type=int,
        help="Limit eval to first N utterances (useful for quick smoke tests).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Force LLM layer off (provider=none); measure only deterministic + embedding layers.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    # Load the worktree .env with override so that a local .env always wins over
    # any dotenv file that may have been auto-loaded by a dependency (e.g.
    # semantic_router's find_dotenv walking up to a parent directory).
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    if not args.eval_set.exists():
        logger.error("Eval-set niet gevonden: %s", args.eval_set)
        print(
            f"FATAL: eval set niet gevonden: {args.eval_set}\n"
            "Genereer er een met scripts/mine_objection_eval_set.py --seed "
            "of geef een bestaand transcript/JSONL op.",
            file=sys.stderr,
        )
        return 1

    records = load_eval_records(args.eval_set)
    if not records:
        logger.error("Geen bruikbare utterances gevonden in %s", args.eval_set)
        return 1

    if args.max_utterances:
        records = records[: args.max_utterances]

    config = DetectorConfig.from_env()
    if args.no_llm:
        from dataclasses import replace

        config = replace(config, llm_provider="none", llm_model="none")

    # Deterministic + embedding layers are constant across the matrix.
    router = PainPointRouter(config)
    debouncer = PainPointDebouncer(cooldown_seconds=0)

    # Build model specs: CLI list > YAML register > single spec from config.
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
    hooks = CascadeEvalHooks()

    for spec in specs:
        logger.info("Evalueren van model: %s", spec)
        pipeline, effective_spec, available, reason = build_pipeline_for_spec(
            config, router, debouncer, spec, hooks
        )
        records_out = run_cascade_on_records(
            pipeline, records, config, effective_spec, hooks, max_utterances=None
        )
        report = build_per_model_report(records_out, config, pricing, spec, available, reason)
        all_reports.append(report)
        all_records.extend(records_out)
        logger.info(
            "%s -> escalatie-rate %.1f%%, embedding p50 %.1f ms",
            spec,
            report["escalation_rate"] * 100,
            report["embedding_latency_ms"]["p50"],
        )

    records_to_ndjson(all_records, args.output)
    report_md = format_markdown_report(all_reports, args.eval_set, config)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report_md, encoding="utf-8")

    print(report_md)
    print(f"\nNDJSON audit-log: {args.output}")
    print(f"Markdown rapport: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
