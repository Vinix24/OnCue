"""Contextual term correction in the post-call report (objective termenlijst-in-uitwerking, D2).

A model with context can choose between a term from the customer's term list and an ordinary
word or name that looks or sounds alike (``ROI`` against the first name Roy, ``Teamleader``
against "team leader"); the deterministic layer (``modules/transcriber/normalize.py``) cannot.
D1 measured this chain with ``scripts/measure_term_corrections.py`` before it was built here;
that harness now imports its prefilter and validation from this module.

The chain, inside the ONE existing ``enrich_report`` call:

1. Prefilter (``TermPrefilter``): per segment, the list terms that resemble something in it.
   Only those go to the model, in the user part of the prompt (so through
   ``apply_outbound_pii``), never the whole list. A segment without candidates gets no
   candidates line.
2. The model names every correction as ``segment_index``, ``source``, ``occurrence``,
   ``target`` and ``reason`` (``TermCorrection``), never a character position.
3. The code validates (``review_term_corrections``): ``target`` is in the list AND was a
   candidate the model saw after the PII policy, ``source`` really occurs at that place on the
   ORIGINAL segment, the PII policy did not change how often ``source`` occurs in the segment
   (then the model's ``occurrence`` cannot be mapped onto the original), the correction removes
   no word (``loses_words``), and it does not overlap a correction already accepted.

The report keeps the original transcript; the accepted corrections are a layer on top of it.

The term list per call (``load_report_terms``): the client's ``klant.yaml`` ``termen``, the
client's optional ``termenlijst.yaml`` (post-call only, up to 5000 entries,
``core/klant_config.py``) and the fixed list ``config/transcript_normalization.yaml``, merged
and de-duplicated. Entry syntax is ``normalize.py``'s: ``"Term"`` or ``"bron -> Term"``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from sales_copilot.core.klant_config import KlantConfigError, load_klant_config, load_termenlijst
from sales_copilot.core.paths import resolve_app_resource
from sales_copilot.modules.transcriber.normalize import (
    _WORD_RE,
    NormalizationLists,
    _bounded_edit_distance,
    _fuzzy_threshold_for_term,
    load_normalization_lists,
    sanitize_call_terms,
    with_call_terms,
)

logger = logging.getLogger(__name__)

#: The fixed post-ASR list, the first source of every call's term list.
NORMALIZATION_CONFIG = "config/transcript_normalization.yaml"

#: Separates the candidate terms on a candidates line; a term holding it can never be a target.
CANDIDATE_SEPARATOR = "; "

_MAX_NGRAM = 3
_ACRONYM_LENGTHS = range(3, 5)
_EMPTY_LISTS = NormalizationLists(enabled=True, terms=(), variants={})

# Both labels start in lower case on purpose: the PII surname pattern ("Naam van Naam") ends on
# a capitalised word and may span a line break, so a capitalised label could be swallowed
# together with the end of the line above it, and the postcode pattern (four digits, two
# capitals) could take a label's first letters along with a number ending the line above.
_SEGMENT_LINE_RE = re.compile(r"^segment (\d+) \(([A-Za-z]+)\): (.*)$")
_CANDIDATES_LINE_RE = re.compile(r"^candidates for segment (\d+): (.*)$")

#: Appended to the report system prompt when at least one segment has candidates.
TERM_CORRECTION_PROMPT = (
    "\n\nIn this transcript every line starts with its segment number and who spoke: "
    "'segment N (Seller): text'. Some segments are followed by a line 'candidates for segment N: "
    "...' with candidate terms, separated by semicolons: entries from the customer's term list "
    "that resemble something in that segment. Speech recognition sometimes writes such a term as "
    "an ordinary word or a name that looks or sounds alike, for example a product name as two "
    "ordinary words, or an acronym as a first name. Just as often the ordinary word or the name "
    "is exactly what was said.\n\n"
    "For every place where the context makes clear that a candidate term was meant, return one "
    "term correction: segment_index (the segment number N), source (the exact text as it appears "
    "in that segment, which is replaced as a whole), occurrence (which occurrence of source in "
    "that segment, counting from 1), target (the candidate term, written exactly as in the list) "
    "and reason (a few words on what in the context shows the term was meant).\n\n"
    "Correct only when the context shows the term was meant. When a word or a name is plausible "
    "as written, leave it. A person's name stays a name unless the context shows the term was "
    "meant, and a term stays a term unless the context shows a person was meant. Never correct "
    "text that is already right, never rewrite anything else, never correct text in square "
    "brackets, and never return a target that is not among the candidates of that segment. "
    "Return an empty list of term corrections when nothing needs correcting."
)


class TermCorrection(BaseModel):
    """One correction as the model names it: a place in a segment and a term. No offsets."""

    segment_index: int = Field(description="Index of the segment, as numbered in the input.")
    source: str = Field(description="The exact text as it appears in that segment; it is replaced as a whole.")
    occurrence: int = Field(description="Which occurrence of source in that segment, counting from 1.")
    target: str = Field(description="The candidate term that was meant, written exactly as in the list.")
    reason: str = Field(description="A few words on what in the context shows the term was meant.")


@dataclass(frozen=True)
class AppliedCorrection:
    """An accepted correction placed on the original segment: the span code computed."""

    segment_index: int
    start: int
    end: int
    target: str


# ---------------------------------------------------------------------------
# Position anchor and validation
# ---------------------------------------------------------------------------


def _source_pattern(source: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(source)}(?!\w)")


def find_occurrences(text: str, source: str) -> list[tuple[int, int]]:
    """Every whole-word span of ``source`` in ``text``, left to right."""
    if not source.strip():
        return []
    return [(m.start(), m.end()) for m in _source_pattern(source).finditer(text)]


def locate_occurrence(text: str, source: str, occurrence: int) -> tuple[int, int] | None:
    """The span of the ``occurrence``-th (1-based) whole-word ``source`` in ``text``, or ``None``."""
    if occurrence < 1:
        return None
    spans = find_occurrences(text, source)
    return spans[occurrence - 1] if occurrence <= len(spans) else None


class TermList:
    """The canonical terms a correction may target."""

    def __init__(self, terms: Iterable[str]) -> None:
        self.terms: tuple[str, ...] = tuple(dict.fromkeys(terms))
        folded: dict[str, list[str]] = {}
        for term in self.terms:
            folded.setdefault(term.casefold(), []).append(term)
        self._folded = {key: values[0] for key, values in folded.items() if len(values) == 1}

    def canonical(self, target: str) -> str | None:
        """``target`` as written in the list; a unique case-insensitive match counts too."""
        if target in self.terms:
            return target
        return self._folded.get(target.strip().casefold())


def word_tokens(text: str) -> list[str]:
    """``normalize.py``'s word tokens of ``text``."""
    return [match.group(0) for match in _WORD_RE.finditer(text)]


