#!/usr/bin/env python3
"""Measurement harness for a post-ASR transcript normalization layer.

This is D1 of the transcript-normalisatie-na-asr track: the harness, built
*before* there is a normalization layer to test. D2 will plug the real
``normalize()`` in; D1 ships only a no-op plug-in and proves the harness
counts correctly on synthetic data.

Two independent, non-overlapping data sources feed the measurement:

- **Tuning-set**: the manually reviewed markdown at
  ``claudedocs/2026-09-25-beoordelingslijst-entiteiten.md`` (103 cases, 50
  real entities: 17 already transcribed correctly ("goed"), 33 transcribed
  wrong ("fout")). This file lives outside the repo tree it is read from
  read-only, by absolute path, and is never copied into the repo.
- **Held-out corpus**: real call transcripts from the ``call_sessions``
  table in ``data/cases.db`` (column ``transcript``, JSON list of
  ``{"text": ..., "speaker": ..., ...}`` segments). These conversations are
  verified to not be among the tuning-set's source calls -- see
  ``select_held_out_sessions`` and the dispatch report for how that was
  checked (the table carries no ``prospect_name``, so the check is a
  content-level spot-check, not a metadata join).

Two plug-in-agnostic measurements:

1. **False positives on the held-out set**: every replacement the plug-in
   makes on real, unannotated conversation text is counted as a
   (potential) false positive, since there is no ground truth there to
   confirm a replacement was warranted. The no-op plug-in makes zero
   replacements by construction, so this is 0 for the D1 baseline.
2. **Broken "goed" cases on the tuning-set**: for each of the 17
   already-correct entity mentions, the plug-in runs over the quoted
   sentence fragment and the harness checks whether the correct word
   survived unchanged. Any that don't count as "broken" -- the guardrail
   the plan requires to be 0 before a real normalization layer ships.

A third measurement (not a D1 acceptance gate, but part of the same
counting machinery, exercised by the tests) is **repaired "fout" cases**:
how many of the 33 known-wrong entity mentions get corrected. For a no-op
plug-in this is always 0.

Only counts are ever printed or written to a report -- never transcript
sentences or customer/client names -- by design, so this script's own
output is safe to paste anywhere.

Usage:
    .venv/bin/python scripts/measure_transcript_normalization.py \\
        --tuning-set /abs/path/to/beoordelingslijst-entiteiten.md \\
        --held-out-db /abs/path/to/cases.db

    # Point at a real normalize() once D2 lands:
    .venv/bin/python scripts/measure_transcript_normalization.py \\
        --tuning-set ... --held-out-db ... \\
        --normalize-fn sales_copilot.modules.transcriber.normalize:normalize
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# (new_text, replacements) -- replacements is a list of (original, replacement)
# pairs. D2's real normalize() additionally takes a ``lists`` argument; wrap
# it in a closure/partial before passing it here as ``--normalize-fn``.
NormalizeFn = Callable[[str], "tuple[str, list[tuple[str, str]]]"]

_GOED = "goed"
_ENTRY_RE = re.compile(
    r"^- \[(?P<mark>[x\- ])\] \*\*(?P<word>.+?)\*\*\s+\((?P<kind>\w+)\)\s+"
    r"`(?P<pattern>.*?)`\s+->\s+\*\*\w+\*\*:\s*(?P<target>.+)$"
)
_SECTION_RE = re.compile(r"^## (?P<klant>\S.*)$")


@dataclass(frozen=True)
class TuningCase:
    """One reviewed entity mention from the beoordelingslijst."""

    klant: str
    asr_word: str
    target: str
    status: str  # "goed" or "fout"
    context: str  # quoted sentence fragment the word occurred in


@dataclass(frozen=True)
class HeldOutSession:
    """One held-out conversation, reduced to its spoken text."""

    session_id: str
    text: str
    word_count: int


@dataclass
class HeldOutSelection:
    sessions: list[HeldOutSession]
    excluded_below_floor: int
    excluded_by_klant_match: int
    excluded_manual: int

    @property
    def total_words(self) -> int:
        return sum(s.word_count for s in self.sessions)


@dataclass
class HeldOutMeasurement:
    session_count: int
    word_count: int
    false_positive_count: int

    @property
    def false_positive_rate_per_10k(self) -> float:
        if self.word_count == 0:
            return 0.0
        return self.false_positive_count / self.word_count * 10_000


@dataclass
class TuningMeasurement:
    goed_total: int
    goed_broken: int
    fout_total: int
    fout_repaired: int


def noop_normalize(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The D1 plug-in: replaces nothing. D2 supplies the real function."""
    return text, []


