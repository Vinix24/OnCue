"""PII redaction filter for Dutch B2B recruitment context.

Used by recruitment preset to redact PII before sending text to LLM.
AI Act Annex III compliance: PII must be redacted at ingress.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

from sales_copilot.core.pii_patterns import (
    PATTERN_ACHTERNAAM,
    PATTERN_DATUM_COMPACT,
    PATTERN_DATUM_WOORD,
    PATTERN_VOORNAAM,
)


@dataclass(frozen=True)
class PIIPattern:
    name: str
    pattern: Pattern[str]
    replacement: str


_PATTERNS: tuple[PIIPattern, ...] = (
    # Namen: achternaam-pattern eerst zodat "Jan van der Berg" als geheel wordt geredact,
    # daarna standalone voornamen opvangen wat overblijft.
    PIIPattern("achternaam", PATTERN_ACHTERNAAM, "[NAAM]"),
    PIIPattern("voornaam", PATTERN_VOORNAAM, "[NAAM]"),
    # Bestaande 6 patronen (backward compat)
    PIIPattern("bsn", re.compile(r"\b\d{9}\b"), "[BSN]"),
    PIIPattern(
        "telefoon",
        # (?<!\w) replaces leading \b because + is not a word-char so \b fails before +31
        re.compile(r"(?<!\w)(?:\+31\s?|0)(?:6[\s-]?\d{8}|\d{2,3}[\s-]?\d{6,7})\b"),
        "[TELEFOON]",
    ),
    PIIPattern(
        "iban",
        re.compile(r"\bNL\d{2}\s?[A-Z]{4}\s?\d{4}\s?\d{4}\s?\d{2}\b"),
        "[IBAN]",
    ),
    PIIPattern(
        "email",
        # Updated: [\w.-]+ in domain allows subdomains (mail.example.com)
        re.compile(r"\b[\w.+-]+@[\w.-]+\.[\w]+\b"),
        "[EMAIL]",
    ),
    PIIPattern(
        "postcode",
        re.compile(r"\b\d{4}\s?[A-Z]{2}\b"),
        "[POSTCODE]",
    ),
    PIIPattern(
        "geboortedatum",
        re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/](19|20)\d{2}\b"),
        "[DATUM]",
    ),
    # Uitgebreide datum-variants
    PIIPattern("datum_woord", PATTERN_DATUM_WOORD, "[DATUM]"),
    PIIPattern("datum_compact", PATTERN_DATUM_COMPACT, "[DATUM]"),
)


def redact_pii(text: str) -> tuple[str, int]:
    """Redact PII patterns from text. Returns (clean_text, hit_count)."""
    clean = text
    hits = 0
    for pat in _PATTERNS:
        clean, n = pat.pattern.subn(pat.replacement, clean)
        hits += n
    return clean, hits