def _merges_into(source_words: Sequence[str], target_word: str) -> bool:
    """Whether adjacent source tokens are written together as ``target_word``.

    Case-insensitive; the joined source must fall within ``normalize.py``'s fuzzy threshold of
    the target (``Deal Flow`` -> ``Dealflow``, ``Team Leader`` -> ``Teamleader``).
    """
    joined = "".join(source_words).casefold()
    key = target_word.casefold()
    if joined == key:
        return True
    limit = _fuzzy_threshold_for_term(key)
    return bool(limit) and _bounded_edit_distance(joined, key, limit) is not None


def loses_words(source: str, target: str) -> bool:
    """Whether replacing ``source`` by ``target`` removes a word.

    Tokens both share at the start and at the end are set aside first (case-insensitive).
    What remains is a loss when the target side is empty while the source side is not, or
    when it has fewer tokens than the source side. One exception: two or more adjacent source
    tokens written together as one target word (``_merges_into``).
    """
    source_words, target_words = word_tokens(source), word_tokens(target)
    head = 0
    while (
        head < min(len(source_words), len(target_words))
        and source_words[head].casefold() == target_words[head].casefold()
    ):
        head += 1
    source_words, target_words = source_words[head:], target_words[head:]
    tail = 0
    while (
        tail < min(len(source_words), len(target_words))
        and source_words[-1 - tail].casefold() == target_words[-1 - tail].casefold()
    ):
        tail += 1
    source_words = source_words[: len(source_words) - tail]
    target_words = target_words[: len(target_words) - tail]
    if len(target_words) >= len(source_words):
        return False
    return not (len(target_words) == 1 and _merges_into(source_words, target_words[0]))