def load_normalize_fn(spec: str | None) -> NormalizeFn:
    """Load a ``module.path:function`` normalize plug-in, or the no-op default."""
    if not spec:
        return noop_normalize
    module_name, _, func_name = spec.partition(":")
    if not module_name or not func_name:
        raise ValueError(f"--normalize-fn must be 'module.path:function', got {spec!r}")
    module = importlib.import_module(module_name)
    fn = getattr(module, func_name)
    return fn


# ---------------------------------------------------------------------------
# Tuning-set parsing
# ---------------------------------------------------------------------------


def parse_tuning_set(path: Path) -> list[TuningCase]:
    """Parse the beoordelingslijst markdown into entity cases.

    Only `[x]` (confirmed entity) rows are returned. Each entry's status is
    "goed" when its description line starts with "goed" and "fout"
    otherwise -- every non-"goed" `[x]` entry in the reviewed list describes
    some flavour of wrong transcription (verified against the source data:
    50 `[x]` entries split 17 "goed" / 33 everything-else, matching the plan's
    counts exactly).
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    cases: list[TuningCase] = []
    current_klant = ""
    for i, line in enumerate(lines):
        section_match = _SECTION_RE.match(line.strip())
        if section_match:
            current_klant = section_match.group("klant")
            continue
        entry_match = _ENTRY_RE.match(line.strip())
        if not entry_match or entry_match.group("mark") != "x":
            continue
        desc = lines[i + 1].strip() if i + 1 < len(lines) else ""
        quote_line = lines[i + 2].strip() if i + 2 < len(lines) else ""
        context = quote_line[1:].strip() if quote_line.startswith(">") else desc
        status = _GOED if desc.lower().startswith(_GOED) else "fout"
        cases.append(
            TuningCase(
                klant=current_klant,
                asr_word=entry_match.group("word"),
                target=entry_match.group("target").strip(),
                status=status,
                context=context,
            )
        )
    return cases


# ---------------------------------------------------------------------------
# Held-out corpus loading
# ---------------------------------------------------------------------------


def _segments_to_text(raw_transcript: str) -> str:
    """Extract spoken text from a call_sessions.transcript JSON blob.

    The column stores a JSON list of ``{"text": ..., "speaker": ..., ...}``
    segments (see ``TranscriptEvent`` / ``engine.py``). Falls back to the raw
    string for any row that isn't that shape, so malformed/legacy rows don't
    crash the harness.
    """
    try:
        segments = json.loads(raw_transcript)
    except (json.JSONDecodeError, TypeError):
        return raw_transcript
    if not isinstance(segments, list):
        return raw_transcript
    parts = [seg.get("text", "") for seg in segments if isinstance(seg, dict)]
    return " ".join(parts)


def select_held_out_sessions(
    db_path: Path,
    *,
    min_words_per_session: int,
    exclude_klanten: set[str],
    exclude_session_ids: set[str],
) -> HeldOutSelection:
    """Load held-out conversations from call_sessions, held apart from the tuning-set.

    Exclusion runs on two signals:

    - ``prospect_name`` matched case-insensitively against ``exclude_klanten``
      (the tuning-set's klant names). In the current database every row has
      an empty ``prospect_name``, so this is a no-op today -- it exists so a
      future run against a populated table excludes automatically instead of
      relying on a one-off manual check.
    - ``exclude_session_ids``, an explicit override for sessions a human has
      confirmed belong to a tuning-set call by content (see the dispatch
      report for the verification performed for this run).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, prospect_name, transcript FROM call_sessions WHERE transcript IS NOT NULL AND transcript != ''"
        ).fetchall()
    finally:
        conn.close()

    exclude_klanten_lower = {k.lower() for k in exclude_klanten}
    sessions: list[HeldOutSession] = []
    excluded_below_floor = 0
    excluded_by_klant_match = 0
    excluded_manual = 0

    for row in rows:
        session_id = row["id"]
        if session_id in exclude_session_ids:
            excluded_manual += 1
            continue
        prospect_name = (row["prospect_name"] or "").strip().lower()
        if prospect_name and prospect_name in exclude_klanten_lower:
            excluded_by_klant_match += 1
            continue
        text = _segments_to_text(row["transcript"])
        word_count = len(text.split())
        if word_count < min_words_per_session:
            excluded_below_floor += 1
            continue
        sessions.append(HeldOutSession(session_id=session_id, text=text, word_count=word_count))

    return HeldOutSelection(
        sessions=sessions,
        excluded_below_floor=excluded_below_floor,
        excluded_by_klant_match=excluded_by_klant_match,
        excluded_manual=excluded_manual,
    )


