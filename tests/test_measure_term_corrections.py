"""Tests for scripts/measure_term_corrections.py: the harness for contextual term correction.

A synthetic fixture only, no network: the report model is replaced by a local coroutine that
answers from a table. Covers the position anchor, the list and candidate validation, the
source check, a name in another segment staying put, the position on the original segment
under the PII strip, and that nothing from the input text ends up in the output. Round 2 adds
the word-loss guard, the free-correction arm with its content-added rule, the model choice
through ``REPORT_TERMS_LLM_MODEL`` and the token count per call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts import measure_term_corrections as mtc

SECRET = "GEHEIMEKLANTNAAM"

#: The route keys of the term task and the report task it inherits from.
_ROUTE_KEYS = (
    "REPORT_LLM_PROVIDER",
    "REPORT_LLM_MODEL",
    "REPORT_LLM_TIMEOUT_MS",
    "REPORT_TERMS_LLM_PROVIDER",
    "REPORT_TERMS_LLM_MODEL",
    "REPORT_TERMS_LLM_TIMEOUT_MS",
)

TERMS = (
    "Teamleader",
    "ROI",
    "Roy",
    "Marten Smit",
    "VBA",
    "EDI",
    "Sentrix",
    "Sofi",
    "Bram",
    "Brightwave",
    "Brite Wave -> Brightwave",
)

GOOD_LIST = """# Beoordelingslijst

## klant-a

- [x] **ROI**  (afkorting)  `X X . .`  -> **ENTITEIT**: ROI
      goed; 'drie keer ROI'
      > ...dat als er niet meer dan drie keer ROI uitkomt, dus geen besparing...
- [x] **Brightwave**  (naam)  `X X X .`  -> **ENTITEIT**: Brightwave
      goed; leverancier
      > ...Dus dat was zo'n chatbot van Brightwave destijds. In AI hebben we...
- [x] **Nederland**  (naam)  `X X X X`  -> **ENTITEIT**: Nederland
      goed
      > ...bestaat uit 110 bedrijven in Nederland. En die nemen...
- [x] **Brightwaif**  (naam)  `. . . X`  -> **ENTITEIT**: Brightwave
      fout
      > ...chatbot van Brightwaif destijds...
- [-] **Bedankt**  (naam)  `. X . .`  -> **RUIS**: bedankt
      gewoon woord
      > ...Bedankt voor je tijd...
"""


def _label(case_id: str, source: str, occurrence: int, expected: str | None, name_like: bool = False) -> dict:
    return {"id": case_id, "source": source, "occurrence": occurrence, "expected": expected, "name_like": name_like}


CASES: dict[str, Any] = {
    "segments": [
        {
            "id": "s1",
            "text": f"Is het via ROI gegaan, vroeg {SECRET} aan de tafel.",
            "labels": [_label("c1", "ROI", 1, "Roy", True)],
        },
        {
            "id": "s2",
            "text": "Wij plannen alles in Team Leader tegenwoordig.",
            "labels": [_label("c2", "Team Leader", 1, "Teamleader")],
        },
        {
            "id": "s3",
            "text": "Het draait bij Zentrix sinds maart.",
            "labels": [_label("c3", "Zentrix", 1, "Sentrix")],
        },
        {
            "id": "s4",
            "text": "Maar Roy, dat is de baas van verkoop.",
            "labels": [_label("k1", "Roy", 1, None, True)],
        },
        {
            "id": "s5",
            "text": "De koppeling met Sophie werkt nu goed.",
            "labels": [_label("c4", "Sophie", 1, "Sofi", True)],
        },
    ]
}


FREE_CASES: dict[str, Any] = {
    "segments": [
        {
            "id": "fs1",
            "text": "hij doet dat als Dobby in het weekend",
            "labels": [{"id": "f1", "source": "Dobby", "occurrence": 1, "expected": "hobby"}],
        },
        {
            "id": "fs2",
            "text": "dan neem je de Hele Sjebang in een keer",
            "labels": [{"id": "f2", "source": "Hele Sjebang", "occurrence": 1, "expected": "hele shebang"}],
        },
        {
            "id": "fs3",
            "text": "we koppelen het aan het KMP van de klant",
            "labels": [{"id": "f3", "source": "KMP", "occurrence": 1, "expected": ["CRM", "CRMP"]}],
        },
    ]
}


@pytest.fixture(autouse=True)
def _cloud_only_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default cloud path: PII stripped before a public provider."""
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)


@pytest.fixture
def plain_normalization(tmp_path: Path) -> Path:
    """An empty fixed list, so only the fixture's own terms drive the deterministic layer."""
    path = tmp_path / "normalization.yaml"
    path.write_text("enabled: true\nterms: []\nvariants: {}\n", encoding="utf-8")
    return path


@pytest.fixture
def cases_file(tmp_path: Path) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(CASES), encoding="utf-8")
    return path


@pytest.fixture
def good_file(tmp_path: Path) -> Path:
    path = tmp_path / "beoordelingslijst.md"
    path.write_text(GOOD_LIST, encoding="utf-8")
    return path


@pytest.fixture
def terms_file(tmp_path: Path) -> Path:
    path = tmp_path / "terms.yaml"
    path.write_text(json.dumps({"termen": list(TERMS)}), encoding="utf-8")
    return path


USAGE = {"input_tokens": 300, "output_tokens": 20, "total_tokens": 320, "cache_read_tokens": 0}


class ScriptedModel:
    """Stands in for the report LLM: answers by the first key found in the request text.

    Like ``LLMClient`` it sets ``last_usage`` after every successful call, and it checks
    that each arm gets its own system prompt and answer shape.
    """

    def __init__(
        self,
        answers: dict[str, list[mtc.TermCorrection]],
        fail_on: tuple[str, ...] = (),
        usage: dict[str, int] | None = None,
    ) -> None:
        self.answers = answers
        self.fail_on = fail_on
        self.usage = USAGE if usage is None else usage
        self.requests: list[str] = []
        self.models: list[str] = []
        self.last_usage: dict[str, int] | None = None

    async def acreate(self, **kwargs: Any) -> Any:
        user_text = kwargs["user_text"]
        mode = "free" if kwargs["response_model"] is mtc.FreeCorrections else "term"
        system_prompt, response_model = mtc._MODE_PROMPTS[mode]
        assert kwargs["system_prompt"] == system_prompt
        assert kwargs["response_model"] is response_model
        self.requests.append(user_text)
        self.models.append(kwargs["model"])
        if any(key in user_text for key in self.fail_on):
            raise RuntimeError("provider unavailable")
        self.last_usage = dict(self.usage)
        for key, corrections in self.answers.items():
            if key in user_text:
                return response_model(corrections=[c.model_dump() for c in corrections])
        return response_model(corrections=[])


