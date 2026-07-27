from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.lang_router import LangRouter, SpeakerLangProfile
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter
from sales_copilot.modules.detector.router import PainPointRouter, _load_routes

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "objection_responses.yaml").exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)


@_requires_curated_pack
def test_multilang_route_files_load_with_expected_counts() -> None:
    for language in ("nl", "en", "de"):
        pain_routes = _load_routes("config/pain_points.yaml", language=language)
        objection_routes = _load_routes("config/objections.yaml", language=language)
        objection_responses = ObjectionDetector._load_responses(
            "config/objection_responses.yaml",
            language=language,
        )

        assert len(pain_routes) == 12
        assert len(objection_routes) == 5
        # The Dutch base file also carries the cold-call (acquisitie preset)
        # rebuttals; the localized en/de files hold only the sales set.
        assert len(objection_responses) == (14 if language == "nl" else 6)


def test_pain_point_router_classifies_language_specific_categories() -> None:
    config = DetectorConfig()

    nl_match = PainPointRouter(config, language="nl").classify("we doen offertes nog steeds in Word")
    en_match = PainPointRouter(config, language="en").classify("creating quotes takes us too much time")
    de_match = PainPointRouter(config, language="de").classify(
        "die Angebotserstellung kostet uns sehr viel Zeit"
    )

    assert nl_match is not None
    assert en_match is not None
    assert de_match is not None
    assert nl_match.category == "offerteproces"
    assert en_match.category == "quote_process"
    assert de_match.category == "angebotsprozess"


def test_objection_router_classifies_language_specific_categories() -> None:
    config = DetectorConfig(enable_objection_detection=True)

    nl_match = ObjectionRouter(config, language="nl").classify("het is te duur voor ons")
    en_match = ObjectionRouter(config, language="en").classify("this is too expensive for us")
    de_match = ObjectionRouter(config, language="de").classify("das ist fuer uns zu teuer")

    assert nl_match is not None
    assert en_match is not None
    assert de_match is not None
    assert nl_match.category == "prijs"
    assert en_match.category == "price"
    assert de_match.category == "preis"


def test_router_falls_back_to_nl_for_missing_language_file(caplog) -> None:
    config = DetectorConfig()

    with caplog.at_level(logging.WARNING):
        match = PainPointRouter(config, language="fr").classify("we doen offertes nog steeds in Word")

    assert match is not None
    assert match.category == "offerteproces"
    assert "Falling back to Dutch routes" in caplog.text


# ---------------------------------------------------------------------------
# LangRouter tests -- multilang detection disabled in the OSS build
# ---------------------------------------------------------------------------

def test_lang_router_route_returns_default_lang() -> None:
    """route() is disabled in the OSS build and always returns the default,
    regardless of the utterance text (fastText detection is future-sprint work)."""
    router = LangRouter()
    lang = router.route("speaker_1", "Ik mis overzicht in mijn pipeline en het kost teveel tijd")
    assert lang == "nl"


def test_lang_router_route_ignores_speaker_switches() -> None:
    """Disabled router does not detect a mid-conversation language switch;
    every call returns the configured default."""
    router = LangRouter()
    router.route("speaker_1", "ik denk dat we dit moeten oppakken")
    lang = router.route("speaker_1", "actually let me reconsider this for a moment")
    assert lang == "nl"


def test_lang_router_route_does_not_populate_speaker_profiles() -> None:
    """Disabled router does not touch per-speaker caching; profiles remain
    untouched (per-speaker detection is future-sprint work)."""
    router = LangRouter()
    router.route("speaker_1", "ik mis context hier")
    router.route("speaker_2", "we need to align on scope")
    profile_1 = router.get_profile("speaker_1")
    profile_2 = router.get_profile("speaker_2")
    assert profile_1.detected_lang is None
    assert profile_2.detected_lang is None


def test_lang_router_route_returns_default_below_min_words() -> None:
    """min_words_for_detect has no effect while routing is disabled; the
    configured default is returned regardless of utterance length."""
    router = LangRouter(default_lang="nl", min_words_for_detect=10)
    lang = router.route("speaker_1", "yes")  # 1 word
    assert lang == "nl"


def test_speaker_lang_profile_defaults() -> None:
    """Non-xfail: dataclass defaults moeten werken zonder implementatie."""
    profile = SpeakerLangProfile(speaker_id="test")
    assert profile.speaker_id == "test"
    assert profile.detected_lang is None
    assert profile.utterance_history == []
    assert profile.confidence == 0.0


def test_lang_router_get_profile_creates_new() -> None:
    """Non-xfail: profile-creatie zonder route()."""
    router = LangRouter()
    profile = router.get_profile("new_speaker")
    assert profile.speaker_id == "new_speaker"


def test_lang_router_reset_clears_all() -> None:
    """Non-xfail: reset() functioneert."""
    router = LangRouter()
    router.get_profile("a")
    router.get_profile("b")
    router.reset()
    assert len(router._profiles) == 0


def test_lang_router_route_never_raises() -> None:
    """Non-xfail pin of route()'s CURRENT contract.

    The OSS build ships multilang routing disabled: route() always returns
    default_lang and never raises. This guards the shipped contract; once
    fastText detection lands in a future sprint, route() may start returning
    a detected language and THIS test must be updated accordingly.
    """
    router = LangRouter(default_lang="nl", min_words_for_detect=10)
    lang = router.route("speaker_1", "ik mis overzicht in mijn pipeline en het kost teveel tijd")
    assert lang == "nl"
