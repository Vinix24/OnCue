"""Tests for modules/transcriber/normalize.py (D2 of transcript-normalisatie-na-asr)."""

from __future__ import annotations

import time
from pathlib import Path

from sales_copilot.modules.transcriber import normalize as norm


def _lists(**overrides) -> norm.NormalizationLists:
    # Every term below is synthetic (no real client, company, or person name):
    # this file is part of the public export, and the whole point of the
    # fix-forward on #253 (D-3cbcb3ba) was to keep real names out of exactly
    # this kind of fixture.
    defaults = dict(
        enabled=True,
        terms=("HubSpot", "Optifra", "Kruisbergen", "DataForge"),
        variants={
            "HupSpot": "HubSpot",
            "Ruffspot": "HubSpot",
            "ROV": "ROI",
            "IDE": "EDI",
            "IDI": "EDI",
            "VWA": "VBA",
            "VWBA": "VBA",
            "MKD": "MKB",
            "Adrian Voss": "Adriaan Voss",
        },
    )
    defaults.update(overrides)
    return norm.NormalizationLists(**defaults)


class TestExactVariant:
    def test_single_token_exact_variant_is_replaced(self) -> None:
        text, reps = norm.normalize("we praten met HupSpot vandaag", _lists())
        assert text == "we praten met HubSpot vandaag"
        assert reps == [("HupSpot", "HubSpot", "variant")]

    def test_short_term_only_matches_exact_never_fuzzy(self) -> None:
        # "IDE" -> "EDI" is an explicit exact mapping; a near-miss like "IDF"
        # (not in the variant table) must NOT be fuzzy-corrected because EDI
        # is only 3 characters (below the fuzzy floor).
        text, reps = norm.normalize("we gebruiken IDF voor dat proces", _lists())
        assert text == "we gebruiken IDF voor dat proces"
        assert reps == []

    def test_multi_word_phrase_variant_matches_fixed_adjacent_tokens(self) -> None:
        text, reps = norm.normalize("opgericht vanuit Adrian Voss gisteren", _lists())
        assert text == "opgericht vanuit Adriaan Voss gisteren"
        assert reps == [("Adrian Voss", "Adriaan Voss", "variant")]

    def test_phrase_variant_does_not_fire_on_first_word_alone(self) -> None:
        text, reps = norm.normalize("Adrian was er niet bij", _lists())
        assert text == "Adrian was er niet bij"
        assert reps == []


class TestMerge:
    def test_two_tokens_merge_into_canonical_term(self) -> None:
        text, reps = norm.normalize("dat komt van Data Forge vandaan", _lists())
        assert text == "dat komt van DataForge vandaan"
        assert reps == [("Data Forge", "DataForge", "merge")]

    def test_merge_does_not_fire_when_concatenation_is_not_a_term(self) -> None:
        text, reps = norm.normalize("Data draait dit kwartaal goed", _lists())
        assert text == "Data draait dit kwartaal goed"
        assert reps == []


class TestFuzzy:
    def test_edit_distance_one_on_long_term_is_corrected(self) -> None:
        text, reps = norm.normalize("we zijn onderdeel van Optifr geworden", _lists())
        assert text == "we zijn onderdeel van Optifra geworden"
        assert reps == [("Optifr", "Optifra", "fuzzy")]

    def test_edit_distance_two_on_wide_term_is_corrected(self) -> None:
        text, reps = norm.normalize("ergens in Kruisberge gevestigd", _lists())
        assert text == "ergens in Kruisbergen gevestigd"
        assert reps == [("Kruisberge", "Kruisbergen", "fuzzy")]

    def test_short_word_never_considered_for_fuzzy(self) -> None:
        # "Hub" is far under the 5-character fuzzy floor.
        text, reps = norm.normalize("de Hub staat centraal", _lists())
        assert text == "de Hub staat centraal"
        assert reps == []

    def test_unrelated_word_of_similar_length_is_untouched(self) -> None:
        text, reps = norm.normalize("we plannen dit voor volgende week", _lists())
        assert text == "we plannen dit voor volgende week"
        assert reps == []


class TestDisabledAndEmpty:
    def test_disabled_config_returns_text_unchanged(self) -> None:
        text, reps = norm.normalize("HupSpot komt langs", _lists(enabled=False))
        assert text == "HupSpot komt langs"
        assert reps == []

    def test_empty_text_returns_unchanged(self) -> None:
        text, reps = norm.normalize("", _lists())
        assert text == ""
        assert reps == []


class TestLoadNormalizationLists:
    def test_missing_file_returns_disabled_config(self, tmp_path: Path) -> None:
        lists = norm.load_normalization_lists(tmp_path / "does-not-exist.yaml")
        assert lists.enabled is False
        assert lists.terms == ()
        assert lists.variants == {}

    def test_malformed_yaml_returns_disabled_config(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("not: a: valid: mapping: [", encoding="utf-8")
        lists = norm.load_normalization_lists(path)
        assert lists.enabled is False

    def test_loads_terms_and_variants(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "enabled: true\nterms:\n  - Acme\nvariants:\n  Akme: Acme\n",
            encoding="utf-8",
        )
        lists = norm.load_normalization_lists(path)
        assert lists.enabled is True
        assert lists.terms == ("Acme",)
        assert lists.variants == {"Akme": "Acme"}

    def test_default_config_file_loads_and_is_enabled(self) -> None:
        lists = norm.default_normalization_lists()
        assert lists.enabled is True
        assert "HubSpot" in lists.terms
        assert lists.variants  # non-empty


class TestLatency:
    def test_normalize_is_fast_on_a_300_char_chunk(self) -> None:
        lists = norm.default_normalization_lists()
        chunk = (
            "we hebben net gesproken over de VWBA en de MKD rapportage en de ROV "
            "en de HupSpot integratie die volgende maand live gaat, plus nog wat "
            "vervolgstappen die we samen hebben afgesproken voor het volgende "
            "kwartaal en de begroting die daarbij hoort, en ook nog een korte "
            "terugkoppeling naar het management over de planning"
        )
        assert len(chunk) >= 250

        start = time.perf_counter()
        for _ in range(20):
            norm.normalize(chunk, lists)
        elapsed_per_call_ms = (time.perf_counter() - start) / 20 * 1000

        assert elapsed_per_call_ms < 5.0
