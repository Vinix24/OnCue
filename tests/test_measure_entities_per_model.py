"""Tests for scripts/measure_entities_per_model.py on synthetic data only.

Fictional companies, a fake transcriber and a tiny normalization config: no
customer names, audio or conversation content.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import measure_entities_per_model as m
from scripts import measure_transcript_normalization as base

TUNING = """\
## acmecorp

- [x] **Akme**  (naam)  `X . . .`  -> **ENTITEIT**: Acme
      fout; klant heet Acme
      > ...we werken samen met Akme voor de software...
- [x] **Bolt**  (naam)  `X . . .`  -> **ENTITEIT**: Bolt
      goed; klantnaam correct
      > ...geleverd door Bolt en dat gaat goed...
- [x] **Zork**  (naam)  `X . . .`  -> **ENTITEIT**: Zorg
      fout; moet Zorg zijn
      > ...de afdeling Zork is nieuw...

## nolisten

- [x] **Quix**  (naam)  `X . . .`  -> **ENTITEIT**: Quick
      fout; moet Quick zijn
      > ...met Quix gesproken...
"""

LISTENING = """\
# Luisterlijst

### acmecorp   [10:00–14:00]
  [10:30]  'Akme'
          ...samen met Akme...
  [12:00]  'Bolt'
          ...door Bolt...
  [13:30]  'Zork'
          ...afdeling Zork...

### nolisten   [01:00–05:00]
  [01:30]  'Quix'
          ...met Quix...
"""

# Layer: Akme -> Acme (variant), and a harmful rule Bolt -> Boot.
NORMALIZATION_YAML = """\
enabled: true
terms: []
variants:
  Akme: Acme
  Bolt: Boot