def place_correction(
    index: int,
    source: str,
    occurrence: int,
    target: str,
    originals: Sequence[str],
    visible: Sequence[str],
    taken: dict[int, list[tuple[int, int]]],
) -> tuple[int, int] | str:
    """The span of ``source`` on the ORIGINAL segment, or the reason the correction is refused.

    Refused: no change, a lost word, a count the PII policy changed (then the model's
    ``occurrence`` cannot be mapped onto the original), no such occurrence, or an overlap with
    a correction already accepted in the segment.
    """
    if not source.strip() or source == target:
        return "no_change"
    if loses_words(source, target):
        return "word_loss"
    if len(find_occurrences(originals[index], source)) != len(find_occurrences(visible[index], source)):
        return "source_changed_by_pii"
    span = locate_occurrence(originals[index], source, occurrence)
    if span is None:
        return "source_not_at_position"
    start, end = span
    if any(start < other_end and other_start < end for other_start, other_end in taken.get(index, [])):
        return "overlap"
    return span


def _validate(
    corrections: Sequence[TermCorrection],
    originals: Sequence[str],
    visible: Sequence[str],
    offered: Sequence[Collection[str]],
    term_list: TermList,
) -> tuple[list[tuple[TermCorrection, AppliedCorrection]], Counter[str]]:
    accepted: list[tuple[TermCorrection, AppliedCorrection]] = []
    rejected: Counter[str] = Counter()
    taken: dict[int, list[tuple[int, int]]] = {}
    for correction in corrections:
        index = correction.segment_index
        if not 0 <= index < len(originals):
            rejected["segment_out_of_range"] += 1
            continue
        target = term_list.canonical(correction.target)
        if target is None:
            rejected["target_not_in_list"] += 1
            continue
        if target not in offered[index]:
            rejected["target_not_candidate"] += 1
            continue
        placed = place_correction(index, correction.source, correction.occurrence, target, originals, visible, taken)
        if isinstance(placed, str):
            rejected[placed] += 1
            continue
        taken.setdefault(index, []).append(placed)
        applied = AppliedCorrection(segment_index=index, start=placed[0], end=placed[1], target=target)
        accepted.append((correction, applied))
    return accepted, rejected


def validate_corrections(
    corrections: Sequence[TermCorrection],
    originals: Sequence[str],
    visible: Sequence[str],
    offered: Sequence[Collection[str]],
    term_list: TermList,
) -> tuple[list[AppliedCorrection], Counter[str]]:
    """Accept only corrections that land on a real, unambiguous place and name an offered term.

    ``offered`` holds, per segment, the candidates the model actually saw (after the PII
    policy): a target outside them is refused even when it is in the list, because the model
    guessed it. The span is always located on ``originals``. ``visible`` is what the model saw
    after the PII policy: when that changed how often ``source`` occurs in the segment, the
    model's ``occurrence`` cannot be mapped onto the original and the correction is refused.
    A correction that removes a word is refused too (``loses_words``).
    """
    accepted, rejected = _validate(corrections, originals, visible, offered, term_list)
    return [applied for _, applied in accepted], rejected


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------


#: Sound-alike spellings folded together before an exact comparison.
_PHONETIC_FOLDS = (("ij", "i"), ("ie", "i"), ("y", "i"), ("th", "t"), ("ph", "f"), ("ck", "k"))
_DOUBLE_CONSONANT_RE = re.compile(r"([b-df-hj-np-tv-z])\1")

#: A word of a term: its case-folded text, and whether it only matches a token in capitals.
TokenKey = tuple[str, bool]


def phonetic_key(key: str) -> str:
    """``key`` with sound-alike spellings folded, so Roy/ROI, Eddie/Eddy and Smith/Smit meet.

    Only a doubled consonant collapses: a doubled vowel changes a Dutch word (dan, Daan).
    """
    for old, new in _PHONETIC_FOLDS:
        key = key.replace(old, new)
    return _DOUBLE_CONSONANT_RE.sub(r"\1", key)


def _deletions(word: str, depth: int) -> set[str]:
    found = {word}
    frontier = {word}
    for _ in range(depth):
        frontier = {w[:i] + w[i + 1 :] for w in frontier if len(w) > 1 for i in range(len(w))} - found
        found |= frontier
    return found


