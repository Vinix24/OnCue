"""Tests for scripts/eval_entity_recall.py.

Covers normalisation, matching, threshold exit codes, and baseline-delta
comparison against synthetic transcript strings. The real audio/pipeline path
is exercised separately (see tests/fixtures/entity_recall_demo/ and the
dispatch report) -- these tests stub ``transcribe_via_pipeline`` so they run
without a whisper.cpp/mlx-whisper backend installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import eval_entity_recall as ear

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Twaalfduizend euro", "12000 euro"),
        ("12.000 euro", "12000 euro"),
        ("driehonderdvijfenzestig", "365"),
        ("een euro", "1 euro"),
        ("EEN EURO!!", "1 euro"),
    ],
)
def test_normalize_dutch_number_words_match_digits(raw: str, expected: str) -> None:
    assert ear.normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["06 12 34 56 78", "06-12-34-56-78", "0612345678", "06 1234 5678"],
)
def test_normalize_phone_spacing_variants_all_equal(raw: str) -> None:
    assert ear.normalize(raw) == "0612345678"


def test_normalize_spoken_digit_by_digit_phone_number() -> None:
    spoken = "nul zes twaalf vierendertig zesenvijftig achtenzeventig"
    assert ear.normalize(spoken) == "0612345678"


def test_normalize_strips_case_and_punctuation() -> None:
    assert ear.normalize("Acme Industries!!") == ear.normalize("acme industries")


def test_normalize_collapses_whitespace() -> None:
    assert ear.normalize("Bram   de   Wit") == "bram de wit"


def test_parse_dutch_number_word_rejects_non_numbers() -> None:
    assert ear._parse_dutch_number_word("voorstel") is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Ground truth loading
# ---------------------------------------------------------------------------


def test_load_ground_truth_parses_entities(tmp_path: Path) -> None:
    path = tmp_path / "truth.yaml"
    path.write_text(
        """
entities:
  - type: person
    value: "Bram de Wit"
    critical: true
  - type: amount
    value: "12.000 euro"
    variants: ["twaalfduizend euro"]
    critical: false
""",
        encoding="utf-8",
    )
    entities = ear.load_ground_truth(path)
    assert entities == [
        ear.Entity(type="person", value="Bram de Wit", variants=(), critical=True),
        ear.Entity(type="amount", value="12.000 euro", variants=("twaalfduizend euro",), critical=False),
    ]


def test_load_ground_truth_defaults_critical_to_true(tmp_path: Path) -> None:
    path = tmp_path / "truth.yaml"
    path.write_text('entities:\n  - type: date\n    value: "9 september"\n', encoding="utf-8")
    entities = ear.load_ground_truth(path)
    assert entities[0].critical is True


def test_load_ground_truth_rejects_unknown_type(tmp_path: Path) -> None:
    path = tmp_path / "truth.yaml"
    path.write_text('entities:\n  - type: nonsense\n    value: "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid type"):
        ear.load_ground_truth(path)


def test_load_ground_truth_rejects_missing_value(tmp_path: Path) -> None:
    path = tmp_path / "truth.yaml"
    path.write_text('entities:\n  - type: person\n    value: ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing a non-empty"):
        ear.load_ground_truth(path)


def test_load_ground_truth_empty_file_returns_no_entities(tmp_path: Path) -> None:
    path = tmp_path / "truth.yaml"
    path.write_text("", encoding="utf-8")
    assert ear.load_ground_truth(path) == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_exact_value_scores_as_hit() -> None:
    entity = ear.Entity(type="company", value="Acme Industries")
    results = ear.match_entities([entity], "Wij werken samen met Acme Industries sinds vorig jaar.")
    assert results[0].found is True
    assert results[0].matched_form == "Acme Industries"


def test_accepted_variant_scores_as_hit() -> None:
    """A variant that the value alone would NOT match must still register as a hit."""
    entity = ear.Entity(type="person", value="Alexander", variants=("Xander",))
    results = ear.match_entities([entity], "Even Xander aan de lijn, goedemiddag.")
    assert results[0].found is True
    assert results[0].matched_form == "Xander"


def test_wrong_surname_one_character_off_does_not_score_as_hit() -> None:
    """Hard requirement: exact match only, no fuzzy tolerance in the pass count."""
    entity = ear.Entity(type="person", value="Janssen")
    results = ear.match_entities([entity], "Met Jansen aan de lijn, goedemiddag.")
    assert results[0].found is False
    # Surfaced as a near-miss (informational, "what did it hear instead") but
    # never counted as a hit -- see the assertion above.
    assert results[0].context_similarity is not None
    assert results[0].context_similarity > 0.8


def test_substring_match_respects_word_boundaries() -> None:
    """"jan" must not match inside "januari"."""
    entity = ear.Entity(type="person", value="Jan")
    results = ear.match_entities([entity], "De afspraak staat gepland in januari.")
    assert results[0].found is False


def test_missing_entity_has_no_context_when_transcript_empty() -> None:
    entity = ear.Entity(type="phone", value="0612345678")
    results = ear.match_entities([entity], "")
    assert results[0].found is False
    assert results[0].context_text is None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_summarize_breaks_down_by_type_and_criticality() -> None:
    results = [
        ear.EntityResult(type="person", value="A", critical=True, found=True),
        ear.EntityResult(type="person", value="B", critical=False, found=False),
        ear.EntityResult(type="phone", value="C", critical=True, found=True),
    ]
    summary = ear.summarize(results)
    assert summary == {
        "total": 3,
        "hits": 2,
        "by_type": {"person": {"total": 2, "hits": 1}, "phone": {"total": 1, "hits": 1}},
        "critical_total": 2,
        "critical_hits": 2,
    }


# ---------------------------------------------------------------------------
# Threshold exit code (via main(), transcription stubbed)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ground_truth_file(tmp_path: Path) -> Path:
    path = tmp_path / "truth.yaml"
    path.write_text(
        """
