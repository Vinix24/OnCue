"""Post-ASR transcript normalization: deterministic, local, no token budget.

D2 of the transcript-normalisatie-na-asr track. Plugs in at the two places a
definitive ``TranscriptEvent`` is created (``inference_worker.py`` and
``whisper_direct.py``), right before publish. Partial transcripts and the
sample_aha demo player are untouched: partials are display-only and get
superseded by the (normalized) final event, and sample_aha replays curated
demo transcripts, not live ASR output.

Matching order, each chosen to keep the held-out false-positive rate low
(measured via ``scripts/measure_transcript_normalization.py``):

1. Exact variant table — human-vetted misspelling -> canonical mappings, no
   length restriction, because each entry was verified against real measured
   transcripts rather than guessed. A key containing a space matches a fixed
   two-token phrase (e.g. ``"Adrian Voss" -> "Adriaan Voss"``), which keeps
   a generic surname alone from ever being touched.
2. Adjacent-token merge — two consecutive tokens concatenated (no separator)
   that exactly equal a canonical term, e.g. ASR splitting a compound product
   name into two separate words.
3. Fuzzy edit-distance against the canonical term list. Terms shorter than 5
   characters are NEVER fuzzy targets: short acronyms collide with real words
   too easily (measured case: the ASR vocabulary bias for "ROI" overwrote the
   real name "Roy" in a live call). Terms of >= 5 characters allow an edit
   distance of 1; terms of >= 8 characters allow 2.

Per-conversation terms (D3): the server puts the client's ``termen`` from
``klant.yaml`` in ``config.call_terms``. ``with_call_terms`` layers them over
the fixed list with precedence: a call term is never rewritten by a fixed
variant or fuzzed into another term, and it wins fuzzy ties. The list lives
only in transcriber process memory and is cleared on ``call_ended``.

LOG-ONLY decision (tiebreaker, plan-gate round 3): the original text and every
replacement (source, target, rule) go to the local transcriber log at DEBUG
and nowhere else. No second wire field, no report field, no side-channel. The
returned text is the only thing that reaches the ``TranscriptEvent`` payload,
so downstream consumers (detector, SummaryEngine, cloud-LLM input, the on-disk
session report) only ever see the normalized wording, never the original.
Call terms are participant names (PII) and are never logged at INFO.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_NORMALIZATION_PATH = Path("config/transcript_normalization.yaml")

_FUZZY_MIN_TERM_LEN = 5
_FUZZY_WIDE_TERM_LEN = 8

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# (source, target, rule) — rule is one of "variant", "merge", "fuzzy"
Replacement = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class NormalizationLists:
    """Loaded normalization configuration.

    ``terms`` are canonical forms eligible for fuzzy/merge matching.
    ``variants`` are exact, pre-vetted misspelling -> canonical mappings.
    """

    enabled: bool
    terms: tuple[str, ...]
    variants: dict[str, str]


def sanitize_call_terms(raw: object) -> tuple[str, ...]:
    """Reduce a server-supplied ``call_terms`` value to a tuple of non-empty strings.

    The server already validated the list (klant.yaml ``termen``); this only
    guards the transcriber against a malformed payload, it does not re-validate.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for item in raw:
        if isinstance(item, str) and item.strip():
            seen[item.strip()] = None
    return tuple(seen)


def with_call_terms(base: NormalizationLists, call_terms: tuple[str, ...]) -> NormalizationLists:
    """Layer per-conversation terms over the fixed lists; call terms take precedence.

    Two entry forms, both plain strings so ``klant.yaml`` ``termen`` carries
    them unchanged: ``"Bergkamp"`` is a canonical term (fuzzy/merge eligible
    under the normal length rules), ``"VWA -> VBA"`` is an exact variant
    (usable for short forms that fuzzy matching never touches). Call terms
    come first in ``terms`` (they win fuzzy ties), a fixed variant whose
    source is a call term is dropped, and a call variant overrides a fixed
    variant with the same source.
    """
    if not call_terms:
        return base
    canonical: list[str] = []
    call_variants: dict[str, str] = {}
    for entry in call_terms:
        source, arrow, target = entry.partition("->")
        source, target = source.strip(), target.strip()
        if arrow and source and target:
            call_variants[source] = target
            canonical.append(target)
        elif not arrow:
            canonical.append(entry)
    protected = {t.casefold() for t in canonical}
    variants = {src: dst for src, dst in base.variants.items() if src.casefold() not in protected}
    variants.update(call_variants)
    terms = tuple(dict.fromkeys(canonical))
    fixed = tuple(t for t in base.terms if t not in terms)
    return NormalizationLists(enabled=base.enabled, terms=(*terms, *fixed), variants=variants)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Normalization config must be a mapping: {path}")
    return data


