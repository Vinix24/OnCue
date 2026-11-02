"""Drop microphone lines that are loudspeaker echo of the system side.

Without a headset the microphone picks up the loudspeakers, so every sentence the other
side says is transcribed twice: on the system track, where it belongs, and a fraction
later and slightly differently segmented on the microphone track. The system track is the
clean copy. A microphone line is echo when it repeats what the system track says *at the
same moment*. A salesperson who repeats the prospect's words right after they finish is
mirroring, and that line is kept: real echo overlaps the original in time, mirroring
follows it.

The core works in one unit (milliseconds) and never looks at audio. Only microphone
segments are ever removed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from sales_copilot.core.config import env_bool

logger = logging.getLogger(__name__)

# Device latency plus differing segment boundaries per track.
OVERLAP_TOLERANCE_MS = 1000
# Share of a long microphone line's words that must be found, in order, on the system side.
LONG_LINE_MIN_WORDS = 3
LONG_LINE_MATCH_SHARE = 0.6
# Short lines: a long-echo call needs at least this many long echo lines before a short
# line is judged by the looser rules below.
ECHO_CALL_MIN_LONG_LINES = 2
# Tighter tolerance for a word clipped at a segment edge.
CLIPPED_WORD_TOLERANCE_MS = 500
# A clipped word must be at least this long to count as the start of a longer word.
CLIPPED_WORD_MIN_CHARS = 3
# A short line this brief that lies mostly inside system-side speech is echo.
SHORT_LINE_MAX_MS = 1500
SHORT_LINE_MIN_INSIDE_SHARE = 0.5

ECHO_FILTER_ENV = "TRANSCRIPT_ECHO_FILTER"

_WORD_RE = re.compile(r"[\w']+")


@dataclass(frozen=True)
class BleedSegment:
    """One transcript segment. ``source`` carries the caller's own record through the filter."""

    speaker: str
    start_ms: int
    end_ms: int
    text: str
    source: Any = None


@dataclass(frozen=True)
class BleedResult:
    kept: list[BleedSegment]
    removed: list[BleedSegment]
    removed_long: int
    removed_short: int


def echo_filter_enabled() -> bool:
    """The ``TRANSCRIPT_ECHO_FILTER`` setting; on by default."""
    return bool(env_bool(ECHO_FILTER_ENV, True))


def normalise_words(text: str) -> list[str]:
    """Lower-case words with apostrophes kept; punctuation and capitalisation never matter."""
    return [w.strip("'") for w in _WORD_RE.findall((text or "").lower()) if w.strip("'")]


def _overlaps(seg: BleedSegment, start_ms: int, end_ms: int) -> bool:
    return seg.start_ms < end_ms and seg.end_ms > start_ms


def _candidates(mic: BleedSegment, system: Sequence[BleedSegment], tolerance_ms: int) -> list[BleedSegment]:
    found = [s for s in system if _overlaps(s, mic.start_ms - tolerance_ms, mic.end_ms + tolerance_ms)]
    return sorted(found, key=lambda s: (s.start_ms, s.end_ms))


def _matched_share(mic_words: list[str], system_words: list[str]) -> float:
    if not mic_words or not system_words:
        return 0.0
    blocks = SequenceMatcher(None, mic_words, system_words, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks) / len(mic_words)


def _inside_system_speech_ms(mic: BleedSegment, system: Sequence[BleedSegment]) -> int:
    spans = sorted((s.start_ms, s.end_ms) for s in system if s.end_ms > s.start_ms)
    inside = 0
    cursor = mic.start_ms
    for start, end in spans:
        lo = max(start, cursor, mic.start_ms)
        hi = min(end, mic.end_ms)
        if hi > lo:
            inside += hi - lo
        cursor = max(cursor, end)
    return inside


def _clipped_match(mic_words: list[str], system_words: list[str]) -> bool:
    return bool(mic_words) and all(
        any(w == sw or (len(w) >= CLIPPED_WORD_MIN_CHARS and sw.startswith(w)) for sw in system_words)
        for w in mic_words
    )


def drop_speaker_bleed(
    segments: Sequence[BleedSegment],
    mic_label: str,
    system_label: str,
    *,
    enabled: bool | None = None,
) -> BleedResult:
    """Split ``segments`` into kept ones and microphone lines that are echo of the system side.

    ``enabled=None`` reads the ``TRANSCRIPT_ECHO_FILTER`` setting. The order of the kept
    segments is the input order. Nothing is removed when the filter is off or when there are
    no system segments.
    """
    segs = list(segments)
    if enabled is None:
        enabled = echo_filter_enabled()
    system = [s for s in segs if s.speaker == system_label]
    if not enabled or not system:
        return BleedResult(kept=segs, removed=[], removed_long=0, removed_short=0)

    removed_ids: set[int] = set()
    long_ids: set[int] = set()
    mics = [(i, s) for i, s in enumerate(segs) if s.speaker == mic_label]

    for i, mic in mics:
        words = normalise_words(mic.text)
        if len(words) < LONG_LINE_MIN_WORDS:
            continue
        cands = _candidates(mic, system, OVERLAP_TOLERANCE_MS)
        system_words = [w for c in cands for w in normalise_words(c.text)]
        if _matched_share(words, system_words) >= LONG_LINE_MATCH_SHARE:
            long_ids.add(i)
    removed_ids |= long_ids
    echo_call = len(long_ids) >= ECHO_CALL_MIN_LONG_LINES

    short_ids: set[int] = set()
    for i, mic in mics:
        words = normalise_words(mic.text)
        if not words or len(words) >= LONG_LINE_MIN_WORDS:
            continue
        cands = _candidates(mic, system, OVERLAP_TOLERANCE_MS)
        if any(normalise_words(c.text) == words for c in cands):
            short_ids.add(i)
            continue
        if not echo_call:
            continue
        tight = _candidates(mic, system, CLIPPED_WORD_TOLERANCE_MS)
        tight_words = [w for c in tight for w in normalise_words(c.text)]
        duration = mic.end_ms - mic.start_ms
        inside_speech = (
            0 < duration <= SHORT_LINE_MAX_MS
            and _inside_system_speech_ms(mic, system) >= SHORT_LINE_MIN_INSIDE_SHARE * duration
        )
        if _clipped_match(words, tight_words) or inside_speech:
            short_ids.add(i)
    removed_ids |= short_ids

    kept = [s for i, s in enumerate(segs) if i not in removed_ids]
    removed = [s for i, s in enumerate(segs) if i in removed_ids]
    if removed:
        logger.info(
            "Echo filter removed %d microphone lines (%d long, %d short)",
            len(removed),
            len(long_ids),
            len(short_ids),
        )
    return BleedResult(kept=kept, removed=removed, removed_long=len(long_ids), removed_short=len(short_ids))
