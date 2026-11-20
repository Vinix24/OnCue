#!/usr/bin/env python3
"""List vocabulary candidates from a raw transcript and its corrected version.

Usage: python scripts/vocabulary_candidates.py <transcript> <corrected>

Aligns the two texts on word level (case-insensitive) and lists the words or
phrases the correction introduced, ranked by count (ties in order of first
appearance). The corrected spelling is shown with the raw one it replaced.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def candidates(raw: str, corrected: str) -> list[tuple[str, str, int]]:
    """Return ``(corrected phrase, raw phrase, count)`` ranked by count, then first appearance."""
    a, b = _words(raw), _words(corrected)
    matcher = SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b], autojunk=False)
    counts: Counter[str] = Counter()
    shown: dict[str, str] = {}
    raw_for: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("replace", "insert") or j1 == j2:
            continue
        phrase = " ".join(b[j1:j2])
        key = phrase.lower()
        counts[key] += 1
        shown.setdefault(key, phrase)
        raw_for.setdefault(key, " ".join(a[i1:i2]))
    # dicts keep first-appearance order and sorted() is stable, so ties stay in text order.
    ranked = sorted(counts, key=lambda k: -counts[k])
    return [(shown[k], raw_for[k], counts[k]) for k in ranked]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcript", type=Path)
    parser.add_argument("corrected", type=Path)
    args = parser.parse_args(argv)
    for path in (args.transcript, args.corrected):
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
    rows = candidates(
        args.transcript.read_text(encoding="utf-8"),
        args.corrected.read_text(encoding="utf-8"),
    )
    for term, was, count in rows:
        print(f"{count}\t{term}\t(was: {was or '-'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
