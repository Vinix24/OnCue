"""termenlijst-in-uitwerking D2: the production term-correction module.

``sales_copilot.modules.reports.term_corrections`` holds the prefilter and the validation that
D1 measured in ``scripts/measure_term_corrections.py``; these are the same cases the harness
tests covered there (position anchor, list and candidate validation, source check, word loss,
merging allowed, a PII-changed count refused, "Roy" in another segment staying put), now
against the production code. Plus what only production has: the numbered request with its
candidates lines, the parse of what the model saw after the PII policy, and the term list of
one call (fixed list, ``klant.yaml``, ``termenlijst.yaml``) with its validation and path safety.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from sales_copilot.core import context_docs
from sales_copilot.core.klant_config import KlantConfigError, load_termenlijst
from sales_copilot.core.outbound_policy import apply_outbound_pii
from sales_copilot.core.pii_filter import redact_pii
from sales_copilot.modules.reports import term_corrections as tc
from sales_copilot.modules.transcriber.normalize import NormalizationLists

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


@pytest.fixture(autouse=True)
def _cloud_only_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default cloud path: PII stripped before a public provider."""
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)


def _fix(source: str, occurrence: int, target: str, segment_index: int = 0) -> tc.TermCorrection:
    return tc.TermCorrection(
        segment_index=segment_index, source=source, occurrence=occurrence, target=target, reason="context"
    )


# ---------------------------------------------------------------------------
# Position anchor
# ---------------------------------------------------------------------------


def test_locate_occurrence_counts_whole_words_only() -> None:
    text = "Roy belt, Royal niet, en Roy weer"

    assert tc.find_occurrences(text, "Roy") == [(0, 3), (25, 28)]
    assert tc.locate_occurrence(text, "Roy", 2) == (25, 28)
    assert tc.locate_occurrence(text, "Roy", 3) is None
    assert tc.locate_occurrence(text, "Roy", 0) is None
    assert tc.find_occurrences(text, "  ") == []


def test_only_the_named_occurrence_is_placed() -> None:
    originals = ["Is het via ROI gegaan? Via ROI is het gegaan."]

    accepted, rejected = tc.validate_corrections(
        [_fix("ROI", 2, "Roy")], originals, originals, [{"Roy"}], tc.TermList(TERMS)
    )

    assert rejected == {}
    assert accepted == [tc.AppliedCorrection(segment_index=0, start=27, end=30, target="Roy")]


def test_roy_in_another_segment_stays() -> None:
    originals = ["Is het via ROI gegaan?", "Roy is het hoofd verkoop."]
    offered = [{"Roy", "ROI"}, {"Roy"}]

    accepted, rejected = tc.validate_corrections(
        [_fix("ROI", 1, "Roy", 0), _fix("Roy", 1, "ROI", 0)], originals, list(originals), offered, tc.TermList(TERMS)
    )

    assert rejected == {"source_not_at_position": 1}
    assert accepted == [tc.AppliedCorrection(segment_index=0, start=11, end=14, target="Roy")]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("correction", "reason"),
    [
        (_fix("ROI", 1, "Rooi"), "target_not_in_list"),
        (_fix("ROI", 1, "VBA"), "target_not_candidate"),
        (_fix("ROI", 3, "Roy"), "source_not_at_position"),
        (_fix("Rob", 1, "Roy"), "source_not_at_position"),
        (_fix("ROI", 1, "Roy", 1), "segment_out_of_range"),
        (_fix("ROI", 1, "Roy", -1), "segment_out_of_range"),
        (_fix("Roy", 1, "Roy"), "no_change"),
        (_fix(" ", 1, "Roy"), "no_change"),
    ],
)
def test_invalid_corrections_are_refused(correction: tc.TermCorrection, reason: str) -> None:
    originals = ["via ROI en via ROI"]

    accepted, rejected = tc.validate_corrections(
        [correction], originals, originals, [{"Roy", "ROI"}], tc.TermList(TERMS)
    )

    assert accepted == []
    assert rejected == {reason: 1}


def test_overlapping_corrections_keep_the_first() -> None:
    originals = ["uit Team Leader kwam het signaal"]

    accepted, rejected = tc.validate_corrections(
        [_fix("Team Leader", 1, "Teamleader"), _fix("Leader", 1, "Roy")],
        originals,
        originals,
        [{"Teamleader", "Roy"}],
        tc.TermList(TERMS),
    )

    assert [c.target for c in accepted] == ["Teamleader"]
    assert rejected == {"overlap": 1}