class TermPrefilter:
    """Which list terms resemble something in a segment: the only terms the model gets.

    Matching works on ``normalize.py``'s word tokens, case-insensitive except for acronyms:

    - exact: a token equals a word of a term;
    - sound-alike: the same ``phonetic_key`` (``Roy`` for ``ROI``, ``Eddie`` for ``Eddy``);
    - fuzzy: ``normalize.py``'s edit-distance rule, for words of 5 or more characters
      (``Zentrix`` for ``Sentrix``), through a deletion index so a 1000-term list stays cheap;
    - acronyms: an all-caps term only matches a token in capitals (``ROI``, never the word
      "roi"), and a 3-4 letter token in capitals also matches an all-caps term with one letter
      different or the same letters in another order (``VWA`` for ``VBA``, ``IDE`` for ``EDI``);
    - a multi-word term needs every word, in order, on adjacent tokens (``Marten Smith`` for
      ``Marten Smit``; a lone "bouw" never proposes a company called "Visser Bouw");
    - written together: 2-3 adjacent tokens joined equal a term (``Team Leader`` for
      ``Teamleader``), or one token equals a multi-word term joined or the source of a
      ``"bron -> Term"`` entry.

    Unlike ``normalize.py`` it replaces nothing: it only decides what the model gets to see.
    """

    def __init__(self, entries: Iterable[str]) -> None:
        lists = with_call_terms(_EMPTY_LISTS, sanitize_call_terms(list(entries)))
        self.terms: tuple[str, ...] = lists.terms
        self._order = {term: position for position, term in enumerate(self.terms)}
        self._registry: set[TokenKey] = set()
        self._phonetic: dict[str, set[TokenKey]] = {}
        self._deletes: dict[str, set[TokenKey]] = {}
        self._acronyms: dict[int, set[str]] = {}
        self._single: dict[TokenKey, set[str]] = {}
        self._sequences: dict[TokenKey, list[tuple[str, tuple[TokenKey, ...]]]] = {}
        self._joined: dict[str, set[str]] = {}
        self._joined_token: dict[str, set[str]] = {}
        for term in self.terms:
            keys = tuple(self._register(raw) for raw in word_tokens(term))
            if not keys:
                continue
            joined = "".join(key for key, _ in keys)
            self._joined.setdefault(joined, set()).add(term)
            if len(keys) == 1:
                self._single.setdefault(keys[0], set()).add(term)
            else:
                self._sequences.setdefault(keys[0], []).append((term, keys))
                self._joined_token.setdefault(joined, set()).add(term)
        for source, target in lists.variants.items():
            words = [word.casefold() for word in word_tokens(source)]
            if words:
                index = self._joined_token if len(words) == 1 else self._joined
                index.setdefault("".join(words), set()).add(target)

    def _register(self, raw: str) -> TokenKey:
        token_key = (raw.casefold(), raw.isupper())
        if token_key in self._registry:
            return token_key
        self._registry.add(token_key)
        key = token_key[0]
        if len(key) >= 3:
            self._phonetic.setdefault(phonetic_key(key), set()).add(token_key)
        depth = _fuzzy_threshold_for_term(key)
        if depth:
            for deletion in _deletions(key, depth):
                self._deletes.setdefault(deletion, set()).add(token_key)
        if token_key[1] and len(key) in _ACRONYM_LENGTHS:
            self._acronyms.setdefault(len(key), set()).add(key)
        return token_key

    def _match(self, raw: str, key: str) -> set[TokenKey]:
        """The term words one segment token matches."""
        caps = raw.isupper()
        matched = {token for token in ((key, False), (key, True)) if token in self._registry and (caps or not token[1])}
        if len(key) >= 3:
            matched |= {token for token in self._phonetic.get(phonetic_key(key), ()) if caps or not token[1]}
        depth = _fuzzy_threshold_for_term(key)
        if depth:
            checked: set[TokenKey] = set()
            for deletion in _deletions(key, depth):
                for token in self._deletes.get(deletion, ()):
                    if token in checked or token in matched or (token[1] and not caps):
                        continue
                    checked.add(token)
                    limit = _fuzzy_threshold_for_term(token[0])
                    if limit and _bounded_edit_distance(key, token[0], limit) is not None:
                        matched.add(token)
        if caps and len(key) in _ACRONYM_LENGTHS:
            letters = sorted(key)
            for other in self._acronyms.get(len(key), ()):
                if sorted(other) == letters or sum(a != b for a, b in zip(key, other, strict=True)) <= 1:
                    matched.add((other, True))
        return matched

    def candidates(self, text: str, cache: dict[tuple[str, bool], set[TokenKey]] | None = None) -> tuple[str, ...]:
        """The list terms that resemble something in ``text``, in list order."""
        cache = {} if cache is None else cache
        raws = word_tokens(text)
        keys = [raw.casefold() for raw in raws]
        matches: list[set[TokenKey]] = []
        for raw, key in zip(raws, keys, strict=True):
            cache_key = (key, raw.isupper())
            hit = cache.get(cache_key)
            if hit is None:
                hit = self._match(raw, key)
                cache[cache_key] = hit
            matches.append(hit)
        found: set[str] = set()
        for position, hit in enumerate(matches):
            found |= self._joined_token.get(keys[position], set())
            for token in hit:
                found |= self._single.get(token, set())
                for term, sequence in self._sequences.get(token, ()):
                    if position + len(sequence) <= len(matches) and all(
                        sequence[offset] in matches[position + offset] for offset in range(1, len(sequence))
                    ):
                        found.add(term)
            for size in range(2, min(_MAX_NGRAM, len(keys) - position) + 1):
                found |= self._joined.get("".join(keys[position : position + size]), set())
        return tuple(sorted(found, key=self._order.__getitem__))

    def scan(self, segments: Sequence[str]) -> list[tuple[str, ...]]:
        """Candidates for every segment of one conversation, sharing one token cache."""
        cache: dict[tuple[str, bool], set[TokenKey]] = {}
        return [self.candidates(text, cache) for text in segments]


