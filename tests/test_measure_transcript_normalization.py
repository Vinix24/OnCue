"""Tests for scripts/measure_transcript_normalization.py.

Uses a synthetic tuning-set markdown (same structure as the real
beoordelingslijst, fictional companies) and a synthetic sqlite held-out
corpus -- no real transcript content or customer names, per the dispatch
constraint that only counts (never transcript sentences or client names)
may appear anywhere this harness touches, including its own tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from scripts import measure_transcript_normalization as harness

SYNTHETIC_TUNING_SET = """\
# Beoordelingslijst — synthetic test fixture

## acmecorp

- [x] **Akme**  (naam)  `X . . .`  -> **ENTITEIT**: Acme
      fout; klant heet Acme
      > ...we werken al drie jaar samen met Akme voor de software...
- [x] **Bolt**  (naam)  `X . . .`  -> **ENTITEIT**: Bolt
      goed; klantnaam correct geschreven
      > ...de hardware-tak wordt geleverd door Bolt en dat gaat goed...

## zentron

- [x] **Zentron**  (naam)  `X . . .`  -> **ENTITEIT**: Zentron
      goed; klantnaam correct geschreven
      > ...het bedrijf heet Zentron en zit in Utrecht sinds vorig jaar...
- [-] **Ruis**  (naam)  `. . . .`  -> **RUIS**: ruis
      gewoon woord, geen entiteit
      > ...dat is gewoon ruis in deze tekst en telt niet mee...