def test_target_is_matched_case_insensitively_to_the_list() -> None:
    originals = ["uit Team Leader kwam het"]

    accepted, _ = tc.validate_corrections(
        [_fix("Team Leader", 1, "teamleader")], originals, originals, [{"Teamleader"}], tc.TermList(TERMS)
    )

    assert accepted[0].target == "Teamleader"


def test_a_count_the_pii_strip_changed_is_refused_and_the_position_stays_on_the_original() -> None:
    original = "Bram zegt ROI en daarna weer ROI."
    visible = apply_outbound_pii(original, provider="openrouter", allow_local=True)

    accepted, rejected = tc.validate_corrections(
        [_fix("ROI", 2, "Roy"), _fix("Bram", 1, "Roy")], [original], [visible], [{"Roy"}], tc.TermList(TERMS)
    )

    assert visible.startswith("[NAAM] zegt ROI")
    assert rejected == {"source_changed_by_pii": 1}
    assert [(c.start, c.end) for c in accepted] == [tc.locate_occurrence(original, "ROI", 2)]
    assert tc.locate_occurrence(visible, "ROI", 2) != tc.locate_occurrence(original, "ROI", 2)


# ---------------------------------------------------------------------------
# The word-loss guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "lost"),
    [
        ("3 VBA", "VBA", True),  # a number before a correct acronym: the number would vanish
        ("VWA Excel", "VBA", True),  # an adjacent word taken along: it would vanish
        ("Dobby jongen", "hobby", True),
        ("de Hele Sjebang", "de", True),
        ("ROI", "", True),
        ("Deal Flow", "Dealflow", False),  # adjacent tokens written together
        ("Team Leader", "Teamleader", False),
        ("ROI", "Roy", False),  # a plain replacement
        ("Marten Smith", "Marten Smit", False),  # the shared first word is set aside
        ("de Hele Sjebang", "de hele shebang", False),
        ("VWA", "VBA Excel", False),
    ],
)
def test_loses_words(source: str, target: str, lost: bool) -> None:
    assert tc.loses_words(source, target) is lost


def test_merging_beyond_the_fuzzy_threshold_is_a_loss() -> None:
    assert tc.loses_words("Deal Flow", "Dealflow") is False
    assert tc.loses_words("Teal Glow", "Dealflow") is False  # distance 2, the threshold of an 8-letter word
    assert tc.loses_words("Real Grow", "Dealflow") is True  # distance 3


@pytest.mark.parametrize(
    ("text", "correction"),
    [
        ("daar zit 3 VBA in", _fix("3 VBA", 1, "VBA")),
        ("hoe zeg je dat, VWA Excel", _fix("VWA Excel", 1, "VBA")),
    ],
)
def test_a_lost_word_is_refused(text: str, correction: tc.TermCorrection) -> None:
    accepted, rejected = tc.validate_corrections([correction], [text], [text], [{"VBA"}], tc.TermList(TERMS))

    assert accepted == []
    assert rejected == {"word_loss": 1}


def test_merging_and_replacing_are_accepted() -> None:
    originals = ["uit Team Leader kwam het, via ROI"]

    accepted, rejected = tc.validate_corrections(
        [_fix("Team Leader", 1, "Teamleader"), _fix("ROI", 1, "Roy")],
        originals,
        originals,
        [{"Teamleader", "Roy"}],
        tc.TermList(TERMS),
    )

    assert rejected == {}
    assert [(originals[0][c.start : c.end], c.target) for c in accepted] == [
        ("Team Leader", "Teamleader"),
        ("ROI", "Roy"),
    ]


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "present", "absent"),
    [
        ("Is het via ROI gegaan?", {"ROI", "Roy"}, set()),
        ("Roy is het hoofd verkoop.", {"Roy"}, {"ROI"}),
        ("de roi van dit project", set(), {"ROI"}),
        ("uit Team Leader kwam het", {"Teamleader"}, set()),
        ("vanuit Marten Smith opgericht", {"Marten Smit"}, set()),
        ("een smith zonder voornaam", set(), {"Marten Smit"}),
        ("hoe zeg je dat, VWA Excel", {"VBA"}, set()),
        ("dat die naar die IDE gaat", {"EDI"}, set()),
        ("het draait bij Zentrix", {"Sentrix"}, set()),
        ("chatbot van Brite Wave", {"Brightwave"}, set()),
        ("een gewone zin zonder termen", set(), set(TERMS)),
    ],
)
def test_prefilter_candidates(text: str, present: set[str], absent: set[str]) -> None:
    candidates = set(tc.TermPrefilter(TERMS).candidates(text))

    assert present <= candidates
    assert not candidates & absent


