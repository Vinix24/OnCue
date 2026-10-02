"""Junk decision for a finished call report (belapp-junkfilter-rapportschema D2).

A report is junk -- a voicemail, a network announcement, a phone menu, a line nobody
answered -- exactly when the report model said so: ``gesprek_gevoerd == False``. The word
count is not a gate. It is a confirming signal that goes into ``junk_reason``: the words
spoken on the prospect track after the known whisper hallucinations on silence
(``config/report_junk.yaml``) are removed.

Fail-open: enrichment that failed returns ``gesprek_gevoerd = True`` (``enrichment.py``), so
it is never junk, and a short real call the model calls a conversation is never junk either,
however few words it holds.

Speaker ``unknown`` (a call that was not diarized, ``session.py``) counts as prospect track:
it is "not the seller", so an undiarized conversation does not read as empty.

``junk_reason`` is a fixed category plus a count. It never carries transcript text, so it holds
no PII and ``REPORT_REDACT_PII`` leaves it alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sales_copilot.core.config import load_yaml
from sales_copilot.core.paths import resolve_app_resource
from sales_copilot.modules.reports.enrichment import ReportEnrichment

#: Speakers whose words count as the prospect track.
PROSPECT_TRACK_SPEAKERS = frozenset({"prospect", "unknown"})

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class JunkConfig:
    min_prospect_words: int
    hallucination_phrases: tuple[str, ...]


@dataclass(frozen=True)
class JunkVerdict:
    junk: bool
    reason: str | None
    prospect_words: int


@lru_cache(maxsize=1)
def load_junk_config() -> JunkConfig:
    """``config/report_junk.yaml``, loaded once."""
    data = load_yaml(resolve_app_resource("config/report_junk.yaml"))
    # Longest first, so "X door de Amara.org gemeenschap" goes as one phrase, not as "X door" plus leftovers.
    phrases = tuple(
        sorted(
            (str(phrase) for phrase in (data.get("hallucination_phrases") or []) if str(phrase).strip()),
            key=len,
            reverse=True,
        )
    )
    return JunkConfig(
        min_prospect_words=int(data.get("min_prospect_words", 12)),
        hallucination_phrases=phrases,
    )


def _strip_phrases(text: str, phrases: Iterable[str]) -> str:
    for phrase in phrases:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    return text


def count_prospect_words(transcript: Iterable[Any], config: JunkConfig | None = None) -> int:
    """Words on the prospect track, hallucination phrases and punctuation removed.

    The seller's words never count: a seller leaving 40 words on a voicemail is not a
    conversation.
    """
    config = config or load_junk_config()
    total = 0
    for entry in transcript:
        if (getattr(entry, "speaker", "") or "").lower() not in PROSPECT_TRACK_SPEAKERS:
            continue
        text = _strip_phrases(getattr(entry, "text", "") or "", config.hallucination_phrases)
        total += len(_WORD.findall(text))
    return total


def decide_junk(
    enrichment: ReportEnrichment, transcript: Iterable[Any], config: JunkConfig | None = None
) -> JunkVerdict:
    """Junk when the model says no conversation took place; the word count only explains why."""
    config = config or load_junk_config()
    words = count_prospect_words(transcript, config)
    if enrichment.gesprek_gevoerd:
        return JunkVerdict(junk=False, reason=None, prospect_words=words)
    confirmation = "onder" if words < config.min_prospect_words else "boven"
    reason = (
        f"model: geen gesprek (voicemail/IVR/geen gehoor); prospect-woorden: {words} "
        f"({confirmation} drempel {config.min_prospect_words})"
    )
    return JunkVerdict(junk=True, reason=reason, prospect_words=words)