def _fix(source: str, occurrence: int, target: str, segment_index: int = 0) -> mtc.TermCorrection:
    return mtc.TermCorrection(
        segment_index=segment_index, source=source, occurrence=occurrence, target=target, reason="context"
    )


def _model_run(llm: ScriptedModel, runs: int = 1, *more: ScriptedModel) -> mtc.ModelRun:
    return mtc.ModelRun(clients=(llm, *more), model="test-model", temperature=0.1, timeout_s=5.0, runs=runs)


def _measure(tmp_path: Path, plain_normalization: Path, model_run: mtc.ModelRun | None, **kwargs: Any) -> dict:
    cases = tmp_path / "cases-measure.json"
    cases.write_text(json.dumps(CASES), encoding="utf-8")
    good = tmp_path / "good-measure.md"
    good.write_text(GOOD_LIST, encoding="utf-8")
    good_segments, unusable = mtc.load_good_cases(good)
    if kwargs.get("mode") == "free" and "free_segments" not in kwargs:
        free = tmp_path / "free-measure.json"
        free.write_text(json.dumps(FREE_CASES), encoding="utf-8")
        kwargs["free_segments"] = mtc.load_cases(free, kind="free")
    return mtc.measure(
        mtc.load_cases(cases),
        good_segments,
        unusable,
        TERMS,
        provider="openrouter",
        model="test-model",
        deterministic_lists=mtc.load_deterministic_lists(plain_normalization, TERMS),
        model_run=model_run,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Position anchor (the prefilter, validation and word-loss cases live in
# tests/test_term_corrections.py, next to the production module they moved to)
# ---------------------------------------------------------------------------


def test_only_the_named_occurrence_is_replaced() -> None:
    originals = ["Is het via ROI gegaan? Via ROI is het gegaan."]
    term_list = mtc.TermList(TERMS)

    accepted, rejected = mtc.validate_corrections([_fix("ROI", 2, "Roy")], originals, originals, [{"Roy"}], term_list)

    assert rejected == {}
    assert accepted == [mtc.AppliedCorrection(segment_index=0, start=27, end=30, target="Roy")]
    assert mtc.apply_corrections(originals, accepted) == ["Is het via ROI gegaan? Via Roy is het gegaan."]


# ---------------------------------------------------------------------------
# PII: the position is computed on the original segment, never on the stripped text
# ---------------------------------------------------------------------------


def test_position_is_computed_on_the_original_segment_under_the_pii_strip() -> None:
    segment = mtc.CaseSegment(
        segment_id="p1",
        text="Bram zegt ROI en daarna weer ROI.",
        labels=(mtc.Label(case_id="p1a", source="ROI", occurrence=2, expected="Roy"),),
        kind="context",
    )
    prepared = mtc.prepare(segment, mtc.TermPrefilter(TERMS), "openrouter")

    assert prepared.visible_text.startswith("[NAAM] zegt ROI")
    assert "Roy" in prepared.visible_candidates
    accepted, rejected = mtc.validate_corrections(
        [_fix("ROI", 2, "Roy"), _fix("Bram", 1, "Roy")],
        [segment.text],
        [prepared.visible_text],
        [set(prepared.visible_candidates)],
        mtc.TermList(TERMS),
    )

    assert rejected == {"source_changed_by_pii": 1}
    original_span = mtc.locate_occurrence(segment.text, "ROI", 2)
    assert (accepted[0].start, accepted[0].end) == original_span
    assert mtc.locate_occurrence(prepared.visible_text, "ROI", 2) != original_span
    assert mtc.apply_corrections([segment.text], accepted) == ["Bram zegt ROI en daarna weer Roy."]


def test_a_redacted_candidate_cannot_be_the_target() -> None:
    segment = mtc.CaseSegment(
        segment_id="p2",
        text="ik sprak Bramm gisteren",
        labels=(mtc.Label(case_id="p2a", source="Bramm", occurrence=1, expected="Bram", name_like=True),),
        kind="context",
    )
    prepared = mtc.prepare(segment, mtc.TermPrefilter(TERMS), "openrouter")

    assert "Bram" in prepared.candidates
    assert "Bram" not in prepared.visible_candidates
    assert mtc.pii_effect(prepared, segment.labels[0]) == "target_redacted"
    _, rejected = mtc.validate_corrections(
        [_fix("Bramm", 1, "Bram")],
        [segment.text],
        [prepared.visible_text],
        [set(prepared.visible_candidates)],
        mtc.TermList(TERMS),
    )
    assert rejected == {"target_not_candidate": 1}


def test_pii_off_sends_the_segment_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    segment = mtc.CaseSegment(
        segment_id="p3",
        text="Bram zegt ROI.",
        labels=(mtc.Label(case_id="p3a", source="ROI", occurrence=1, expected="Roy"),),
        kind="context",
    )

    prepared = mtc.prepare(segment, mtc.TermPrefilter(TERMS), "openrouter")

    assert prepared.visible_text == segment.text
    assert prepared.visible_candidates == prepared.candidates
    assert mtc.pii_effect(prepared, segment.labels[0]) is None


def test_a_structure_breaking_redaction_is_refused() -> None:
    with pytest.raises(ValueError, match="segment structure"):
        mtc.parse_visible("Segment 0: tekst", 1)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_cases_reads_the_labels(cases_file: Path) -> None:
    segments = mtc.load_cases(cases_file)

    assert [s.segment_id for s in segments] == ["s1", "s2", "s3", "s4", "s5"]
    assert segments[0].labels[0] == mtc.Label(case_id="c1", source="ROI", occurrence=1, expected="Roy", name_like=True)
    assert all(s.kind == "context" for s in segments)


@pytest.mark.parametrize(
    ("segment", "message"),
    [
        ({"id": "x", "text": "via ROI", "labels": [_label("", "ROI", 1, "Roy")]}, "needs an id"),
        ({"id": "x", "text": "via ROI", "labels": [_label("a", "Roy", 1, "ROI")]}, "does not occur"),
        ({"id": "x", "text": "via ROI", "labels": [_label("a", "ROI", 0, "Roy")]}, "integer >= 1"),
        ({"id": "x", "text": "via ROI", "labels": [_label("a", "ROI", 1, "ROI")]}, "different from source"),
        ({"id": "x", "text": "via\nROI", "labels": [_label("a", "ROI", 1, "Roy")]}, "one non-empty line"),
        ({"id": "x", "text": "via ROI", "labels": []}, "non-empty labels"),
        ({"id": "x", "text": "via ROI", "labels": [_label("x", "ROI", 1, "Roy")]}, "duplicated id"),
    ],
)
def test_load_cases_refuses_malformed_entries(tmp_path: Path, segment: dict, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"segments": [segment]}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        mtc.load_cases(path)


def test_load_cases_refuses_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("segments: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty 'segments'"):
        mtc.load_cases(path)


def test_load_terms_refuses_a_separator(tmp_path: Path) -> None:
    path = tmp_path / "terms.yaml"
    path.write_text("termen:\n  - 'ROI; Roy'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="newline or ';'"):
        mtc.load_terms(path)


def test_load_terms_reads_a_klant_yaml_list(terms_file: Path) -> None:
    assert mtc.load_terms(terms_file) == TERMS


def test_an_expected_term_outside_the_list_is_refused(cases_file: Path) -> None:
    with pytest.raises(ValueError, match="c2: its expected term is not in the term list"):
        mtc.check_expected_in_list(mtc.load_cases(cases_file), mtc.TermList(("Roy", "Sentrix", "Sofi")))


def test_load_good_cases_reads_only_the_goed_rows(good_file: Path) -> None:
    segments, unusable = mtc.load_good_cases(good_file)

    assert unusable == 0
    assert [s.segment_id for s in segments] == ["good-01", "good-02", "good-03"]
    assert [s.labels[0].source for s in segments] == ["ROI", "Brightwave", "Nederland"]
    assert all(s.kind == "good" and s.labels[0].expected is None for s in segments)
    assert not segments[0].text.startswith("...")


def test_load_deterministic_lists_refuses_a_missing_or_disabled_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        mtc.load_deterministic_lists(tmp_path / "missing.yaml", TERMS)
    disabled = tmp_path / "off.yaml"
    disabled.write_text("enabled: false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="disabled"):
        mtc.load_deterministic_lists(disabled, TERMS)


def test_deterministic_cases_are_split_off(cases_file: Path, plain_normalization: Path) -> None:
    lists = mtc.load_deterministic_lists(plain_normalization, TERMS)

    assert mtc.deterministic_case_ids(mtc.load_cases(cases_file), lists) == frozenset({"c3"})


def test_load_scale_transcript_cuts_real_text_and_refuses_padding(tmp_path: Path) -> None:
    path = tmp_path / "gesprek.txt"
    path.write_text("\n".join(f"[00:{i:02d}] " + " ".join(["woord"] * 10) for i in range(6)), encoding="utf-8")

    segments = mtc.load_scale_transcript([path], 60)

    assert len(segments) == 3
    assert sum(len(s.split()) for s in segments) == 60
    assert "[" not in " ".join(segments)
    with pytest.raises(ValueError, match="needs 61"):
        mtc.load_scale_transcript([path], 61)


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


def test_distractor_terms_are_deterministic_distinct_and_new() -> None:
    first = mtc.distractor_terms(300, TERMS)

    assert first == mtc.distractor_terms(300, TERMS)
    assert len(first) == 300
    assert len({t.casefold() for t in first}) == 300
    assert not {t.casefold() for t in first} & {t.casefold() for t in TERMS}


def test_scaled_terms_refuses_a_size_below_the_base() -> None:
    with pytest.raises(ValueError, match="smaller than the base"):
        mtc.scaled_terms(TERMS, 3)
    assert len(mtc.scaled_terms(TERMS, 40)) == 40


def test_measure_scale_reports_recall_and_time(cases_file: Path) -> None:
    segments = mtc.load_cases(cases_file)
    contextual = [label for s in segments for label in s.labels if label.expected and label.case_id != "c3"]
    conversation = ["een gewone zin over ROI en Team Leader"] * 20

    rows = mtc.measure_scale(TERMS, [len(TERMS), 60], segments, contextual, conversation)

    assert [row["terms"] for row in rows] == [len(TERMS), 60]
    assert all(row["prefilter_recall"] == 1.0 for row in rows)
    assert all(row["total_ms_median"] > 0 and row["conversation_words"] == 160 for row in rows)
    assert rows[1]["avg_candidates_labelled_segment"] >= rows[0]["avg_candidates_labelled_segment"]


# ---------------------------------------------------------------------------
# The whole measurement
# ---------------------------------------------------------------------------


def test_measure_scores_every_group(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel(
        {
            "via ROI gegaan": [_fix("ROI", 1, "Roy")],
            "drie keer ROI": [_fix("ROI", 1, "Roy")],
            "Sophie": [_fix("Sophie", 1, "Sofi")],
        }
    )

    result = _measure(tmp_path, plain_normalization, _model_run(model, runs=2))

    run = result["runs"][0]
    assert result["prefilter"]["positives"] == 3
    assert result["prefilter"]["deterministic_case_ids"] == ["c3"]
    assert result["prefilter"]["unsolvable_case_ids"] == ["c4"]
    assert result["prefilter"]["segments_redacted_by_pii"] == 1
    assert (run["hits"], run["positives"], run["recall"]) == (1, 3, 0.333)
    assert (run["recall_name_like"], run["recall_other"]) == (0.5, 0.0)
    assert run["rejected"] == {"source_changed_by_pii": 1}
    assert (run["good_broken"], run["good_total"], run["keep_broken"]) == (1, 3, 0)
    assert result["good_cases"] == {"total": 3, "unusable": 0, "sent_to_model": 2}
    assert len(model.requests) == 2 * run["requests"]
    assert result["per_case"]["c1"]["outcomes"] == ["hit", "hit"]
    assert result["per_case"]["good-01"]["outcomes"] == ["broken", "broken"]
    assert result["per_case"]["good-03"] == {
        "role": "good",
        "name_like": False,
        "sent_to_model": False,
        "outcomes": ["intact", "intact"],
    }
    assert result["criteria"]["3_good_cases_broken"]["met"] is False


def test_a_do_nothing_model_does_not_pass(tmp_path: Path, plain_normalization: Path) -> None:
    result = _measure(tmp_path, plain_normalization, _model_run(ScriptedModel({}), runs=3))

    assert [run["good_broken"] for run in result["runs"]] == [0, 0, 0]
    assert result["criteria"]["2_recall_median"] == {
        "required": ">= 0.6 median over the runs",
        "value": 0.0,
        "met": False,
    }


def test_a_failed_call_is_an_error_not_a_pass(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel({}, fail_on=("drie keer ROI", "Team Leader"))

    run = _measure(tmp_path, plain_normalization, _model_run(model))["runs"][0]

    assert run["model_errors"] == 2
    assert run["good_model_errors"] == 1
    assert run["good_broken"] == 0


def test_evaluate_criteria_needs_every_run_clean() -> None:
    def result(good_errors: int) -> dict[str, Any]:
        runs = [
            {"recall": 0.7, "good_broken": 0, "good_model_errors": 0},
            {"recall": 0.6, "good_broken": 0, "good_model_errors": good_errors},
            {"recall": 0.5, "good_broken": 0, "good_model_errors": 0},
        ]
        return {
            "prefilter": {"positives": 20, "recall": 0.9},
            "runs": runs,
            "good_cases": {"total": 17},
            "scale": [{"terms": 1000, "prefilter_recall": 0.9, "total_ms_median": 49.9}],
        }

    clean = mtc.evaluate_criteria(result(0))
    assert all(row["met"] for row in clean.values())
    assert clean["2_recall_median"]["value"] == 0.6
    assert mtc.evaluate_criteria(result(1))["3_good_cases_broken"]["met"] is False


def test_prefilter_only_leaves_the_model_criteria_open(tmp_path: Path, plain_normalization: Path) -> None:
    criteria = _measure(tmp_path, plain_normalization, None)["criteria"]

    assert criteria["2_recall_median"]["met"] is None
    assert criteria["3_good_cases_broken"]["met"] is None
    assert criteria["5_scale"]["met"] is None
    assert criteria["1_context_set_size"] == {
        "required": ">= 20 labelled contextual corrections",
        "value": 3,
        "met": False,
    }


def test_output_holds_no_transcript_text_and_no_terms(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel({"via ROI gegaan": [_fix("ROI", 1, "Roy")], "Team Leader": [_fix("Team", 1, "Sofi")]})
    result = _measure(
        tmp_path,
        plain_normalization,
        _model_run(model, runs=2),
        scale_sizes=[len(TERMS)],
        conversation=[f"een gesprek over Team Leader met {SECRET}"] * 4,
    )

    dumped = json.dumps(result, ensure_ascii=False) + mtc.format_summary(result)
    forbidden = [SECRET, "Team Leader", "Teamleader", "Zentrix", "Sentrix", "Sophie", "Sofi", "Brightwave"]
    forbidden += [segment["text"] for segment in CASES["segments"]]
    forbidden += ["drie keer", "chatbot", "Nederland", "Marten"]
    assert [text for text in forbidden if text in dumped] == []


def test_main_prefilter_only_writes_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cases_file: Path,
    good_file: Path,
    terms_file: Path,
    plain_normalization: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    for name in _ROUTE_KEYS:
        monkeypatch.delenv(name, raising=False)
    transcript = tmp_path / "gesprek.txt"
    transcript.write_text(
        "\n".join(f"[00:{i:02d}] " + f"via ROI en {SECRET} " * 5 for i in range(10)), encoding="utf-8"
    )
    out = tmp_path / "result.json"

    code = mtc.main(
        [
            "--cases", str(cases_file), "--good-cases", str(good_file), "--terms", str(terms_file),
            "--normalization-config", str(plain_normalization), "--prefilter-only",
            "--scale", f"{len(TERMS)},40", "--scale-transcript", str(transcript), "--scale-words", "100",
            "--out", str(out),
        ]
    )

    assert code == 0
    [result] = json.loads(out.read_text(encoding="utf-8"))["measurements"]
    assert result["config"]["provider"] == "openrouter"
    assert result["config"]["mode"] == "term"
    assert result["config"]["pii_mode"] == "cloud_only"
    assert [row["terms"] for row in result["scale"]] == [len(TERMS), 40]
    assert not list(tmp_path.glob("*.tmp"))
    assert SECRET not in capsys.readouterr().out + out.read_text(encoding="utf-8")


def test_main_refuses_a_malformed_cases_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], good_file: Path, terms_file: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"segments": [{"id": "x", "text": "via ROI", "labels": []}]}), encoding="utf-8")

    code = mtc.main(["--cases", str(bad), "--good-cases", str(good_file), "--terms", str(terms_file)])

    assert code == 2
    assert "non-empty labels" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Round 2: the free arm
# ---------------------------------------------------------------------------


def test_free_validation_accepts_any_target_and_keeps_the_guards() -> None:
    originals = ["dan neem je de Hele Sjebang in een keer"]
    corrections = [
        _fix("Hele Sjebang", 1, " hele shebang "),
        _fix("de Hele", 1, "het hele"),
        _fix("keer", 1, "[NAAM]"),
        _fix("in een keer", 1, "ineens"),
        _fix("Sjebang", 2, "shebang"),
        _fix("neem", 1, "neem", 3),
    ]

    accepted, rejected = mtc.validate_free_corrections(corrections, originals, originals)

    assert rejected == {
        "overlap": 1,
        "placeholder_in_target": 1,
        "word_loss": 1,
        "source_not_at_position": 1,
        "segment_out_of_range": 1,
    }
    assert mtc.apply_corrections(originals, accepted) == ["dan neem je de hele shebang in een keer"]


def test_free_validation_positions_on_the_original_under_the_pii_strip() -> None:
    segment = mtc.CaseSegment(
        segment_id="p4",
        text="Bram zegt Sjebang en daarna weer Sjebang.",
        labels=(mtc.Label(case_id="p4a", source="Sjebang", occurrence=2, expected="shebang"),),
        kind="free",
    )
    prepared = mtc.prepare(segment, mtc.TermPrefilter(TERMS), "openrouter")

    assert prepared.visible_text.startswith("[NAAM] zegt")
    accepted, rejected = mtc.validate_free_corrections(
        [_fix("Sjebang", 2, "shebang"), _fix("Bram", 1, "Brahm")], [segment.text], [prepared.visible_text]
    )

    assert rejected == {"source_changed_by_pii": 1}
    assert (accepted[0].start, accepted[0].end) == mtc.locate_occurrence(segment.text, "Sjebang", 2)
    assert mtc.apply_corrections([segment.text], accepted) == ["Bram zegt Sjebang en daarna weer shebang."]


@pytest.mark.parametrize(
    ("corrections", "outcome"),
    [
        ([_fix("Dobby", 1, "hobby")], "hit"),
        ([_fix("als Dobby", 1, "als hobby")], "hit"),  # a wider source still repairs
        ([_fix("Dobby in", 1, "hobby in")], "hit"),
        ([_fix("Dobby", 1, "Bobby")], "wrong"),
        ([_fix("Dobby", 1, "Dobby hobby")], "wrong"),  # the misheard word is still there
        ([_fix("weekend", 1, "week")], "miss"),
        ([], "miss"),
    ],
)
def test_repair_outcome(corrections: list[mtc.TermCorrection], outcome: str) -> None:
    text = "hij doet dat als Dobby in het weekend"
    label = mtc.Label(case_id="f", source="Dobby", occurrence=1, expected="hobby")
    accepted, _ = mtc.validate_free_corrections(corrections, [text], [text])

    assert mtc.repair_outcome(text, label, accepted) == outcome


def test_repair_outcome_joins_adjacent_corrections_over_the_phrase() -> None:
    text = "dan neem je de Hele Sjebang in een keer"
    label = mtc.Label(case_id="f", source="Hele Sjebang", occurrence=1, expected="hele shebang")
    both, _ = mtc.validate_free_corrections([_fix("Hele", 1, "hele"), _fix("Sjebang", 1, "shebang")], [text], [text])
    half, _ = mtc.validate_free_corrections([_fix("Hele", 1, "hele")], [text], [text])

    assert mtc.repair_outcome(text, label, both) == "hit"
    assert mtc.repair_outcome(text, label, half) == "wrong"


def test_repair_outcome_accepts_every_expected_form() -> None:
    text = "we koppelen het aan het KMP van de klant"
    label = mtc.Label(case_id="f", source="KMP", occurrence=1, expected="CRM", alternatives=("CRMP",))
    accepted, _ = mtc.validate_free_corrections([_fix("KMP", 1, "crmp")], [text], [text])

    assert mtc.repair_outcome(text, label, accepted) == "hit"


@pytest.mark.parametrize(
    ("source", "target", "candidates", "added"),
    [
        ("Sjebang", "shebang op 12 mei", (), ["op", "12", "mei"]),  # an agreement nobody made
        ("de Hele", "de hele Pieter", (), ["pieter"]),  # a name out of nowhere
        ("Sjebang", "shebang", (), []),  # a spelling variant of the source
        ("Dobby", "hobbyclub", (), ["hobbyclub"]),  # a far-off repair is flagged too; the hand check decides
        ("Dobby", "hobby", (), []),  # a spelling variant of the source
        ("Dobby", "Roy", ("Roy",), []),  # a candidate the model saw
        ("Dobby", "week", (), []),  # elsewhere in the segment
        ("Deal Flow", "Dealflow", (), []),  # written together
        ("Teamleader", "Team Leader", (), []),  # split
    ],
)
def test_added_words(source: str, target: str, candidates: tuple[str, ...], added: list[str]) -> None:
    text = "dan neem je de Hele Sjebang in een keer, met Dobby en week, Deal Flow of Teamleader"
    correction = mtc.AppliedCorrection(
        segment_index=0, start=text.index(source), end=text.index(source) + len(source), target=target
    )

    assert mtc.added_words(text, correction, candidates) == added


def test_measure_free_arm_scores_every_group(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel(
        {
            "Hele Sjebang": [_fix("Hele Sjebang", 1, "hele shebang")],
            "Dobby": [_fix("Dobby", 1, "Bobby")],
            "via ROI gegaan": [_fix("via ROI", 1, "via Roy")],
            "Team Leader": [_fix("tegenwoordig", 1, "tegenwoordig met 12 man")],
            "drie keer ROI": [_fix("ROI uitkomt", 1, "ROI uitkomen")],
        }
    )

    result = _measure(tmp_path, plain_normalization, _model_run(model, runs=2), mode="free")

    run = result["runs"][0]
    assert result["config"]["mode"] == "free"
    assert run["requests"] == 5 + 3 + 3  # every segment is asked about, candidates or not
    assert len(model.requests) == 2 * run["requests"]
    assert (run["free_repaired"], run["free_wrong"], run["free_total"]) == (1, 1, 3)
    assert run["hits"] == 1  # the wider 'via ROI' still repairs the contextual case
    assert run["good_broken"] == 1  # an overlap with a good word breaks it, as in round 1
    assert run["content_added"] == 1
    assert run["content_added_by_kind"] == {"context": 1}
    assert run["content_added_segment_ids"] == ["s2"]
    assert result["free_cases"] == {"total": 3, "segments": 3, "redacted_by_pii": 0, "unsolvable_by_pii": 0}
    assert result["per_case"]["f2"] == {
        "role": "free",
        "name_like": False,
        "prefilter": False,
        "pii": None,
        "outcomes": ["hit", "hit"],
    }
    criteria = result["criteria"]
    assert list(criteria) == ["1_free_repaired", "2_good_cases_broken", "3_content_added"]
    assert criteria["1_free_repaired"]["value"] == 1
    assert criteria["1_free_repaired"]["met"] is False
    assert criteria["3_content_added"] == {
        "required": "0 corrections adding content (fixed code rule) in every run",
        "value": [1, 1],
        "met": False,
    }


def test_evaluate_free_criteria_needs_the_full_set_and_clean_runs() -> None:
    def result(repaired: list[int], added: list[int], free_total: int = 14) -> dict[str, Any]:
        runs = [
            {"free_repaired": r, "content_added": a, "good_broken": 0, "good_model_errors": 0}
            for r, a in zip(repaired, added, strict=True)
        ]
        return {
            "config": {"mode": "free"},
            "runs": runs,
            "good_cases": {"total": 17},
            "free_cases": {"total": free_total},
        }

    clean = mtc.evaluate_criteria(result([7, 6, 9], [0, 0, 0]))
    assert all(row["met"] for row in clean.values())
    assert clean["1_free_repaired"]["value"] == 7
    assert mtc.evaluate_criteria(result([6, 6, 9], [0, 0, 0]))["1_free_repaired"]["met"] is False
    assert mtc.evaluate_criteria(result([7, 7, 7], [0, 1, 0]))["3_content_added"]["met"] is False
    assert mtc.evaluate_criteria(result([7, 7, 7], [0, 0, 0], free_total=13))["1_free_repaired"]["met"] is False


def test_the_free_arm_needs_free_cases(tmp_path: Path, plain_normalization: Path) -> None:
    with pytest.raises(ValueError, match="needs the free cases"):
        _measure(tmp_path, plain_normalization, None, mode="free", free_segments=())
    with pytest.raises(ValueError, match="unknown mode"):
        _measure(tmp_path, plain_normalization, None, mode="vrij")


def test_load_free_cases_reads_alternatives_and_refuses_a_keep(tmp_path: Path) -> None:
    path = tmp_path / "free.json"
    path.write_text(json.dumps(FREE_CASES), encoding="utf-8")

    segments = mtc.load_cases(path, kind="free")

    assert [s.kind for s in segments] == ["free"] * 3
    assert segments[2].labels[0].expected == "CRM"
    assert segments[2].labels[0].alternatives == ("CRMP",)
    keep = {"segments": [{"id": "x", "text": "als Dobby", "labels": [_label("a", "Dobby", 1, None)]}]}
    path.write_text(json.dumps(keep), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty string or list"):
        mtc.load_cases(path, kind="free")
    listed = {"segments": [{"id": "x", "text": "als Dobby", "labels": [_label("a", "Dobby", 1, ["hobby"])]}]}
    path.write_text(json.dumps(listed), encoding="utf-8")
    with pytest.raises(ValueError, match="null or a term"):
        mtc.load_cases(path)


def test_ids_shared_between_sets_are_refused(tmp_path: Path, plain_normalization: Path) -> None:
    clash = {"segments": [{"id": "s1", "text": "als Dobby", "labels": [_label("x1", "Dobby", 1, "hobby")]}]}
    path = tmp_path / "clash.json"
    path.write_text(json.dumps(clash), encoding="utf-8")

    with pytest.raises(ValueError, match="'s1' occurs in more than one set"):
        _measure(tmp_path, plain_normalization, None, mode="free", free_segments=mtc.load_cases(path, kind="free"))


def test_free_output_holds_no_text(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel(
        {
            "Hele Sjebang": [_fix("Hele Sjebang", 1, "hele shebang op 12 mei")],
            "Dobby": [_fix("Dobby", 1, "hobby")],
        }
    )
    result = _measure(tmp_path, plain_normalization, _model_run(model, runs=2), mode="free")

    dumped = json.dumps(result, ensure_ascii=False) + mtc.format_summary(result)
    forbidden = ["Sjebang", "shebang", "Dobby", "hobby", "KMP", "CRM", "12 mei", SECRET, "Team Leader"]
    forbidden += [segment["text"] for segment in FREE_CASES["segments"]]
    assert [text for text in forbidden if text in dumped] == []
    assert result["runs"][0]["content_added"] == 1


def test_show_flagged_prints_the_flag_to_stderr_only(
    tmp_path: Path, plain_normalization: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model = ScriptedModel({"Hele Sjebang": [_fix("Hele Sjebang", 1, "hele shebang op 12 mei")]})
    model_run = mtc.ModelRun(
        clients=(model,), model="test-model", temperature=0.1, timeout_s=5.0, runs=1, show_flagged=True
    )

    result = _measure(tmp_path, plain_normalization, model_run, mode="free")

    captured = capsys.readouterr()
    assert "[flagged] test-model free run 1 fs2" in captured.err
    assert "Sjebang" not in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Round 2: tokens and cost
# ---------------------------------------------------------------------------


def test_cost_report_per_1000_words_and_per_call() -> None:
    runs = [
        {"usage": {"calls": 2, "calls_without_usage": 0, "words_sent": 30, "input_tokens": 240, "output_tokens": 24}},
        {"usage": {"calls": 1, "calls_without_usage": 1, "words_sent": 20, "input_tokens": 160, "output_tokens": 16}},
    ]

    report = mtc.cost_report(runs, mtc.Price(input_usd_per_mtok=1.0, output_usd_per_mtok=5.0), 0.9)

    assert report["calls"] == 3
    assert report["calls_without_usage"] == 1
    assert report["per_1000_words"] == {"input_tokens": 8000.0, "output_tokens": 800.0}
    assert report["mean_per_call"] == {"input_tokens": 133.3, "output_tokens": 13.3, "words": 16.7}
    assert report["per_60_minute_call"] == {
        "words": 9000,
        "input_tokens": 72000,
        "output_tokens": 7200,
        "usd": 0.108,
        "eur": 0.0972,
    }


def test_cost_report_without_usage_or_price() -> None:
    assert mtc.cost_report([], None, None)["per_60_minute_call"] is None
    runs = [{"usage": {"calls": 1, "words_sent": 10, "input_tokens": 50, "output_tokens": 5}}]
    call = mtc.cost_report(runs, None, None)["per_60_minute_call"]
    assert (call["usd"], call["eur"]) == (None, None)
    assert call["input_tokens"] == 45000


def test_tokens_are_read_from_the_client_that_made_the_call(tmp_path: Path, plain_normalization: Path) -> None:
    first = ScriptedModel({}, usage={"input_tokens": 100, "output_tokens": 1, "cache_read_tokens": 0})
    second = ScriptedModel({}, usage={"input_tokens": 1000, "output_tokens": 10, "cache_read_tokens": 0})

    result = _measure(tmp_path, plain_normalization, _model_run(first, 1, second), mode="free")

    usage = result["runs"][0]["usage"]
    assert usage["calls"] == len(first.requests) + len(second.requests) == 11
    assert usage["input_tokens"] == 100 * len(first.requests) + 1000 * len(second.requests)
    assert usage["output_tokens"] == len(first.requests) + 10 * len(second.requests)
    assert usage["words_sent"] > 0


def test_a_failed_call_counts_no_tokens(tmp_path: Path, plain_normalization: Path) -> None:
    model = ScriptedModel({}, fail_on=("drie keer ROI",))

    usage = _measure(tmp_path, plain_normalization, _model_run(model))["runs"][0]["usage"]

    assert usage["calls"] == len(model.requests) - 1
    assert usage["input_tokens"] == USAGE["input_tokens"] * usage["calls"]


# ---------------------------------------------------------------------------
# Round 2: the model per run through REPORT_TERMS_LLM_MODEL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["model", "model=1", "model=a,b", "=1,5", "model=1,-5", "model=1,5,7"]
)
def test_parse_price_refuses_malformed_input(raw: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="MODEL=IN,OUT"):
        mtc._parse_price(raw)


def test_parse_price_keeps_a_model_id_with_slashes() -> None:
    assert mtc._parse_price("vendor/model-1.5=3,15") == ("vendor/model-1.5", mtc.Price(3.0, 15.0))


def test_main_measures_each_model_through_report_terms_llm_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cases_file: Path,
    good_file: Path,
    terms_file: Path,
    plain_normalization: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "conversation-model")
    for name in _ROUTE_KEYS:
        monkeypatch.delenv(name, raising=False)
    built: list[tuple[str, str]] = []
    clients: list[ScriptedModel] = []

    def fake_build(resolved: Any, **_: Any) -> ScriptedModel:
        built.append((resolved.task, resolved.model))
        client = ScriptedModel({"Hele Sjebang": [_fix("Hele Sjebang", 1, "hele shebang")]})
        clients.append(client)
        return client

    monkeypatch.setattr(mtc, "build_llm_client", fake_build)
    free = tmp_path / "free.json"
    free.write_text(json.dumps(FREE_CASES), encoding="utf-8")
    out = tmp_path / "result.json"

    code = mtc.main(
        [
            "--cases", str(cases_file), "--good-cases", str(good_file), "--terms", str(terms_file),
            "--normalization-config", str(plain_normalization), "--free-cases", str(free),
            "--mode", "term,free", "--models", "vendor/model-a,vendor/model-b",
            "--price", "vendor/model-a=1,5", "--eur-per-usd", "0.9",
            "--runs", "1", "--concurrency", "2", "--out", str(out),
        ]
    )

    assert code == 0
    assert built == [("report_terms", "vendor/model-a")] * 2 + [("report_terms", "vendor/model-b")] * 2
    assert "REPORT_TERMS_LLM_MODEL" not in os.environ
    measurements = json.loads(out.read_text(encoding="utf-8"))["measurements"]
    assert [(m["config"]["model"], m["config"]["mode"]) for m in measurements] == [
        ("vendor/model-a", "term"),
        ("vendor/model-a", "free"),
        ("vendor/model-b", "term"),
        ("vendor/model-b", "free"),
    ]
    assert {model for client in clients[:2] for model in client.models} == {"vendor/model-a"}
    assert {model for client in clients[2:] for model in client.models} == {"vendor/model-b"}
    assert measurements[0]["cost"]["per_60_minute_call"]["eur"] is not None
    assert measurements[2]["cost"]["per_60_minute_call"]["usd"] is None
    assert measurements[1]["runs"][0]["free_repaired"] == 1
    assert "Sjebang" not in capsys.readouterr().out + out.read_text(encoding="utf-8")


def test_resolve_terms_llm_restores_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ROUTE_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "conversation-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "configured-terms-model")
    monkeypatch.setattr(mtc, "build_llm_client", lambda resolved, **_: resolved.model)

    resolved, clients = mtc.resolve_terms_llm("vendor/model-a", mtc.DetectorConfig.from_env(), 2)

    assert (resolved.task, resolved.model) == ("report_terms", "vendor/model-a")
    assert clients == ("vendor/model-a", "vendor/model-a")
    assert os.environ["REPORT_TERMS_LLM_MODEL"] == "configured-terms-model"
    assert mtc.resolve_terms_llm(None, mtc.DetectorConfig.from_env(), 0)[0].model == "configured-terms-model"


def test_resolve_terms_llm_without_its_own_model_measures_the_report_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without REPORT_TERMS_* values the harness measures what production would call: the report route."""
    for name in _ROUTE_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "conversation-model")
    monkeypatch.setenv("REPORT_LLM_MODEL", "configured-report-model")
    monkeypatch.setattr(mtc, "build_llm_client", lambda resolved, **_: resolved.model)

    resolved, _ = mtc.resolve_terms_llm(None, mtc.DetectorConfig.from_env(), 0)

    assert (resolved.task, resolved.provider) == ("report_terms", "openrouter")
    assert resolved.model == "configured-report-model"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--mode", "free"], "--mode free needs --free-cases"),
        (["--mode", "term,vrij"], "unknown mode"),
        (["--models", "a,a"], "without repeats"),
        (["--models", "a", "--price", "b=1,5"], "not in --models"),
        (["--price", "a=1,5", "--price", "a=2,5"], "names a model twice"),
        (["--eur-per-usd", "0"], "must be positive"),
    ],
)
def test_main_refuses_inconsistent_arguments(
    extra: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
    cases_file: Path,
    good_file: Path,
    terms_file: Path,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        mtc.main(["--cases", str(cases_file), "--good-cases", str(good_file), "--terms", str(terms_file), *extra])

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Synthetic probes (outside the criteria)
# ---------------------------------------------------------------------------

PROBE_FILE = Path(__file__).parent / "fixtures" / "term_correction_probes.yaml"

PROBES_YAML = """
probes:
  - id: p-fix
    text: "We gebruiken nu Team Leader voor de offertes."
    terms: [Teamleader]
    occurrences:
      - {source: "Team Leader", correct_to: Teamleader}
  - id: p-keep
    text: "Roy belt je morgen terug."
    terms: [ROI]
    occurrences:
      - {source: "Roy", leave: true}
  - id: p-mixed
    text: "De ROI is er en Roy ook, maar die ROI telt niet."
    terms: [ROI, Roy]
    occurrences:
      - {source: "ROI", occurrence: 1, leave: true}
      - {source: "ROI", occurrence: 2, correct_to: Roy}
"""


@pytest.fixture
def probes_file(tmp_path: Path) -> Path:
    path = tmp_path / "probes.yaml"
    path.write_text(PROBES_YAML, encoding="utf-8")
    return path


def test_load_probes_reads_the_shipped_fixture() -> None:
    probes = mtc.load_probes(PROBE_FILE)
    assert len(probes) >= 16
    kinds = {label.expected is None for probe in probes for label in probe.segment.labels}
    assert kinds == {True, False}
    for needle in ("Team Leader", "team leader", "ROI", "Roy", "IDI", "IDE", "HupSpot"):
        assert any(needle in probe.segment.text for probe in probes)


def test_load_probes_reads_labels_and_terms(probes_file: Path) -> None:
    probes = mtc.load_probes(probes_file)
    assert [p.probe_id for p in probes] == ["p-fix", "p-keep", "p-mixed"]
    labels = probes[2].segment.labels
    assert [(label.case_id, label.occurrence, label.expected) for label in labels] == [
        ("p-mixed.1", 1, None),
        ("p-mixed.2", 2, "Roy"),
    ]
    assert probes[0].terms == ("Teamleader",)


def _probe_entry(*occurrences: dict, **overrides: Any) -> dict:
    entry: dict[str, Any] = {"id": "x", "text": "a b", "terms": ["T"], "occurrences": list(occurrences)}
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not None}


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        (_probe_entry({"source": "a"}), "exactly one"),
        (_probe_entry({"source": "a", "leave": True, "correct_to": "T"}), "exactly one"),
        (_probe_entry({"source": "a", "correct_to": "U"}), "probe's list"),
        (_probe_entry({"source": "zzz", "leave": True}), "does not occur"),
        (_probe_entry({"source": "a", "leave": False}), "leave must be true"),
        (_probe_entry({"source": "a", "leave": True}, terms=[]), "terms"),
        (_probe_entry(), "occurrences"),
        (_probe_entry({"source": "a", "leave": True}, id=None), "needs an id"),
    ],
)
def test_load_probes_refuses_malformed_entries(tmp_path: Path, probe: dict, message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(json.dumps({"probes": [probe]}), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        mtc.load_probes(path)


def test_load_probes_refuses_a_repeated_id_and_an_empty_file(tmp_path: Path) -> None:
    entry = {"id": "x", "text": "a b", "terms": ["T"], "occurrences": [{"source": "a", "leave": True}]}
    path = tmp_path / "bad.yaml"
    path.write_text(json.dumps({"probes": [entry, entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        mtc.load_probes(path)
    path.write_text("probes: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        mtc.load_probes(path)


def _applied(text: str, source: str, target: str, occurrence: int = 1) -> mtc.AppliedCorrection:
    span = mtc.locate_occurrence(text, source, occurrence)
    assert span is not None
    return mtc.AppliedCorrection(segment_index=0, start=span[0], end=span[1], target=target)


def test_score_probes_counts_good_wrong_and_missed(probes_file: Path) -> None:
    fix, keep, mixed = mtc.load_probes(probes_file)
    accepted = {
        fix.probe_id: [_applied(fix.segment.text, "Team Leader", "Teamleader")],
        keep.probe_id: [_applied(keep.segment.text, "Roy", "ROI")],
        mixed.probe_id: [],
    }
    result = mtc.score_probes([fix, keep, mixed], accepted)
    assert result["good_ids"] == ["p-fix"]
    assert result["wrongly_corrected_ids"] == ["p-keep"]
    assert result["missed_ids"] == ["p-mixed"]
    assert (result["probes"], result["good"], result["wrongly_corrected"], result["missed"]) == (3, 1, 1, 1)


def test_score_probes_counts_a_wrong_target_as_missed_and_an_error_never_as_good(probes_file: Path) -> None:
    fix, keep, mixed = mtc.load_probes(probes_file)
    accepted = {fix.probe_id: [_applied(fix.segment.text, "Team Leader", "Roy")]}
    result = mtc.score_probes([fix, keep, mixed], accepted, errored={"p-keep"})
    assert result["missed_ids"] == ["p-fix", "p-mixed"]
    assert result["error_ids"] == ["p-keep"]
    assert "p-keep" not in result["good_ids"]


def test_run_probes_scores_a_scripted_model(probes_file: Path) -> None:
    probes = mtc.load_probes(probes_file)
    model = ScriptedModel(
        {
            "Team Leader": [_fix("Team Leader", 1, "Teamleader")],
            "belt je morgen": [_fix("Roy", 1, "ROI")],
        },
        fail_on=("De ROI is er",),
    )
    result = asyncio.run(mtc.run_probes(probes, _model_run(model), "openrouter"))
    # The name is stripped by the PII policy before the model sees it, so its correction is refused.
    assert result["good_ids"] == ["p-fix", "p-keep"]
    assert result["error_ids"] == ["p-mixed"]
    assert result["model"] == "test-model"
    assert result["probes"] == 3
    # Counts and ids only: no segment text in the result.
    assert "Team Leader" not in json.dumps(result)


def test_main_probes_refuses_prefilter_only(
    probes_file: Path, cases_file: Path, good_file: Path, terms_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        mtc.main(
            [
                "--cases", str(cases_file), "--good-cases", str(good_file), "--terms", str(terms_file),
                "--prefilter-only", "--probes", str(probes_file),
            ]
        )
    assert "--probes" in capsys.readouterr().err