def test_prefilter_keeps_list_order_and_shares_its_cache() -> None:
    prefilter = tc.TermPrefilter(TERMS)

    assert prefilter.candidates("Roy of ROI") == ("ROI", "Roy")
    assert prefilter.scan(["Roy of ROI", "niets hier"]) == [("ROI", "Roy"), ()]


# ---------------------------------------------------------------------------
# The request and what the model saw of it
# ---------------------------------------------------------------------------


def _plan() -> tc.TermCorrectionPlan:
    return tc.plan_term_corrections(
        [
            (0, "Seller", "Is het via ROI gegaan, vroeg Bram?"),
            (2, "Prospect", "Prima, dank je."),
            (3, "Unknown", "Team Leader"),
        ],
        TERMS,
    )


def test_plan_numbers_segments_by_their_report_index_and_lists_only_their_candidates() -> None:
    plan = _plan()

    assert [s.index for s in plan.segments] == [0, 2, 3]
    assert plan.segments[1].candidates == ()
    assert plan.has_candidates
    assert tc.numbered_transcript(plan.segments).split("\n") == [
        "segment 0 (Seller): Is het via ROI gegaan, vroeg Bram?",
        "candidates for segment 0: ROI; Roy; Bram",
        "segment 2 (Prospect): Prima, dank je.",
        "segment 3 (Unknown): Team Leader",
        "candidates for segment 3: Teamleader",
    ]


def test_a_plan_without_any_resemblance_has_no_candidates() -> None:
    plan = tc.plan_term_corrections([(0, "Seller", "een gewone zin")], TERMS)

    assert not plan.has_candidates


def test_parse_visible_reads_back_what_the_model_saw() -> None:
    plan = _plan()
    visible = apply_outbound_pii(tc.numbered_transcript(plan.segments), provider="openrouter", allow_local=True)

    parsed = tc.parse_visible(visible, plan.segments)

    assert parsed[0] == tc.VisibleSegment(
        text="Is het via ROI gegaan, vroeg [NAAM]?", candidates=("ROI", "Roy", "[NAAM]")
    )
    assert parsed[2] == tc.VisibleSegment(text="Prima, dank je.", candidates=())
    assert parsed[3].candidates == ("Teamleader",)


def test_the_lower_case_labels_survive_a_surname_spanning_the_line_break() -> None:
    """A segment ending on 'Bureau van de' must not swallow the next segment's label."""
    segments = (
        tc.PromptSegment(index=0, speaker="Seller", text="ik ging naar Bureau van de", candidates=()),
        tc.PromptSegment(index=1, speaker="Prospect", text="Peters zei het", candidates=()),
    )
    text = tc.numbered_transcript(segments)

    assert set(tc.parse_visible(redact_pii(text)[0], segments)) == {0, 1}
    # The same text with a capitalised label is exactly what the surname pattern swallows.
    assert "Segment 1" not in redact_pii(text.replace("segment ", "Segment "))[0]


@pytest.mark.parametrize(
    "visible",
    [
        "segment 0 (Seller): Is het via ROI gegaan, vroeg [NAAM]?\nsegment 2 (Prospect): Prima, dank je.",
        "segment 0 (Seller): tekst [NAAM] 2 (Prospect): Prima\nsegment 3 (Unknown): Team Leader",
        "",
    ],
)
def test_a_structure_breaking_redaction_is_refused(visible: str) -> None:
    with pytest.raises(ValueError, match="segment structure"):
        tc.parse_visible(visible, _plan().segments)


def test_review_accepts_on_the_original_and_counts_the_pii_limited_segments() -> None:
    plan = _plan()
    originals = ["Is het via ROI gegaan, vroeg Bram?", "", "Prima,  dank je.", "Team Leader"]
    visible = apply_outbound_pii(tc.numbered_transcript(plan.segments), provider="openrouter", allow_local=True)

    review = tc.review_term_corrections(
        [
            _fix("ROI", 1, "Roy", 0),
            _fix("Team Leader", 1, "teamleader", 3),
            _fix("Bram", 1, "Brightwave", 0),  # in the list, but not offered for segment 0
            _fix("Prima", 1, "Roy", 2),  # segment 2 had no candidates
            _fix("Roy", 1, "ROI", 1),  # segment 1 was never sent
        ],
        plan,
        originals,
        visible,
    )

    assert [(c.segment_index, c.source, c.target, c.reason) for c in review.accepted] == [
        (0, "ROI", "Roy", "context"),
        (3, "Team Leader", "Teamleader", "context"),
    ]
    assert review.rejected == {"target_not_candidate": 3}
    assert review.pii_limited == 1