# ---------------------------------------------------------------------------
# The prompt part and what the model saw of it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptSegment:
    """One transcript line of the request: its place in the report and its candidates."""

    index: int  # position in the report's full_transcript
    speaker: str  # Seller, Prospect or Unknown
    text: str  # the segment text as sent, whitespace collapsed
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class TermCorrectionPlan:
    """Every segment of the request with its candidates, and the list a target must be in."""

    segments: tuple[PromptSegment, ...]
    term_list: TermList

    @property
    def has_candidates(self) -> bool:
        return any(segment.candidates for segment in self.segments)


def plan_term_corrections(lines: Sequence[tuple[int, str, str]], entries: Sequence[str]) -> TermCorrectionPlan:
    """Candidates per segment for ``lines`` (``(index, speaker, text)``) from the term list ``entries``."""
    prefilter = TermPrefilter(entries)
    candidate_lists = prefilter.scan([text for _, _, text in lines])
    segments = tuple(
        PromptSegment(index=index, speaker=speaker, text=text, candidates=candidates)
        for (index, speaker, text), candidates in zip(lines, candidate_lists, strict=True)
    )
    return TermCorrectionPlan(segments=segments, term_list=TermList(prefilter.terms))


def numbered_transcript(segments: Sequence[PromptSegment]) -> str:
    """The transcript as the term-aware request carries it: numbered lines plus candidates.

    A segment without candidates gets no candidates line.
    """
    lines: list[str] = []
    for segment in segments:
        lines.append(f"segment {segment.index} ({segment.speaker}): {segment.text}")
        if segment.candidates:
            lines.append(f"candidates for segment {segment.index}: {CANDIDATE_SEPARATOR.join(segment.candidates)}")
    return "\n".join(lines)


@dataclass(frozen=True)
class VisibleSegment:
    """What the model saw of one segment after the PII policy."""

    text: str
    candidates: tuple[str, ...]


def parse_visible(visible_text: str, segments: Sequence[PromptSegment]) -> dict[int, VisibleSegment]:
    """Split the text the model saw back into its segments and candidate lists.

    Raises ``ValueError`` when the PII policy changed the structure of the request: a line
    that is no longer a segment or candidates line, or a segment or candidates line that went
    missing or appeared. Then no correction can be mapped onto the original.
    """
    texts: dict[int, str] = {}
    candidates: dict[int, tuple[str, ...]] = {}
    for line in visible_text.split("\n"):
        if (match := _SEGMENT_LINE_RE.match(line)) and int(match.group(1)) not in texts:
            texts[int(match.group(1))] = match.group(3)
        elif (match := _CANDIDATES_LINE_RE.match(line)) and int(match.group(1)) not in candidates:
            candidates[int(match.group(1))] = tuple(t for t in match.group(2).split(CANDIDATE_SEPARATOR) if t)
        else:
            raise ValueError("the PII policy changed the segment structure of the request")
    if set(texts) != {s.index for s in segments} or set(candidates) != {s.index for s in segments if s.candidates}:
        raise ValueError("the PII policy changed the segment structure of the request")
    return {index: VisibleSegment(text=text, candidates=candidates.get(index, ())) for index, text in texts.items()}