"""


def _write_tuning_set(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic-beoordelingslijst.md"
    path.write_text(SYNTHETIC_TUNING_SET, encoding="utf-8")
    return path


def _write_held_out_db(tmp_path: Path, sessions: dict[str, str]) -> Path:
    """Build a synthetic call_sessions table with plain filler transcripts."""
    db_path = tmp_path / "synthetic-cases.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE call_sessions (id TEXT PRIMARY KEY, prospect_name TEXT, transcript TEXT)")
        for session_id, text in sessions.items():
            segments = [{"text": word, "speaker": "self"} for word in text.split()]
            conn.execute(
                "INSERT INTO call_sessions (id, prospect_name, transcript) VALUES (?, ?, ?)",
                (session_id, None, json.dumps(segments)),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


FILLER_SESSION_1 = (
    "We bespraken de planning voor volgende week en de planning zag er goed uit "
    "want iedereen had tijd vrijgemaakt voor de sessie en de uitkomst was positief "
    "genoeg om verder te gaan met de vervolgstappen die we hadden afgesproken"
)
FILLER_SESSION_2 = (
    "De tweede sessie ging over de begroting en de planning voor volgend kwartaal "
    "waarbij iedereen tevreden was met de voortgang die geboekt werd deze maand"
)


def _make_replacing_normalize(source: str, target: str) -> harness.NormalizeFn:
    """A normalize_fn that whole-word-replaces every ``source`` with ``target``."""

    def _normalize(text: str) -> tuple[str, list[tuple[str, str]]]:
        pattern = rf"\b{re.escape(source)}\b"
        matches = re.findall(pattern, text)
        new_text = re.sub(pattern, target, text)
        return new_text, [(source, target) for _ in matches]

    return _normalize


class TestParseTuningSet:
    def test_splits_goed_and_fout_and_skips_non_entities(self, tmp_path: Path) -> None:
        path = _write_tuning_set(tmp_path)
        cases = harness.parse_tuning_set(path)

        # 3 [x] entries parsed; the [-] "Ruis" row is not an entity and is excluded.
        assert len(cases) == 3
        goed = [c for c in cases if c.status == "goed"]
        fout = [c for c in cases if c.status == "fout"]
        assert {c.asr_word for c in goed} == {"Bolt", "Zentron"}
        assert {c.asr_word for c in fout} == {"Akme"}
        assert next(c for c in fout if c.asr_word == "Akme").target == "Acme"

    def test_klant_names_from_tuning_set(self, tmp_path: Path) -> None:
        path = _write_tuning_set(tmp_path)
        assert harness.klant_names_from_tuning_set(path) == {"acmecorp", "zentron"}


class TestHeldOutSelection:
    def test_counts_words_from_json_segments(self, tmp_path: Path) -> None:
        db_path = _write_held_out_db(tmp_path, {"syn-001": FILLER_SESSION_1, "syn-002": FILLER_SESSION_2})
        selection = harness.select_held_out_sessions(
            db_path, min_words_per_session=1, exclude_klanten=set(), exclude_session_ids=set()
        )

        assert selection.excluded_below_floor == 0
        assert selection.excluded_by_klant_match == 0
        assert selection.excluded_manual == 0
        assert len(selection.sessions) == 2
        expected_words = len(FILLER_SESSION_1.split()) + len(FILLER_SESSION_2.split())
        assert selection.total_words == expected_words

    def test_floor_excludes_short_sessions(self, tmp_path: Path) -> None:
        db_path = _write_held_out_db(tmp_path, {"syn-short": "een twee drie"})
        selection = harness.select_held_out_sessions(
            db_path, min_words_per_session=50, exclude_klanten=set(), exclude_session_ids=set()
        )
        assert selection.sessions == []
        assert selection.excluded_below_floor == 1

    def test_manual_exclude_by_session_id(self, tmp_path: Path) -> None:
        db_path = _write_held_out_db(tmp_path, {"syn-001": FILLER_SESSION_1})
        selection = harness.select_held_out_sessions(
            db_path,
            min_words_per_session=1,
            exclude_klanten=set(),
            exclude_session_ids={"syn-001"},
        )
        assert selection.sessions == []
        assert selection.excluded_manual == 1


class TestNoopBaseline:
    """The D1 nulmeting: no-op plug-in must show zero false positives and
    zero broken 'goed' cases, on both synthetic data here and the real
    tuning-set/held-out sources (verified separately in the dispatch report)."""

    def test_noop_normalize_yields_zero_false_positives_and_zero_broken(self, tmp_path: Path) -> None:
        tuning_path = _write_tuning_set(tmp_path)
        db_path = _write_held_out_db(tmp_path, {"syn-001": FILLER_SESSION_1, "syn-002": FILLER_SESSION_2})
        cases = harness.parse_tuning_set(tuning_path)
        selection = harness.select_held_out_sessions(
            db_path, min_words_per_session=1, exclude_klanten=set(), exclude_session_ids=set()
        )

        held_out = harness.measure_held_out(selection, harness.noop_normalize)
        tuning = harness.measure_tuning_set(cases, harness.noop_normalize)

        assert held_out.false_positive_count == 0
        assert tuning.goed_broken == 0
        assert tuning.goed_total == 2
        assert tuning.fout_total == 1
        assert tuning.fout_repaired == 0  # no-op never repairs anything


class TestCountingCatchesRealEffects:
    """Demonstrates the harness counts a false positive and a repaired case
    correctly, and -- forced with a normalize that deliberately breaks one
    good word -- that the broken-good-case counter catches it."""

    def test_counts_false_positive_on_held_out_text(self, tmp_path: Path) -> None:
        db_path = _write_held_out_db(tmp_path, {"syn-001": FILLER_SESSION_1, "syn-002": FILLER_SESSION_2})
        selection = harness.select_held_out_sessions(
            db_path, min_words_per_session=1, exclude_klanten=set(), exclude_session_ids=set()
        )
        # "planning" is ordinary held-out vocabulary, not a known entity --
        # any normalize() that touches it on unannotated text is a false positive.
        mangling_normalize = _make_replacing_normalize("planning", "PLANNINGX")
        expected_hits = len(re.findall(r"\bplanning\b", FILLER_SESSION_1)) + len(
            re.findall(r"\bplanning\b", FILLER_SESSION_2)
        )
        assert expected_hits > 0

        held_out = harness.measure_held_out(selection, mangling_normalize)

        assert held_out.false_positive_count == expected_hits

    def test_counts_repaired_fout_case(self, tmp_path: Path) -> None:
        tuning_path = _write_tuning_set(tmp_path)
        cases = harness.parse_tuning_set(tuning_path)
        fixing_normalize = _make_replacing_normalize("Akme", "Acme")

        tuning = harness.measure_tuning_set(cases, fixing_normalize)

        assert tuning.fout_repaired == 1
        assert tuning.goed_broken == 0  # fixing Akme must not touch the good cases

    def test_deliberately_broken_good_word_is_caught(self, tmp_path: Path) -> None:
        tuning_path = _write_tuning_set(tmp_path)
        cases = harness.parse_tuning_set(tuning_path)
        # Forced regression: a normalize that corrupts the already-correct
        # "Bolt" case. This must show up as exactly one broken "goed" case --
        # the guardrail a real normalization layer has to pass before it ships.
        breaking_normalize = _make_replacing_normalize("Bolt", "Bilt")

        tuning = harness.measure_tuning_set(cases, breaking_normalize)

        assert tuning.goed_broken == 1
        assert tuning.fout_repaired == 0  # Akme is still wrong; nothing fixed it


class TestLoadNormalizeFn:
    def test_defaults_to_noop(self) -> None:
        fn = harness.load_normalize_fn(None)
        assert fn("hallo") == ("hallo", [])

    def test_loads_dotted_module_function(self) -> None:
        fn = harness.load_normalize_fn("scripts.measure_transcript_normalization:noop_normalize")
        assert fn is harness.noop_normalize

    def test_rejects_malformed_spec(self) -> None:
        with pytest.raises(ValueError):
            harness.load_normalize_fn("not-a-valid-spec")