def klant_names_from_tuning_set(path: Path) -> set[str]:
    """Return the ``## klant`` section headers from the beoordelingslijst."""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            names.add(match.group("klant"))
    return names


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _count_occurrences(text: str, word: str) -> int:
    if not word:
        return 0
    return len(re.findall(rf"\b{re.escape(word)}\b", text))


def measure_held_out(selection: HeldOutSelection, normalize_fn: NormalizeFn) -> HeldOutMeasurement:
    false_positive_count = 0
    for session in selection.sessions:
        _, replacements = normalize_fn(session.text)
        false_positive_count += len(replacements)
    return HeldOutMeasurement(
        session_count=len(selection.sessions),
        word_count=selection.total_words,
        false_positive_count=false_positive_count,
    )


def measure_tuning_set(cases: list[TuningCase], normalize_fn: NormalizeFn) -> TuningMeasurement:
    goed_cases = [c for c in cases if c.status == _GOED]
    fout_cases = [c for c in cases if c.status == "fout"]

    goed_broken = 0
    for case in goed_cases:
        new_text, _ = normalize_fn(case.context)
        before = _count_occurrences(case.context, case.asr_word)
        after = _count_occurrences(new_text, case.asr_word)
        if after < before:
            goed_broken += 1

    fout_repaired = 0
    for case in fout_cases:
        new_text, _ = normalize_fn(case.context)
        target_before = _count_occurrences(case.context, case.target)
        target_after = _count_occurrences(new_text, case.target)
        asr_before = _count_occurrences(case.context, case.asr_word)
        asr_after = _count_occurrences(new_text, case.asr_word)
        if target_after > target_before and asr_after < asr_before:
            fout_repaired += 1

    return TuningMeasurement(
        goed_total=len(goed_cases),
        goed_broken=goed_broken,
        fout_total=len(fout_cases),
        fout_repaired=fout_repaired,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--tuning-set", required=True, type=Path, help="Path to the beoordelingslijst markdown")
    parser.add_argument("--held-out-db", required=True, type=Path, help="Path to cases.db (call_sessions table)")
    parser.add_argument(
        "--normalize-fn",
        default=None,
        help="'module.path:function' plug-in; defaults to the D1 no-op normalize",
    )
    parser.add_argument(
        "--min-words-per-session",
        type=int,
        default=50,
        help="Skip held-out sessions with fewer spoken words than this (default: 50)",
    )
    parser.add_argument(
        "--exclude-session",
        action="append",
        default=[],
        dest="exclude_sessions",
        help="Held-out session id to exclude explicitly (repeatable)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    tuning_cases = parse_tuning_set(args.tuning_set)
    klant_names = klant_names_from_tuning_set(args.tuning_set)
    selection = select_held_out_sessions(
        args.held_out_db,
        min_words_per_session=args.min_words_per_session,
        exclude_klanten=klant_names,
        exclude_session_ids=set(args.exclude_sessions),
    )
    normalize_fn = load_normalize_fn(args.normalize_fn)

    held_out = measure_held_out(selection, normalize_fn)
    tuning = measure_tuning_set(tuning_cases, normalize_fn)

    print("Held-out corpus")
    print(f"  sessions included:         {held_out.session_count}")
    print(f"  sessions excluded (floor): {selection.excluded_below_floor}")
    print(f"  sessions excluded (klant): {selection.excluded_by_klant_match}")
    print(f"  sessions excluded (manual):{selection.excluded_manual}")
    print(f"  words:                     {held_out.word_count}")
    print(f"  false positives:           {held_out.false_positive_count}")
    print(f"  false positives / 10k words: {held_out.false_positive_rate_per_10k:.3f}")
    print()
    print("Tuning set")
    print(f"  goed cases (total/broken): {tuning.goed_total}/{tuning.goed_broken}")
    print(f"  fout cases (total/repaired): {tuning.fout_total}/{tuning.fout_repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