def test_review_refuses_everything_when_the_structure_broke() -> None:
    review = tc.review_term_corrections([_fix("Roy", 1, "ROI", 0)], _plan(), ["a", "", "b", "c"], "kapot")

    assert review.accepted == ()
    assert review.rejected == {"structure_changed_by_pii": 1}
    assert review.pii_limited == 2


def test_pii_off_leaves_the_request_and_its_candidates_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    plan = _plan()
    text = tc.numbered_transcript(plan.segments)

    review = tc.review_term_corrections(
        [_fix("Bram", 1, "Bram", 0)],
        plan,
        ["Is het via ROI gegaan, vroeg Bram?", "", "x", "Team Leader"],
        apply_outbound_pii(text, provider="openrouter"),
    )

    assert apply_outbound_pii(text, provider="openrouter") == text
    assert review.pii_limited == 0
    assert review.rejected == {"no_change": 1}


# ---------------------------------------------------------------------------
# The term list of one call
# ---------------------------------------------------------------------------


def _client(root: Path, slug: str = "acme", *, klant: str | None = None, termenlijst: str | None = None) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    if klant is not None:
        (directory / "klant.yaml").write_text(klant, encoding="utf-8")
    if termenlijst is not None:
        (directory / "termenlijst.yaml").write_text(termenlijst, encoding="utf-8")
    return directory


@pytest.fixture
def fixed_list(tmp_path: Path) -> Path:
    path = tmp_path / "normalization.yaml"
    path.write_text(
        "enabled: true\nterms: [Teamleader, Dealflow]\nvariants:\n  Deel Flow: Dealflow\n", encoding="utf-8"
    )
    return path


def test_fixed_term_entries_carry_terms_and_variants() -> None:
    lists = NormalizationLists(enabled=True, terms=("Teamleader",), variants={"Team Lieder": "Teamleader"})

    assert tc.fixed_term_entries(lists) == ("Teamleader", "Team Lieder -> Teamleader")
    assert tc.fixed_term_entries(NormalizationLists(enabled=False, terms=("X",), variants={})) == ()