"""


def _lists(tmp_path: Path):
    path = tmp_path / "norm.yaml"
    path.write_text(NORMALIZATION_YAML, encoding="utf-8")
    return m.load_normalization_lists(path)


def _normalize_fn(tmp_path: Path):
    lists = _lists(tmp_path)

    def fn(text: str):
        new_text, reps = m.normalize(text, lists)
        return new_text, [(a, b) for a, b, _ in reps]

    return fn


def _fixtures(tmp_path: Path):
    tuning = tmp_path / "tuning.md"
    tuning.write_text(TUNING, encoding="utf-8")
    listening = tmp_path / "listening.md"
    listening.write_text(LISTENING, encoding="utf-8")
    cases = base.parse_tuning_set(tuning)
    fragments = {m._key(f.name): f for f in m.parse_listening_list(listening)}
    return cases, fragments


class TestParsing:
    def test_clock_formats(self):
        assert m.parse_clock("37:29") == 37 * 60 + 29
        assert m.parse_clock("1:02:03") == 3723

    def test_listening_list(self, tmp_path):
        _, fragments = _fixtures(tmp_path)
        frag = fragments["acmecorp"]
        assert (frag.start_s, frag.end_s) == (600, 840)
        assert frag.mentions == ((630, "Akme"), (720, "Bolt"), (810, "Zork"))

    def test_timestamped_transcript(self):
        chunks = m.parse_timestamped_transcript("[00:00] eerste\n[00:30] tweede\n[01:05] derde")
        assert chunks == [(0, "eerste"), (30, "tweede"), (65, "derde")]

    def test_continuation_lines_stay_with_their_chunk(self):
        chunks = m.parse_timestamped_transcript("[00:00] eerste\nvervolg\n\n[00:30] tweede")
        assert chunks == [(0, "eerste vervolg"), (30, "tweede")]


class TestWindowAndMatching:
    def test_window_picks_overlapping_chunks(self):
        chunks = [(0, "a"), (30, "b"), (60, "c"), (90, "d"), (120, "e")]
        assert m.window_text(chunks, 75, span_s=10) == "c"
        assert m.window_text(chunks, 60, span_s=30) == "b c"

    @pytest.mark.parametrize(
        ("text", "target", "expected"),
        [
            ("samen met Acme nu", "Acme", True),
            ("samen met acme nu", "Acme", True),
            ("Acmeco is anders", "Acme", False),
            ("de hele shebang dus", "hele shebang", True),
            ("kost 12,5K per jaar", "12,5K", True),
            ("niks", "", False),
        ],
    )
    def test_target_present(self, text, target, expected):
        assert m.target_present(text, target) is expected


class TestFindAudio:
    def test_unique_prefix_and_nested(self, tmp_path):
        for rel in ("acmecorp-2026-01-01", "multi/intro-2026-02-02", "multi/demo-2026-03-03"):
            d = tmp_path / rel
            d.mkdir(parents=True)
            (d / "audio.mp3").write_bytes(b"x")
        assert m.find_audio("acmecorp", tmp_path) == tmp_path / "acmecorp-2026-01-01" / "audio.mp3"
        assert m.find_audio("multi-demo", tmp_path) == tmp_path / "multi/demo-2026-03-03" / "audio.mp3"
        assert m.find_audio("multi", tmp_path) is None  # ambiguous
        assert m.find_audio("absent", tmp_path) is None


class TestScoreVariant:
    def test_before_after_broken_and_unscored(self, tmp_path):
        cases, fragments = _fixtures(tmp_path)
        transcripts = {
            "acmecorp": (
                "[00:00] vooraf\n"
                "[00:30] we werken samen met Akme voor de software\n"
                "[02:00] geleverd door Bolt en dat gaat goed\n"
                "[03:30] de afdeling Zorg is nieuw"
            )
        }
        score = m.score_variant(
            m.VARIANTS["turbo-voc"], cases, fragments, transcripts, _normalize_fn(tmp_path), seconds=12.0
        )
        assert (score.cases_total, score.cases_scored, score.cases_unscored) == (4, 3, 1)
        # Akme: wrong before, repaired by layer. Bolt: right before, broken by layer.
        # Zork: ASR already wrote Zorg, layer leaves it.
        assert score.before_hits == 2
        assert score.after_hits == 2
        assert (score.goed_scored, score.goed_broken) == (1, 1)
        assert (score.fout_scored, score.fout_repaired) == (2, 1)
        assert score.score_before == pytest.approx(2 / 3)
        assert score.replacements_in_fragments == 2

    def test_untimed_case_uses_whole_fragment(self, tmp_path):
        cases, fragments = _fixtures(tmp_path)
        frag = fragments["acmecorp"]
        stripped = m.Fragment(frag.name, frag.start_s, frag.end_s, ())
        transcripts = {"acmecorp": "[00:00] alleen Acme hier\n[03:30] verderop"}
        score = m.score_variant(
            m.VARIANTS["v3-novoc"], cases, {"acmecorp": stripped}, transcripts, _normalize_fn(tmp_path)
        )
        akme = next(o for o in score.outcomes if o.asr_word == "Akme")
        assert akme.scored and akme.present_before and akme.present_after

    def test_no_audio_scores_nothing(self, tmp_path):
        cases, fragments = _fixtures(tmp_path)
        score = m.score_variant(m.VARIANTS["v3-voc"], cases, fragments, {}, _normalize_fn(tmp_path))
        assert score.cases_scored == 0
        assert score.score_before == 0.0 and score.score_after == 0.0


class TestTranscribeVariant:
    def test_caches_raw_text_and_timing(self, tmp_path, monkeypatch):
        _, fragments = _fixtures(tmp_path)
        monkeypatch.setattr(m, "cut_fragment", lambda audio, s, e, dst: dst)
        calls: list[str] = []

        def fake(wav: Path, variant: m.AsrVariant) -> str:
            calls.append(variant.label)
            return "[00:00] tekst"

        audio = {"acmecorp": tmp_path / "audio.mp3"}
        variant = m.VARIANTS["turbo-novoc"]
        first, _ = m.transcribe_variant(variant, fragments, audio, tmp_path, fake)
        second, _ = m.transcribe_variant(variant, fragments, audio, tmp_path, fake)
        assert first == second == {"acmecorp": "[00:00] tekst"}
        assert calls == ["turbo-novoc"]  # second run came from the cache
        assert (tmp_path / "turbo-novoc" / "acmecorp.seconds").exists()


class TestMain:
    def test_end_to_end_prints_only_aggregates(self, tmp_path, monkeypatch, capsys):
        tuning = tmp_path / "tuning.md"
        tuning.write_text(TUNING, encoding="utf-8")
        listening = tmp_path / "listening.md"
        listening.write_text(LISTENING, encoding="utf-8")
        root = tmp_path / "fireflies" / "acmecorp-2026-01-01"
        root.mkdir(parents=True)
        (root / "audio.mp3").write_bytes(b"x")
        norm = tmp_path / "norm.yaml"
        norm.write_text(NORMALIZATION_YAML, encoding="utf-8")

        db = tmp_path / "cases.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE call_sessions (id TEXT, prospect_name TEXT, transcript TEXT)")
        words = " ".join(["woord"] * 60 + ["Akme"])
        conn.execute("INSERT INTO call_sessions VALUES ('s1', '', ?)", (json.dumps([{"text": words}]),))
        conn.commit()
        conn.close()

        monkeypatch.setattr(m, "cut_fragment", lambda audio, s, e, dst: dst)
        out_dir = tmp_path / "out"
        rc = m.main(
            [
                "--tuning-set", str(tuning),
                "--listening-list", str(listening),
                "--held-out-db", str(db),
                "--fireflies-root", str(tmp_path / "fireflies"),
                "--models-dir", str(tmp_path),
                "--out-dir", str(out_dir),
                "--variants", "turbo-voc",
                "--normalization-config", str(norm),
            ],
            transcribe_fn=lambda wav, variant: "[00:30] samen met Akme\n[02:00] door Bolt\n[03:30] Zorg",
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "turbo-voc" in out
        assert "3/4" in out  # scored/total: the call without audio is unscored
        assert "without audio: 1" in out
        assert "Akme" not in out and "Bolt" not in out
        outcomes = json.loads((out_dir / "turbo-voc-outcomes.json").read_text(encoding="utf-8"))
        assert len(outcomes) == 4