entities:
  - type: person
    value: "Bram de Wit"
  - type: phone
    value: "0612345678"
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "call.wav"
    path.write_bytes(b"not-real-audio")  # content is irrelevant; transcription is stubbed below
    return path


def test_main_exits_zero_when_threshold_met(
    monkeypatch: pytest.MonkeyPatch,
    ground_truth_file: Path,
    audio_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Hier is Bram de Wit, bel 0612345678.")
    exit_code = ear.main([str(audio_file), "-g", str(ground_truth_file), "--threshold", "2"])
    assert exit_code == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_main_exits_nonzero_when_threshold_not_met(
    monkeypatch: pytest.MonkeyPatch,
    ground_truth_file: Path,
    audio_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Hier is Bram de Wit.")
    exit_code = ear.main([str(audio_file), "-g", str(ground_truth_file), "--threshold", "2"])
    assert exit_code == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_main_exits_zero_without_threshold_even_on_misses(
    monkeypatch: pytest.MonkeyPatch, ground_truth_file: Path, audio_file: Path
) -> None:
    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Niets herkenbaars hier.")
    exit_code = ear.main([str(audio_file), "-g", str(ground_truth_file)])
    assert exit_code == 0


def test_main_reports_fatal_on_missing_ground_truth(audio_file: Path, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    exit_code = ear.main([str(audio_file), "-g", str(missing)])
    assert exit_code == 1


# ---------------------------------------------------------------------------
# Baseline-delta comparison
# ---------------------------------------------------------------------------


def test_render_baseline_delta_reports_regression() -> None:
    baseline = {
        "audio_path": "clean.wav",
        "summary": {"hits": 2, "total": 2},
        "entities": [
            {"type": "person", "value": "Bram de Wit", "found": True},
            {"type": "phone", "value": "0612345678", "found": True},
        ],
    }
    degraded_results = [
        ear.EntityResult(type="person", value="Bram de Wit", critical=True, found=True),
        ear.EntityResult(type="phone", value="0612345678", critical=True, found=False),
    ]
    text = "\n".join(ear.render_baseline_delta(degraded_results, baseline))
    assert "delta: -1 entities" in text
    assert "regressed (1): phone:'0612345678'" in text


def test_render_baseline_delta_reports_improvement() -> None:
    baseline = {
        "summary": {"hits": 1, "total": 2},
        "entities": [
            {"type": "person", "value": "Bram de Wit", "found": True},
            {"type": "phone", "value": "0612345678", "found": False},
        ],
    }
    improved_results = [
        ear.EntityResult(type="person", value="Bram de Wit", critical=True, found=True),
        ear.EntityResult(type="phone", value="0612345678", critical=True, found=True),
    ]
    text = "\n".join(ear.render_baseline_delta(improved_results, baseline))
    assert "delta: +1 entities" in text
    assert "improved (1): phone:'0612345678'" in text


def test_render_baseline_delta_warns_on_entity_not_in_baseline() -> None:
    baseline = {"summary": {"hits": 0, "total": 0}, "entities": []}
    results = [ear.EntityResult(type="person", value="Bram de Wit", critical=True, found=True)]
    text = "\n".join(ear.render_baseline_delta(results, baseline))
    assert "WARNING" in text
    assert "person:'Bram de Wit'" in text


def test_main_writes_output_report_reloadable_as_baseline(
    monkeypatch: pytest.MonkeyPatch, ground_truth_file: Path, audio_file: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Hier is Bram de Wit, bel 0612345678.")
    output_path = tmp_path / "report.json"
    exit_code = ear.main([str(audio_file), "-g", str(ground_truth_file), "-o", str(output_path)])
    assert exit_code == 0
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["summary"]["hits"] == 2

    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Hier is Bram de Wit.")
    exit_code2 = ear.main([str(audio_file), "-g", str(ground_truth_file), "--baseline", str(output_path)])
    assert exit_code2 == 0


def test_main_fatal_on_missing_baseline_file(
    monkeypatch: pytest.MonkeyPatch, ground_truth_file: Path, audio_file: Path, tmp_path: Path
) -> None:
    monkeypatch.setattr(ear, "transcribe_via_pipeline", lambda _path: "Hier is Bram de Wit.")
    missing_baseline = tmp_path / "no-such-report.json"
    exit_code = ear.main([str(audio_file), "-g", str(ground_truth_file), "--baseline", str(missing_baseline)])
    assert exit_code == 1
