"""Language routing for the copilot.

Two independent surfaces live here:

1. ``route_coaching_prompts`` / ``route_vocabulary_config`` -- given the
   configured product language (``LANGUAGE``, via ``i18n.configured_language``),
   return the language-appropriate coaching-prompt set and the transcription
   vocabulary/biasing config for that language. This is the seam the rest of the
   engine calls so those two surfaces stop being hardcoded NL.
2. ``LangRouter`` -- a per-speaker language *detection* skeleton for a future
   sprint (diarization + fastText). Disabled in the OSS build; always returns
   the configured default language.

Sprint+1 detection plan (LangRouter):
1. Per speaker (via diarization) hou last-N-utterances bij
2. fastText taal-detectie op gecombineerde tekst (>10 woorden context)
3. Cache language-decision tot speaker-switch detected
4. Bij switch: emit lang_change WS event
5. Detector-pipeline: route per utterance naar juiste preset-routes (sales_nl|sales_en|sales_de)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from sales_copilot.core import i18n
from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

SupportedLang = Literal["nl", "en", "de"]

#: Vocabulary/biasing config for the default (Dutch) language. Language-specific
#: variants follow the ``transcription_vocabulary_<lang>.yaml`` naming convention
#: and are picked up automatically when present.
_DEFAULT_VOCABULARY_CONFIG = "config/transcription_vocabulary.yaml"


@dataclass(frozen=True)
class CoachingPrompts:
    """Language-specific coaching-prompt set served to the suggestion coach."""

    language: str
    system_prompt: str
    user_prompt: str
    empty_state: str
    context_docs_label: str


def route_coaching_prompts(language: str | None = None) -> CoachingPrompts:
    """Return the coaching-prompt set for ``language`` (default: configured LANGUAGE).

    Strings are resolved from the i18n message catalog, so a language that is
    only partially translated transparently falls back to Dutch per key.
    """
    lang = language if language is not None else i18n.configured_language()
    lang = lang.strip().lower() or i18n.DEFAULT_LANGUAGE
    return CoachingPrompts(
        language=lang,
        system_prompt=i18n.t("coaching.suggestion_system_prompt", lang=lang),
        user_prompt=i18n.t("coaching.suggestion_user_prompt", lang=lang),
        empty_state=i18n.t("coaching.suggestion_empty_state", lang=lang),
        context_docs_label=i18n.t("coaching.context_docs_label", lang=lang),
    )


def route_vocabulary_config(language: str | None = None) -> str:
    """Return the transcription-vocabulary config path for ``language``.

    The default language maps to the base ``config/transcription_vocabulary.yaml``.
    Other languages use ``config/transcription_vocabulary_<lang>.yaml`` when that
    file exists, and fall back to the base config otherwise.
    """
    lang = language if language is not None else i18n.configured_language()
    lang = lang.strip().lower() or i18n.DEFAULT_LANGUAGE
    if lang == i18n.DEFAULT_LANGUAGE:
        return _DEFAULT_VOCABULARY_CONFIG
    candidate = f"config/transcription_vocabulary_{lang}.yaml"
    if resolve_app_path(candidate).exists():
        return candidate
    return _DEFAULT_VOCABULARY_CONFIG


@dataclass
class SpeakerLangProfile:
    """Cached language decision per speaker."""

    speaker_id: str
    detected_lang: SupportedLang | None = None
    utterance_history: list[str] = field(default_factory=list)
    confidence: float = 0.0
    last_switch_ts: float = 0.0


class LangRouter:
    """Route utterances naar juiste preset-language op basis van per-speaker taal-detectie."""

    def __init__(
        self,
        *,
        default_lang: SupportedLang = "nl",
        min_words_for_detect: int = 10,
    ) -> None:
        self._default_lang = default_lang
        self._min_words = min_words_for_detect
        self._profiles: dict[str, SpeakerLangProfile] = {}

    def route(self, speaker_id: str, text: str) -> SupportedLang:
        """Bepaal taal voor deze utterance.

        Multilang detection (fastText + per-speaker caching) is future-sprint
        work; the OSS build ships with routing disabled and always returns
        the configured default language.
        """
        logger.info("multilang routing disabled in OSS build")
        return self._default_lang

    def get_profile(self, speaker_id: str) -> SpeakerLangProfile:
        """Haal profiel op (creeer als nieuw)."""
        if speaker_id not in self._profiles:
            self._profiles[speaker_id] = SpeakerLangProfile(speaker_id=speaker_id)
        return self._profiles[speaker_id]

    def reset(self, speaker_id: str | None = None) -> None:
        """Reset cache voor een spreker of alle sprekers."""
        if speaker_id is None:
            self._profiles.clear()
        else:
            self._profiles.pop(speaker_id, None)
