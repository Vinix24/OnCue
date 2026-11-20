"""English is the default of a new install; Dutch is a setting (UI, call, Whisper, teaser, vocabulary)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sales_copilot.core import i18n, lang_router
from sales_copilot.core.config import DetectorConfig, I18nConfig, TranscriberConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIR = REPO_ROOT / "config" / "i18n"


@pytest.fixture(autouse=True)
def _clean_language_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGUAGE", "CALL_LANGUAGE", "WHISPER_LANGUAGE", "WHISPER_VOCABULARY_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    i18n.clear_cache()


def _flatten(node: object, prefix: str = "") -> set[str]:
    if isinstance(node, dict):
        keys: set[str] = set()
        for key, value in node.items():
            keys |= _flatten(value, f"{prefix}{key}.")
        return keys
    return {prefix.rstrip(".")}


# --- defaults ---------------------------------------------------------------


def test_without_language_env_ui_call_and_whisper_are_english() -> None:
    assert I18nConfig.from_env().language == "en"
    assert i18n.configured_language() == "en"
    assert DetectorConfig.from_env().call_language == "en"
    assert TranscriberConfig.from_env().language == "en"


def test_dutch_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGUAGE", "nl")
    monkeypatch.setenv("CALL_LANGUAGE", "nl")

    assert I18nConfig.from_env().language == "nl"
    assert DetectorConfig.from_env().call_language == "nl"
    assert TranscriberConfig.from_env().language == "nl"


def test_whisper_language_overrides_call_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "nl")
    monkeypatch.setenv("WHISPER_LANGUAGE", "de")

    assert TranscriberConfig.from_env().language == "de"
    assert DetectorConfig.from_env().call_language == "nl"


def test_env_example_ships_english_and_whisper_follows_call_language() -> None:
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()

    assert "LANGUAGE=en" in lines
    assert "CALL_LANGUAGE=en" in lines
    assert not any(line.startswith("WHISPER_LANGUAGE=") for line in lines)
    assert not any(line.startswith("WHISPER_VOCABULARY_CONFIG=") for line in lines)


def test_fresh_env_from_example_resolves_the_english_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dotenv import dotenv_values

    values = dotenv_values(REPO_ROOT / ".env.example")
    for name in ("LANGUAGE", "CALL_LANGUAGE", "WHISPER_LANGUAGE", "WHISPER_VOCABULARY_CONFIG"):
        value = values.get(name)
        if value is not None:
            monkeypatch.setenv(name, value)
    i18n.clear_cache()

    config = TranscriberConfig.from_env()

    assert config.vocabulary_config == lang_router.route_vocabulary_config("en")
    assert config.vocabulary_config != "config/transcription_vocabulary.yaml"


NL_VOCAB = "config/transcription_vocabulary.yaml"
EN_VOCAB = "config/transcription_vocabulary_en.yaml"


@pytest.mark.parametrize(
    ("ui", "call", "whisper", "expected"),
    [
        ("en", "nl", None, NL_VOCAB),
        ("nl", "en", None, EN_VOCAB),
        ("en", "en", "nl", NL_VOCAB),
        ("nl", "nl", "en", EN_VOCAB),
        ("nl", "en", "nl", NL_VOCAB),
    ],
)
def test_vocabulary_follows_the_spoken_language(
    monkeypatch: pytest.MonkeyPatch,
    ui: str,
    call: str,
    whisper: str | None,
    expected: str,
) -> None:
    for name in ("WHISPER_LANGUAGE", "WHISPER_VOCABULARY_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGUAGE", ui)
    monkeypatch.setenv("CALL_LANGUAGE", call)
    if whisper is not None:
        monkeypatch.setenv("WHISPER_LANGUAGE", whisper)
    i18n.clear_cache()

    assert TranscriberConfig.from_env().vocabulary_config == expected


@pytest.mark.parametrize(("ui", "call"), [("en", "nl"), ("nl", "en"), ("en", "en")])
def test_explicit_vocabulary_config_wins(
    monkeypatch: pytest.MonkeyPatch, ui: str, call: str
) -> None:
    monkeypatch.delenv("WHISPER_LANGUAGE", raising=False)
    monkeypatch.setenv("LANGUAGE", ui)
    monkeypatch.setenv("CALL_LANGUAGE", call)
    monkeypatch.setenv("WHISPER_VOCABULARY_CONFIG", "config/custom_vocab.yaml")
    i18n.clear_cache()

    assert TranscriberConfig.from_env().vocabulary_config == "config/custom_vocab.yaml"


# --- catalog parity ---------------------------------------------------------


def test_catalogs_carry_the_same_keys() -> None:
    en = _flatten(yaml.safe_load((CATALOG_DIR / "en.yaml").read_text(encoding="utf-8")))
    nl = _flatten(yaml.safe_load((CATALOG_DIR / "nl.yaml").read_text(encoding="utf-8")))

    assert en and en == nl, f"only in en: {sorted(en - nl)}, only in nl: {sorted(nl - en)}"


# --- vocabulary -------------------------------------------------------------


def test_english_call_does_not_get_the_dutch_vocabulary() -> None:
    from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config

    path = lang_router.route_vocabulary_config("en")
    vocabulary = load_vocabulary_config(path)

    assert vocabulary.language == "en"
    assert vocabulary.enabled
    assert vocabulary.terms
    assert "Nederlands" not in vocabulary.initial_prompt


def test_dutch_call_keeps_the_dutch_vocabulary() -> None:
    from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config

    vocabulary = load_vocabulary_config(lang_router.route_vocabulary_config("nl"))

    assert vocabulary.language == "nl"
    assert "Nederlands" in vocabulary.initial_prompt
