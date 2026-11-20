"""A start-call payload without ``language`` follows the configured CALL_LANGUAGE."""

from __future__ import annotations

import pytest

from sales_copilot.__main__ import _parse_call_config
from sales_copilot.core import i18n
from sales_copilot.core.config import build_module_configs

EN_VOCAB = "config/transcription_vocabulary_en.yaml"
NL_VOCAB = "config/transcription_vocabulary.yaml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LANGUAGE", "CALL_LANGUAGE", "WHISPER_LANGUAGE", "WHISPER_VOCABULARY_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    i18n.clear_cache()


def _resolve(payload: dict) -> tuple[str, str, str]:
    call = _parse_call_config(payload)
    transcriber = build_module_configs(call)["transcriber"]
    return call.call_language, transcriber.language, transcriber.vocabulary_config


def test_english_install_without_language_is_english_everywhere() -> None:
    assert _resolve({}) == ("en", "en", EN_VOCAB)


def test_call_language_nl_without_payload_language_is_dutch_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "nl")
    assert _resolve({}) == ("nl", "nl", NL_VOCAB)


def test_payload_language_nl_on_english_install_gives_dutch() -> None:
    assert _resolve({"language": "nl"}) == ("nl", "nl", NL_VOCAB)


@pytest.mark.parametrize("payload", [{"language": ""}, {"language": "   "}, {"language": 5}])
def test_empty_or_invalid_language_falls_back_to_configured(payload: dict) -> None:
    assert _resolve(payload) == ("en", "en", EN_VOCAB)


@pytest.mark.parametrize("payload", [{}, {"language": "nl"}, {"language": "en"}])
def test_explicit_vocabulary_config_wins(
    monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    monkeypatch.setenv("WHISPER_VOCABULARY_CONFIG", "config/custom_vocab.yaml")
    assert _resolve(payload)[2] == "config/custom_vocab.yaml"
