"""Tests for scripts/measure_call_terms.py on synthetic data only.

Fictional companies, tiny transcripts and a tiny normalization config: no customer names,
audio or conversation content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from scripts import measure_call_terms as ct
from scripts import measure_entities_per_model as m
from scripts import measure_transcript_normalization as base
from tests.test_measure_entities_per_model import (
    LISTENING,
    NORMALIZATION_YAML,
    TUNING,
    _fixtures,
    _lists,
)

# Chunks start at 00:00, 00:30 (Akme), 02:00 (Bolt), 03:30 (Zork) relative to the fragment.
TRANSCRIPT = "[00:30] samen met Akme nu\n[02:00] geleverd door Bolt\n[03:30] de afdeling Zork"


def _cases(tmp_path: Path):
    cases, fragments = _fixtures(tmp_path)
    return cases, fragments


class TestDeriveCallTerms:
    def test_only_termen_count_like_the_product(self):
        data = {"bedrijf": "Acme", "termen": ["Wattelaar", "VWA -> VBA"], "contactpersonen": ["Roos Bergkamp"]}
        assert ct.derive_call_terms(data) == ("Wattelaar", "VWA -> VBA")

    def test_contacts_counted_only_on_request_and_split_into_words(self):
        data = {"bedrijf": "Acme", "termen": ["Wattelaar"], "contactpersonen": ["Roos Bergkamp"]}
        assert ct.derive_call_terms(data, include_contacts=True) == ("Wattelaar", "Roos", "Bergkamp")

    def test_duplicates_and_blanks_are_sanitized(self):
        data = {"bedrijf": "Acme", "termen": ["Wattelaar", " Wattelaar ", "Bergkamp"]}
        assert ct.derive_call_terms(data) == ("Wattelaar", "Bergkamp")

    def test_invalid_klant_yaml_is_refused(self):
        with pytest.raises(ValidationError):
            ct.derive_call_terms({"bedrijf": "Acme", "termen": ["x" * 65]})
        with pytest.raises(ValidationError):
            ct.derive_call_terms({"termen": ["Wattelaar"]})

    def test_load_klant_yaml_rejects_non_mapping(self, tmp_path):
        path = tmp_path / "klant.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            ct.load_klant_yaml(path)


class TestCanonicalForm:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [("Wattelaar", "Wattelaar"), ("VWA -> VBA", "VBA"), (" x -> ", None), ("", None)],
    )
    def test_canonical_form(self, entry, expected):
        assert ct.canonical_form(entry) == expected


class TestUpperBound:
    def test_single_token_is_term_and_multiword_is_variant(self, tmp_path):
        cases = [
            base.TuningCase("acmecorp", "Akme", "Acme", "fout", ""),
            base.TuningCase("acmecorp", "Jansen", "Jan Jansen", "fout", ""),
            base.TuningCase("acmecorp", "Akme", "Acme", "fout", ""),
            base.TuningCase("other", "Zork", "Zorg", "fout", ""),
        ]
        assert ct.upper_bound_terms(cases, "acmecorp") == ["Acme", "Jansen -> Jan Jansen"]

    def test_oracle_adds_an_exact_variant_per_case(self):
        cases = [
            base.TuningCase("acmecorp", "Akme", "Acme", "fout", ""),
            base.TuningCase("acmecorp", "Bolt", "Bolt", "goed", ""),
        ]
        assert ct.upper_bound_terms(cases, "acmecorp", oracle=True) == ["Akme -> Acme", "Bolt"]


class TestProvenance:
    def _prov(self, call="2026-04-09T10:00", **overrides):
        entry = {"term": "Wattelaar", "source": "briefing", "source_datetime": "2026-04-01"}
        entry.update(overrides)
        return {"call_datetime": call, "entries": [entry]}

    def test_valid_provenance_is_returned_per_term(self):
        assert ct.validate_provenance(self._prov(), ["Wattelaar"])[0]["source"] == "briefing"

    def test_same_day_source_passes_only_with_a_time_before_the_call(self):
        ok = self._prov(source_datetime="2026-04-09T09:21")
        assert ct.validate_provenance(ok, ["Wattelaar"])[0]["source_datetime"] == "2026-04-09T09:21"
        with pytest.raises(ct.ProvenanceError, match="at or after the call start"):
            ct.validate_provenance(self._prov(source_datetime="2026-04-09T10:00"), ["Wattelaar"])
        with pytest.raises(ct.ProvenanceError, match="at or after"):
            ct.validate_provenance(self._prov(source_datetime="2026-04-09"), ["Wattelaar"])  # end of that day

    def test_source_after_call_is_refused(self):
        with pytest.raises(ct.ProvenanceError, match="at or after"):
            ct.validate_provenance(self._prov(source_datetime="2026-05-01"), ["Wattelaar"])

    def test_term_without_entry_is_refused(self):
        with pytest.raises(ct.ProvenanceError, match="no provenance"):
            ct.validate_provenance(self._prov(), ["Wattelaar", "Bergkamp"])

    def test_empty_source_and_bad_moment_are_refused(self):
        with pytest.raises(ct.ProvenanceError, match="empty source"):
            ct.validate_provenance(self._prov(source=" "), ["Wattelaar"])
        with pytest.raises(ct.ProvenanceError, match="valid source_datetime"):
            ct.validate_provenance(self._prov(source_datetime="gisteren"), ["Wattelaar"])

    def test_missing_or_bad_call_datetime_is_refused(self):
        with pytest.raises(ct.ProvenanceError, match="call_datetime"):
            ct.validate_provenance({"entries": []}, [])
        with pytest.raises(ct.ProvenanceError, match="call_datetime"):
            ct.validate_provenance({"call_datetime": "morgen", "entries": []}, [])


class TestCoverage:
    def test_counts_covered_short_and_variant_terms(self, tmp_path):
        cases, _ = _cases(tmp_path)
        terms = ("Acme", "Bolt", "Zork -> Zorg", "Wattelaar")
        cov = ct.coverage(terms, cases, "acmecorp")
        assert (cov.targets, cov.covered) == (3, 3)
        assert cov.terms == 4
        assert cov.short_terms == 2  # Acme, Bolt: under five characters, exact only
        assert cov.variant_terms == 1
        assert cov.share == 1.0
        assert (cov.inert_terms, cov.covered_usable) == (0, 3)

    def test_bare_multiword_term_is_covered_but_inert(self, tmp_path):
        cases = [base.TuningCase("acmecorp", "Jansen", "Jan Jansen", "fout", "")]
        cov = ct.coverage(("Jan Jansen", "Jansen -> Jan Jansen"), cases, "acmecorp")
        assert (cov.covered, cov.covered_usable, cov.inert_terms) == (1, 1, 1)
        inert_only = ct.coverage(("Jan Jansen",), cases, "acmecorp")
        assert (inert_only.covered, inert_only.covered_usable) == (1, 0)

    def test_partial_and_empty_coverage(self, tmp_path):
        cases, _ = _cases(tmp_path)
        assert ct.coverage(("acme",), cases, "acmecorp").covered == 1  # case-insensitive
        assert ct.coverage((), cases, "acmecorp").share == 0.0
        assert ct.coverage((), cases, "absent").share == 0.0


class TestScoreArm:
    def _score(self, tmp_path, call_terms, arm="x"):
        cases, fragments = _cases(tmp_path)
        lists = ct.with_call_terms(_lists(tmp_path), ct.sanitize_call_terms(list(call_terms)))
        return ct.score_arm(arm, "run1", "acmecorp", TRANSCRIPT, cases, fragments["acmecorp"], lists)

    def test_no_list_baseline_breaks_the_good_case(self, tmp_path):
        result = self._score(tmp_path, [], arm="a")
        assert (result.before_hits, result.after_hits) == (1, 1)  # Akme -> Acme comes from the fixed layer
        assert result.goed_broken == 1
        assert result.fout_repaired == 1

    def test_call_terms_repair_and_protect(self, tmp_path):
        result = self._score(tmp_path, ["Akme -> Acme", "Bolt", "Zork -> Zorg"])
        assert (result.before_hits, result.after_hits) == (1, 3)
        assert result.goed_broken == 0  # the call term shields Bolt from the fixed Bolt -> Boot
        assert result.fout_repaired == 2
        assert result.cases_scored == 3

    def test_only_the_fragments_own_cases_are_scored(self, tmp_path):
        cases, fragments = _cases(tmp_path)
        result = ct.score_arm("a", "run1", "nolisten", "[00:00] Quix", cases, fragments["nolisten"], _lists(tmp_path))
        assert (result.cases_scored, result.after_hits) == (1, 0)


class TestOutsideReplacements:
    def test_only_chunks_beyond_case_windows_count(self, tmp_path):
        lists = ct.with_call_terms(_lists(tmp_path), ("Wattelaar",))
        normalize_fn = ct.make_normalize_fn(lists)
        fragment = m.Fragment("x", 0, 600, ((30, "Akme"),))
        cases = [base.TuningCase("x", "Akme", "Acme", "fout", "")]
        text = "[00:30] Wattelaer hier\n[05:00] Wattelaer daar\n[05:30] niets"
        pairs = ct.outside_replacements(text, cases, fragment, normalize_fn)
        assert pairs == [("Wattelaer", "Wattelaar")]  # the 00:30 chunk is inside the window

    def test_no_list_means_no_extra_outside_replacements(self, tmp_path):
        normalize_fn = ct.make_normalize_fn(_lists(tmp_path))
        fragment = m.Fragment("x", 0, 600, ((30, "Akme"),))
        cases = [base.TuningCase("x", "Akme", "Acme", "fout", "")]
        assert ct.outside_replacements("[05:00] Wattelaer", cases, fragment, normalize_fn) == []


class TestAggregateAndReport:
    def _result(self, arm, label, after, outside, broken=0):
        return ct.ArmResult(arm, label, "f", 10, 5, after, 4, broken, 3, 6, 7, outside)

    def test_sums_and_extra_against_baseline(self):
        results = [
            self._result("a", "run1", 5, 2, broken=1),
            self._result("a", "run2", 4, 1, broken=1),
            self._result("b", "run1", 8, 5),
            self._result("b", "run2", 7, 4),
        ]
        aggs = ct.aggregate(results)
        assert [(a.arm, a.transcript_set, a.after_hits) for a in aggs] == [
            ("a", "run1", 5),
            ("a", "run2", 4),
            ("b", "run1", 8),
            ("b", "run2", 7),
        ]
        report = ct.render_report(aggs)
        assert "| a | 5/10 (5) | 4/10 (5) | 4.5 | 0/0 (0) | 0/0 (0) | 2 | 3 | +0 | n/a | n/a |" in report
        assert "| b | 8/10 (5) | 7/10 (5) | 7.5 | 0/0 (0) | 0/0 (0) | 0 | 9 | +6 | n/a | n/a |" in report

    def test_held_out_rate_and_own_cases_show_in_the_row(self):
        a = ct.ArmResult("e", "run1", "f", 10, 5, 8, 4, 0, 3, 6, 7, 1, own_scored=4, own_before=1, own_after=3)
        rows = [ct.HeldOutResult("e", "all", 2000, 3)]
        assert "| e | 8/10 (5) | 8.0 | 3/4 (1) | 0 | 1 | +1 | 1.500 | 3 |" in ct.render_report(ct.aggregate([a]), rows)

    def test_coverage_table_has_a_total_row(self):
        text = ct.render_coverage([("f1", ct.Coverage(4, 2, 5, 1, 1, 2, 1)), ("f2", ct.Coverage(6, 3, 4, 0, 0, 0, 3))])
        assert "| total | 9 | 10 | 5 (50%) | 4 | 1 | 1 | 2 |" in text


class TestTranscriptSpecs:
    def test_parse(self):
        assert ct.parse_transcript_specs(["run1=/a", "live=/b"]) == {"run1": Path("/a"), "live": Path("/b")}

    @pytest.mark.parametrize("bad", ["run1", "=/a", "run1="])
    def test_malformed_spec_is_refused(self, bad):
        with pytest.raises(ValueError, match="LABEL=DIR"):
            ct.parse_transcript_specs([bad])


class TestMainEndToEnd:
    def _setup(self, tmp_path: Path, source_date: str = "2026-04-01"):
        (tmp_path / "tuning.md").write_text(TUNING, encoding="utf-8")
        (tmp_path / "listening.md").write_text(LISTENING, encoding="utf-8")
        (tmp_path / "norm.yaml").write_text(NORMALIZATION_YAML, encoding="utf-8")
        klant = tmp_path / "lists" / "b" / "acmecorp"
        klant.mkdir(parents=True)
        terms = ["Akme -> Acme", "Bolt", "Zork -> Zorg"]
        (klant / "klant.yaml").write_text(
            yaml.safe_dump({"bedrijf": "Acme", "termen": terms, "contactpersonen": ["Jan Jansen"]}), encoding="utf-8"
        )
        prov = {
            "call_datetime": "2026-04-09T10:00",
            "entries": [
                {"term": t, "source": "briefing", "source_datetime": source_date} for t in terms + ["Jan Jansen"]
            ],
        }
        (klant / "provenance.json").write_text(json.dumps(prov), encoding="utf-8")
        for label in ("run1", "run2"):
            d = tmp_path / label
            d.mkdir()
            (d / "acmecorp.txt").write_text(TRANSCRIPT, encoding="utf-8")
        return [
            "--tuning-set",
            str(tmp_path / "tuning.md"),
            "--listening-list",
            str(tmp_path / "listening.md"),
            "--lists-dir",
            str(tmp_path / "lists"),
            "--transcripts",
            f"run1={tmp_path / 'run1'}",
            f"run2={tmp_path / 'run2'}",
            "--out-dir",
            str(tmp_path / "out"),
            "--normalization-config",
            str(tmp_path / "norm.yaml"),
        ]

    def test_all_arms_write_per_fragment_output_and_report(self, tmp_path, capsys):
        assert ct.main(self._setup(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "| a | 1/3 (1) | 1/3 (1) |" in out
        assert "| b | 3/3 (1) | 3/3 (1) |" in out
        assert "| c | 2/3 (1) | 2/3 (1) |" in out  # Zork -> Zorg is not reachable from a 4-letter term
        assert "| c-oracle | 3/3 (1) | 3/3 (1) |" in out
        assert "| total | 3 | 3 | 3 (100%) |" in out
        written = json.loads((tmp_path / "out" / "per-fragment" / "acmecorp.json").read_text(encoding="utf-8"))
        assert {r["arm"] for r in written} == set(ct.ARMS) - set(ct.OWN_ARMS)
        assert (tmp_path / "lists" / "c" / "acmecorp" / "klant.yaml").is_file()
        assert (tmp_path / "lists" / "c-oracle" / "acmecorp" / "klant.yaml").is_file()
        assert (tmp_path / "out" / "provenance-checked" / "acmecorp-b.json").is_file()
        assert not list((tmp_path / "out").rglob("*.tmp"))

    def test_late_source_aborts_before_any_score(self, tmp_path):
        argv = self._setup(tmp_path, source_date="2026-04-10")
        with pytest.raises(ct.ProvenanceError):
            ct.main(argv + ["--arms", "b"])
        assert not (tmp_path / "out" / "per-fragment").exists()

    def test_disabled_normalization_is_refused(self, tmp_path, capsys):
        argv = self._setup(tmp_path)
        (tmp_path / "norm.yaml").write_text("enabled: false\n", encoding="utf-8")
        assert ct.main(argv) == 2
        assert "refusing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Own-side arms (e, e+b, g)
# ---------------------------------------------------------------------------

OWN_ENTRIES = ["Wattelaar", "Jan Jansen", "Akme -> Acme"]


def _write_eigen(directory: Path, entries=None, source_date="2026-04-01T09:00:00", frozen_at="2026-04-10T10:00:00"):
    directory.mkdir(parents=True, exist_ok=True)
    entries = OWN_ENTRIES if entries is None else entries
    klant = yaml.safe_dump({"bedrijf": "Own side", "termen": entries}, allow_unicode=True, sort_keys=False)
    prov = json.dumps({"entries": [{"term": e, "source": "handbook", "source_datetime": source_date} for e in entries]})
    (directory / "klant.yaml").write_text(klant, encoding="utf-8")
    (directory / "provenance.json").write_text(prov, encoding="utf-8")
    freeze = {
        "frozen_at": frozen_at,
        "klant_yaml_sha256": hashlib.sha256(klant.encode()).hexdigest(),
        "provenance_sha256": hashlib.sha256(prov.encode()).hexdigest(),
    }
    (directory / "freeze.json").write_text(json.dumps(freeze), encoding="utf-8")
    return directory


class TestEigenList:
    def test_loads_a_frozen_list_with_provenance(self, tmp_path):
        eigen = ct.load_eigen_list(_write_eigen(tmp_path / "e"))
        assert eigen.entries == tuple(OWN_ENTRIES)
        assert len(eigen.provenance) == 3
        assert eigen.frozen_at == "2026-04-10T10:00:00"
        assert eigen.short_terms() == 0

    def test_short_terms_counts_bare_terms_under_five_characters(self, tmp_path):
        eigen = ct.load_eigen_list(_write_eigen(tmp_path / "e", ["Wattelaar", "Bolt", "Zork -> Zorg"]))
        assert eigen.short_terms() == 1

    def test_changed_list_is_refused(self, tmp_path):
        directory = _write_eigen(tmp_path / "e")
        with (directory / "klant.yaml").open("a", encoding="utf-8") as handle:
            handle.write("- Extra\n")
        with pytest.raises(ct.FrozenListError, match="klant.yaml"):
            ct.load_eigen_list(directory)

    def test_changed_provenance_is_refused(self, tmp_path):
        directory = _write_eigen(tmp_path / "e")
        (directory / "provenance.json").write_text(json.dumps({"entries": []}), encoding="utf-8")
        with pytest.raises(ct.FrozenListError, match="provenance.json"):
            ct.load_eigen_list(directory)

    def test_missing_freeze_record_is_refused(self, tmp_path):
        directory = _write_eigen(tmp_path / "e")
        (directory / "freeze.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ct.FrozenListError, match="freeze.json"):
            ct.load_eigen_list(directory)

    def test_source_saved_after_the_freeze_is_refused(self, tmp_path):
        directory = _write_eigen(tmp_path / "e", source_date="2026-04-10T11:00:00")
        with pytest.raises(ct.ProvenanceError, match="at or after"):
            ct.load_eigen_list(directory)

    def test_term_without_provenance_is_refused(self, tmp_path):
        directory = _write_eigen(tmp_path / "e")
        prov = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
        prov["entries"].pop()
        text = json.dumps(prov)
        (directory / "provenance.json").write_text(text, encoding="utf-8")
        freeze = json.loads((directory / "freeze.json").read_text(encoding="utf-8"))
        freeze["provenance_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        (directory / "freeze.json").write_text(json.dumps(freeze), encoding="utf-8")
        with pytest.raises(ct.ProvenanceError, match="no provenance"):
            ct.load_eigen_list(directory)


class TestPairs:
    def _write(self, tmp_path, items):
        path = tmp_path / "pairs.yaml"
        path.write_text(yaml.safe_dump({"pairs": items}), encoding="utf-8")
        return path

    def test_loads_and_normalizes_pairs(self, tmp_path):
        path = self._write(tmp_path, [{"pair": " Watelaar  ->  Wattelaar ", "source": "held-out session"}])
        assert ct.load_pairs(path) == ("Watelaar -> Wattelaar",)

    @pytest.mark.parametrize("pair", ["Wattelaar", "-> Wattelaar", "Watelaar ->"])
    def test_pair_needs_both_sides(self, tmp_path, pair):
        with pytest.raises(ct.ProvenanceError, match="source -> target"):
            ct.load_pairs(self._write(tmp_path, [{"pair": pair, "source": "x"}]))

    def test_pair_needs_a_source(self, tmp_path):
        with pytest.raises(ct.ProvenanceError, match="empty source"):
            ct.load_pairs(self._write(tmp_path, [{"pair": "a -> b", "source": " "}]))

    def test_file_without_pairs_list_is_refused(self, tmp_path):
        path = tmp_path / "pairs.yaml"
        path.write_text("pairs: nope\n", encoding="utf-8")
        with pytest.raises(ct.ProvenanceError, match="pairs list"):
            ct.load_pairs(path)


class TestOwnSideTargets:
    def test_canonical_forms_and_long_words_of_multiword_forms(self):
        targets = ct.own_side_targets(("Wattelaar", "Jan Jansen", "Akme -> Acme", "Van der Ven"))
        assert targets == {"wattelaar", "jan jansen", "jansen", "acme", "van der ven"}

    def test_empty_variant_side_is_ignored(self):
        assert ct.own_side_targets(("x -> ", "")) == frozenset()


class TestOwnSideScoring:
    def test_own_cases_are_counted_apart(self, tmp_path):
        cases, fragments = _cases(tmp_path)
        lists = ct.with_call_terms(_lists(tmp_path), ct.sanitize_call_terms(["Akme -> Acme", "Bolt"]))
        targets = ct.own_side_targets(("Acme", "Bolt"))
        result = ct.score_arm(
            "e", "run1", "acmecorp", TRANSCRIPT, cases, fragments["acmecorp"], lists, own_targets=targets
        )
        assert result.own_scored == 2  # Acme and Bolt, not Zorg
        assert (result.own_before, result.own_after) == (1, 2)
        assert result.cases_scored == 3

    def test_without_own_targets_nothing_counts_as_own(self, tmp_path):
        cases, fragments = _cases(tmp_path)
        result = ct.score_arm("a", "run1", "acmecorp", TRANSCRIPT, cases, fragments["acmecorp"], _lists(tmp_path))
        assert (result.own_scored, result.own_after) == (0, 0)


class TestArmCallTermsOwn:
    def _b(self, tmp_path):
        klant = tmp_path / "lists" / "b" / "acmecorp"
        klant.mkdir(parents=True)
        (klant / "klant.yaml").write_text(
            yaml.safe_dump({"bedrijf": "Acme", "termen": ["Wattelaar", "Bolt"]}), encoding="utf-8"
        )
        return tmp_path / "lists"

    def test_arms_e_eb_g(self, tmp_path):
        eigen = ct.load_eigen_list(_write_eigen(tmp_path / "e"))
        lists_dir = self._b(tmp_path)
        assert ct.arm_call_terms("e", "acmecorp", [], lists_dir, eigen)[0] == tuple(OWN_ENTRIES)
        eb = ct.arm_call_terms("e+b", "acmecorp", [], lists_dir, eigen)[0]
        assert eb == ("Wattelaar", "Jan Jansen", "Akme -> Acme", "Bolt")  # duplicates dropped, own side first
        g = ct.arm_call_terms("g", "acmecorp", [], lists_dir, eigen, ("Watelaar -> Wattelaar",))[0]
        assert g == (*OWN_ENTRIES, "Watelaar -> Wattelaar")

    def test_own_arm_without_a_list_is_refused(self, tmp_path):
        with pytest.raises(ct.FrozenListError, match="--eigen-dir"):
            ct.arm_call_terms("e", "acmecorp", [], tmp_path)


class TestHeldOut:
    def _selection(self, texts):
        sessions = [base.HeldOutSession(f"s{i}", t, len(t.split())) for i, t in enumerate(texts)]
        return base.HeldOutSelection(sessions, 0, 0, 0)

    def test_replacements_per_1000_words_and_pairs(self, tmp_path):
        selection = self._selection(["Wattelaer is er", "niets te doen hier"])
        lists = ct.with_call_terms(_lists(tmp_path), ("Wattelaar",))
        result = ct.held_out_false_corrections("e", "all", selection, lists)
        assert (result.words, result.replacements) == (7, 1)
        assert result.pairs == {("Wattelaer", "Wattelaar"): 1}
        assert result.per_1000 == pytest.approx(1000 / 7)

    def test_no_list_gives_no_false_corrections(self, tmp_path):
        result = ct.held_out_false_corrections("a", "all", self._selection(["Wattelaer is er"]), _lists(tmp_path))
        assert result.replacements == 0
        assert ct.HeldOutResult("a", "all", 0, 0).per_1000 == 0.0

    def test_rate_averages_per_fragment_lists_and_reads_a_shared_one_once(self):
        rows = [
            ct.HeldOutResult("e+b", "f1", 1000, 2),
            ct.HeldOutResult("e+b", "f2", 1000, 4),
            ct.HeldOutResult("e", "all", 2000, 2),
        ]
        assert ct.held_out_rate(rows, "e+b") == (3.0, 6)
        assert ct.held_out_rate(rows, "e") == (1.0, 2)
        assert ct.held_out_rate(rows, "g") is None

    def test_collisions_table_marks_replacements_that_land_on_the_own_side(self):
        rows = [
            ct.HeldOutResult("e", "all", 100, 3, ct.Counter({("Wattelaer", "Wattelaar"): 2, ("tafel", "table"): 1}))
        ]
        text = ct.render_collisions(rows, ("Wattelaar",))
        assert "| e | Wattelaer -> Wattelaar | 2 | yes |" in text
        assert "| e | tafel -> table | 1 | no |" in text


class TestCollisionProbe:
    def test_flags_words_rewritten_into_an_own_side_term(self, tmp_path):
        lists = ct.with_call_terms(_lists(tmp_path), ("Wattelaar",))
        assert ct.collision_probe(lists, ["Wattelaer", "Bolt", "Wattelaer", ""]) == [
            ("Wattelaer", "Wattelaar"),
            ("Bolt", "Boot"),
        ]

    def test_rewrites_the_fixed_layer_makes_anyway_are_not_the_lists_fault(self, tmp_path):
        base_lists = _lists(tmp_path)
        lists = ct.with_call_terms(base_lists, ("Wattelaar",))
        assert ct.collision_probe(lists, ["Wattelaer", "Bolt"], base_lists) == [("Wattelaer", "Wattelaar")]

    def test_clean_words_give_no_hits(self, tmp_path):
        assert ct.collision_probe(ct.with_call_terms(_lists(tmp_path), ("Wattelaar",)), ["tafel"]) == []


class TestMainOwnArms:
    def _argv(self, tmp_path):
        helper = TestMainEndToEnd()
        argv = helper._setup(tmp_path)
        _write_eigen(tmp_path / "eigen", ["Wattelaar", "Akme -> Acme"])
        pairs = tmp_path / "pairs.yaml"
        pairs.write_text(
            yaml.safe_dump({"pairs": [{"pair": "Zork -> Zorg", "source": "held-out session"}]}), encoding="utf-8"
        )
        db = tmp_path / "cases.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE call_sessions (id TEXT, prospect_name TEXT, transcript TEXT)")
        text = " ".join(["Wattelaer ligt hier"] + ["woord"] * 59)
        conn.execute("INSERT INTO call_sessions VALUES ('s1', '', ?)", (json.dumps([{"text": text}]),))
        conn.commit()
        conn.close()
        return argv, tmp_path / "eigen", pairs, db

    def test_own_arms_report_own_cases_and_held_out_false_corrections(self, tmp_path, capsys):
        argv, eigen, pairs, db = self._argv(tmp_path)
        extra = ["--eigen-dir", str(eigen), "--pairs", str(pairs), "--held-out-db", str(db)]
        assert ct.main(argv + extra + ["--arms", "a", "e", "e+b", "g"]) == 0
        out = capsys.readouterr().out
        assert "| a | 1/3 (1) | 1/3 (1) | 1.0 | 1/1 (0) | 1/1 (0) | 2 | 0 | +0 | 0.000 | 0 |" in out
        assert "| e+b | 3/3 (1) | 3/3 (1) | 3.0 | 1/1 (0) | 1/1 (0) | 0 | 0 | +0 | 16.129 | 1 |" in out
        assert "| g | 2/3 (1) | 2/3 (1) | 2.0 |" in out
        assert "held-out corpus: 1 sessions, 62 words" in out
        assert "| e | Wattelaer -> Wattelaar | 1 | yes |" in out
        assert "list e: 2 entries, 0 bare terms under 5 characters, frozen 2026-04-10T10:00:00" in out
        written = json.loads((tmp_path / "out" / "per-fragment" / "acmecorp.json").read_text(encoding="utf-8"))
        assert {r["arm"] for r in written} == {"a", "e", "e+b", "g"}
        assert (tmp_path / "out" / "held-out-pairs.json").is_file()
        e_rows = [r for r in written if r["arm"] == "e"]
        assert all(r["own_scored"] == 1 and r["own_after"] == 1 for r in e_rows)

    def test_default_arms_follow_the_inputs_given(self, tmp_path, capsys):
        argv, eigen, pairs, _db = self._argv(tmp_path)
        assert ct.main(argv + ["--eigen-dir", str(eigen)]) == 0
        written = json.loads((tmp_path / "out" / "per-fragment" / "acmecorp.json").read_text(encoding="utf-8"))
        arms = {r["arm"] for r in written}
        assert {"e", "e+b"} <= arms and "g" not in arms

    def test_own_arm_without_eigen_dir_is_refused(self, tmp_path, capsys):
        argv, *_ = self._argv(tmp_path)
        assert ct.main(argv + ["--arms", "e"]) == 2
        assert "--eigen-dir" in capsys.readouterr().err

    def test_arm_g_without_pairs_is_refused(self, tmp_path, capsys):
        argv, eigen, *_ = self._argv(tmp_path)
        assert ct.main(argv + ["--eigen-dir", str(eigen), "--arms", "g"]) == 2
        assert "--pairs" in capsys.readouterr().err

    def test_tampered_list_aborts_before_any_score(self, tmp_path):
        argv, eigen, *_ = self._argv(tmp_path)
        with (eigen / "klant.yaml").open("a", encoding="utf-8") as handle:
            handle.write("- Sneaky\n")
        with pytest.raises(ct.FrozenListError):
            ct.main(argv + ["--eigen-dir", str(eigen), "--arms", "e"])
        assert not (tmp_path / "out" / "per-fragment").exists()