@dataclass(frozen=True)
class TermReview:
    """The model's term corrections after validation.

    ``pii_limited`` counts the segments with candidates whose text or candidates the PII
    policy changed before the model saw them: a name stripped to ``[NAAM]`` there could not be
    corrected, and the report says so.
    """

    accepted: tuple[TermCorrection, ...]
    rejected: Counter[str]
    pii_limited: int


def review_term_corrections(
    corrections: Sequence[TermCorrection],
    plan: TermCorrectionPlan,
    originals: Sequence[str],
    visible_text: str,
) -> TermReview:
    """Validate the model's ``corrections`` against what it saw and the ORIGINAL segments.

    ``originals`` is the report's ``full_transcript`` text per index. ``visible_text`` is the
    request after ``apply_outbound_pii`` with the resolved provider: exactly what the model
    got. When the PII policy broke the request structure, every correction is refused.
    """
    try:
        visible = parse_visible(visible_text, plan.segments)
    except ValueError:
        return TermReview(
            accepted=(),
            rejected=Counter({"structure_changed_by_pii": len(corrections)}) if corrections else Counter(),
            pii_limited=sum(1 for segment in plan.segments if segment.candidates),
        )
    seen = [visible.get(index) for index in range(len(originals))]
    accepted, rejected = _validate(
        corrections,
        originals,
        [segment.text if segment else "" for segment in seen],
        [frozenset(segment.candidates) if segment else frozenset() for segment in seen],
        plan.term_list,
    )
    pii_limited = sum(
        1
        for segment in plan.segments
        if segment.candidates
        and (visible[segment.index].text != segment.text or visible[segment.index].candidates != segment.candidates)
    )
    return TermReview(
        accepted=tuple(
            correction.model_copy(update={"target": applied.target, "reason": correction.reason.strip()})
            for correction, applied in accepted
        ),
        rejected=rejected,
        pii_limited=pii_limited,
    )


# ---------------------------------------------------------------------------
# The term list of one call
# ---------------------------------------------------------------------------


def fixed_term_entries(lists: NormalizationLists) -> tuple[str, ...]:
    """The fixed list as term entries: every canonical term, every variant as ``"bron -> doel"``."""
    if not lists.enabled:
        return ()
    return (*lists.terms, *(f"{source} -> {target}" for source, target in lists.variants.items()))


def merge_term_entries(*sources: Iterable[str]) -> tuple[str, ...]:
    """``sources`` in order, stripped and de-duplicated.

    An entry with a line break or a ``;`` is left out: a line break would break the request
    structure, and ``;`` separates the candidates on their line.
    """
    merged: dict[str, None] = {}
    unusable = 0
    for source in sources:
        for entry in sanitize_call_terms(list(source)):
            if "\n" in entry or "\r" in entry or CANDIDATE_SEPARATOR.strip() in entry:
                unusable += 1
                continue
            merged[entry] = None
    if unusable:
        logger.warning("Report term list: %d entries with a line break or ';' left out.", unusable)
    return tuple(merged)


def load_report_terms(
    client_slug: str | None,
    *,
    root: Path | None = None,
    normalization_path: Path | None = None,
) -> tuple[str, ...]:
    """The term list for one call's report: the client's terms first, then the fixed list.

    Sources: ``klant.yaml`` ``termen`` and ``termenlijst.yaml`` of the client (none without a
    client), and the fixed list. Each source fails open on its own: an invalid, unreadable or
    unsafe one is logged as an ERROR and left out, the others still count. A
    ``termenlijst.yaml`` error carries its validation message (which names entries by number,
    never by text); a ``klant.yaml`` error is logged without its message, which may quote a term.
    """
    client_terms: tuple[str, ...] = ()
    client_list: tuple[str, ...] = ()
    if client_slug:
        try:
            klant = load_klant_config(client_slug, root=root)
            client_terms = tuple(klant.termen) if klant is not None else ()
        except (OSError, ValueError, yaml.YAMLError):
            logger.error("Report term list: klant.yaml for client=%s is invalid; its termen are left out.", client_slug)
        try:
            client_list = load_termenlijst(client_slug, root=root)
        except KlantConfigError as exc:
            logger.error("Report term list: %s; the file is left out.", exc)
        except OSError as exc:
            logger.error(
                "Report term list: termenlijst.yaml for client=%s could not be read (%s); the file is left out.",
                client_slug,
                type(exc).__name__,
            )
    fixed = load_normalization_lists(normalization_path or resolve_app_resource(NORMALIZATION_CONFIG))
    return merge_term_entries(client_terms, client_list, fixed_term_entries(fixed))