def test_merge_keeps_order_drops_duplicates_and_unusable_entries(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    merged = tc.merge_term_entries(
        ["ROI", " Teamleader "], ["ROI; Roy", "Sofi\nBram", "Teamleader"], ["ROI", "Dealflow"]
    )

    assert merged == ("ROI", "Teamleader", "Dealflow")
    assert "2 entries with a line break or ';' left out" in caplog.text
    assert "Roy" not in caplog.text and "Bram" not in caplog.text


def test_load_report_terms_merges_the_three_sources(tmp_path: Path, fixed_list: Path) -> None:
    root = tmp_path / "klanten"
    _client(
        root,
        klant="bedrijf: Acme BV\ntermen: [Wattelaar, VWA -> VBA]\n",
        termenlijst="termen: [Turbinewacht, Wattelaar, Teamleader]\n",
    )

    terms = tc.load_report_terms("acme", root=root, normalization_path=fixed_list)

    assert terms == ("Wattelaar", "VWA -> VBA", "Turbinewacht", "Teamleader", "Dealflow", "Deel Flow -> Dealflow")
    # No live cap: the same list feeds the prefilter in full.
    assert set(tc.TermPrefilter(terms).terms) >= {"Wattelaar", "VBA", "Turbinewacht", "Teamleader", "Dealflow"}


def test_load_report_terms_without_a_client_is_the_fixed_list(fixed_list: Path) -> None:
    assert tc.load_report_terms(None, normalization_path=fixed_list) == (
        "Teamleader",
        "Dealflow",
        "Deel Flow -> Dealflow",
    )


def test_load_report_terms_uses_the_default_root(
    tmp_path: Path, fixed_list: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    _client(tmp_path, termenlijst="termen: [Turbinewacht]\n")

    assert tc.load_report_terms("acme", normalization_path=fixed_list)[0] == "Turbinewacht"


def test_an_invalid_termenlijst_is_left_out_and_the_rest_still_counts(
    tmp_path: Path, fixed_list: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "klanten"
    _client(root, klant="bedrijf: Acme BV\ntermen: [Wattelaar]\n", termenlijst=f"termen: [{'x' * 65}]\n")
    caplog.set_level(logging.ERROR)

    terms = tc.load_report_terms("acme", root=root, normalization_path=fixed_list)

    assert terms == ("Wattelaar", "Teamleader", "Dealflow", "Deel Flow -> Dealflow")
    assert "term 1 is 65 tekens lang, maximum is 64" in caplog.text


def test_an_unsafe_slug_loads_no_client_terms(
    tmp_path: Path, fixed_list: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "klanten"
    root.mkdir()
    _client(tmp_path, slug="buiten", klant="bedrijf: X\ntermen: [Geheim]\n", termenlijst="termen: [Geheim]\n")
    caplog.set_level(logging.ERROR)

    terms = tc.load_report_terms("../buiten", root=root, normalization_path=fixed_list)

    assert "Geheim" not in terms
    assert "resolveert buiten KLANTEN_ROOT" in caplog.text


# ---------------------------------------------------------------------------
# termenlijst.yaml validation
# ---------------------------------------------------------------------------


def test_a_missing_termenlijst_is_empty(tmp_path: Path) -> None:
    _client(tmp_path)

    assert load_termenlijst("acme", root=tmp_path) == ()


def test_termenlijst_accepts_5000_terms_of_64_characters(tmp_path: Path) -> None:
    terms = [f"term{index:05d}".ljust(64, "x") for index in range(5000)]
    _client(tmp_path, termenlijst="termen:\n" + "".join(f"  - {term}\n" for term in terms))

    assert load_termenlijst("acme", root=tmp_path) == tuple(terms)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("termen:\n" + "".join(f"  - t{index}\n" for index in range(5001)), "5001 termen, maximum is 5000"),
        (f"termen: [ok, {'y' * 65}]\n", "term 2 is 65 tekens lang, maximum is 64"),
        ("termen: [ok, '']\n", "term 2 moet een niet-lege tekst zijn"),
        ("termen: [ok, 12]\n", "term 2 moet een niet-lege tekst zijn"),
        ("termen: ['ROI; Roy']\n", "term 1 bevat een regelovergang of ';'"),
        ('termen: ["ROI\\nRoy"]\n', "term 1 bevat een regelovergang of ';'"),
        ("termen: ROI\n", "veld 'termen' moet een lijst zijn"),
        ("- ROI\n", "verwacht een mapping met alleen het veld 'termen'"),
        ("termen: [ROI]\nextra: 1\n", "verwacht een mapping met alleen het veld 'termen'"),
        ("termen: [ROI\n", "geen geldige YAML"),
    ],
)
def test_termenlijst_validation(tmp_path: Path, content: str, message: str) -> None:
    _client(tmp_path, termenlijst=content)

    with pytest.raises(KlantConfigError, match=message.replace("[", r"\[")):
        load_termenlijst("acme", root=tmp_path)


def test_termenlijst_messages_never_quote_a_term(tmp_path: Path) -> None:
    _client(tmp_path, termenlijst=f"termen: [{'Geheimenaam' * 7}]\n")

    with pytest.raises(KlantConfigError) as exc_info:
        load_termenlijst("acme", root=tmp_path)

    assert "Geheimenaam" not in str(exc_info.value)


def test_a_termenlijst_over_one_mebibyte_is_refused(tmp_path: Path) -> None:
    _client(tmp_path, termenlijst="termen: [ROI]\n" + "#" * (1024 * 1024))

    with pytest.raises(KlantConfigError, match="maximum is 1048576"):
        load_termenlijst("acme", root=tmp_path)


def test_a_termenlijst_symlink_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "klanten"
    directory = _client(root)
    outside = tmp_path / "elders.yaml"
    outside.write_text("termen: [Geheim]\n", encoding="utf-8")
    os.symlink(outside, directory / "termenlijst.yaml")

    with pytest.raises(KlantConfigError, match="verwijst buiten KLANTEN_ROOT"):
        load_termenlijst("acme", root=root)


@pytest.mark.parametrize("slug", ["../acme", "/etc", ""])
def test_termenlijst_refuses_an_unsafe_slug(tmp_path: Path, slug: str) -> None:
    root = tmp_path / "klanten"
    root.mkdir()

    with pytest.raises(KlantConfigError):
        load_termenlijst(slug, root=root)