def load_normalization_lists(path: str | Path) -> NormalizationLists:
    """Load a normalization YAML file into a ``NormalizationLists``.

    Missing or malformed files are logged and return a disabled config so a
    bad config file cannot break transcription startup.
    """
    resolved = Path(path).expanduser()
    try:
        data = _load_yaml(resolved)
    except FileNotFoundError:
        logger.warning("Normalization config not found at %s; normalization disabled.", resolved)
        return NormalizationLists(enabled=False, terms=(), variants={})
    except Exception as exc:  # vnx-silent-except: malformed config must not crash transcription startup
        logger.warning("Could not load normalization config %s (%s); normalization disabled.", resolved, exc)
        return NormalizationLists(enabled=False, terms=(), variants={})

    raw_terms = data.get("terms", [])
    if not isinstance(raw_terms, list):
        raw_terms = []
    terms = tuple(str(t).strip() for t in raw_terms if str(t).strip())

    raw_variants = data.get("variants", {})
    if not isinstance(raw_variants, dict):
        raw_variants = {}
    variants = {
        str(source).strip(): str(target).strip()
        for source, target in raw_variants.items()
        if str(source).strip() and str(target).strip()
    }

    return NormalizationLists(
        enabled=bool(data.get("enabled", True)),
        terms=terms,
        variants=variants,
    )


def default_normalization_lists() -> NormalizationLists:
    """Load the default normalization config from ``config/transcript_normalization.yaml``."""
    return load_normalization_lists(DEFAULT_NORMALIZATION_PATH)


_default_lists_cache: NormalizationLists | None = None


def get_default_normalization_lists() -> NormalizationLists:
    """Lazily load and cache the default normalization config for production use."""
    global _default_lists_cache
    if _default_lists_cache is None:
        _default_lists_cache = default_normalization_lists()
    return _default_lists_cache


def _bounded_edit_distance(a: str, b: str, max_dist: int) -> int | None:
    """Levenshtein distance, bailing out early once it is provably > ``max_dist``."""
    if abs(len(a) - len(b)) > max_dist:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        row_min = curr[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
            row_min = min(row_min, curr[j])
        if row_min > max_dist:
            return None
        prev = curr
    return prev[-1] if prev[-1] <= max_dist else None


def _fuzzy_threshold_for_term(term: str) -> int | None:
    """Allowed edit distance for a canonical term, or None if it's too short to fuzzy-match."""
    if len(term) >= _FUZZY_WIDE_TERM_LEN:
        return 2
    if len(term) >= _FUZZY_MIN_TERM_LEN:
        return 1
    return None


def _closest_term(word: str, terms: tuple[str, ...]) -> str | None:
    if len(word) < _FUZZY_MIN_TERM_LEN or word in terms:
        return None
    best_term: str | None = None
    best_dist: int | None = None
    for term in terms:
        if term == word:
            continue
        threshold = _fuzzy_threshold_for_term(term)
        if threshold is None:
            continue
        dist = _bounded_edit_distance(word, term, threshold)
        if dist is not None and (best_dist is None or dist < best_dist):
            best_dist = dist
            best_term = term
    return best_term


def normalize(text: str, lists: NormalizationLists) -> tuple[str, list[Replacement]]:
    """Normalize known ASR misspellings in ``text``. Returns ``(new_text, replacements)``.

    ``replacements`` is only ever used for local DEBUG logging by the caller —
    it must never be attached to a published payload or written to disk.
    """
    if not lists.enabled or not text:
        return text, []

    matches = list(_WORD_RE.finditer(text))
    if not matches:
        return text, []

    replacements: list[Replacement] = []
    edits: list[tuple[int, int, str]] = []

    i = 0
    n = len(matches)
    while i < n:
        m = matches[i]
        word = m.group(0)

        if i + 1 < n:
            next_m = matches[i + 1]
            next_word = next_m.group(0)

            phrase = f"{word} {next_word}"
            target = lists.variants.get(phrase)
            if target is not None and target != phrase:
                edits.append((m.start(), next_m.end(), target))
                replacements.append((phrase, target, "variant"))
                i += 2
                continue

            merged = f"{word}{next_word}"
            if merged in lists.terms and merged != word:
                edits.append((m.start(), next_m.end(), merged))
                replacements.append((phrase, merged, "merge"))
                i += 2
                continue

        target = lists.variants.get(word)
        if target is not None and target != word:
            edits.append((m.start(), m.end(), target))
            replacements.append((word, target, "variant"))
            i += 1
            continue

        fuzzy_target = _closest_term(word, lists.terms)
        if fuzzy_target is not None:
            edits.append((m.start(), m.end(), fuzzy_target))
            replacements.append((word, fuzzy_target, "fuzzy"))
            i += 1
            continue

        i += 1

    if not edits:
        return text, []

    parts: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        parts.append(text[cursor:start])
        parts.append(replacement)
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), replacements


def normalize_with_default_lists(text: str) -> tuple[str, list[Replacement]]:
    """One-arg entry point for the D1 measurement harness's ``--normalize-fn`` plug-in."""
    return normalize(text, get_default_normalization_lists())
