#!/usr/bin/env python3
"""Render a vocabulary file to the exact Whisper prompt and check its token budget.

Usage: python scripts/render_vocabulary.py <vocabulary.yaml>

Prints the prompt string the backends send, its token count (multilingual GPT-2
BPE, offline) and the margin to Whisper's 223-token prompt budget. Exits 1 when
the file is over budget, naming the trailing terms past the budget; exits 2 when
the file cannot be read or the tokenizer is missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sales_copilot.modules.transcriber.vocabulary import (
    TokenizerUnavailableError,
    check_budget,
    load_vocabulary_config,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", type=Path, help="vocabulary YAML file")
    args = parser.parse_args(argv)

    if not args.file.is_file():
        print(f"error: vocabulary file not found: {args.file}", file=sys.stderr)
        return 2
    config = load_vocabulary_config(args.file)
    if not config.enabled or not config.terms:
        print(f"error: {args.file} is disabled or has no terms; nothing to render", file=sys.stderr)
        return 2
    try:
        report = check_budget(config)
    except TokenizerUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(config.initial_prompt)
    print(f"terms: {len(config.terms)}")
    print(f"tokens: {report.tokens} / {report.budget}  margin: {report.margin}")
    if report.over_budget:
        print(f"OVER BUDGET: {report.describe()}", file=sys.stderr)
        return 1
    print("OK: within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
