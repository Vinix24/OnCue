"""Post-call batch transcription: turn a recorded session into a labeled transcript.

The live copilot uses whisper.cpp for low-latency streaming; this module is the
opposite end. It takes a finished recording (the two WAV tracks the recorder
writes per session: one per speaker) and produces one time-ordered, speaker-
labeled transcript, fast, via the Parakeet batch backend. That transcript is the
input the ``gespreksanalyse`` skill wants.

The heavy lifting (chunked decode to avoid Metal-OOM on long calls) lives in
``ParakeetMlxBackend``; this module only orchestrates and merges. The merge/format
helpers are pure and unit-tested; ``transcribe_session`` is the async glue.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from sales_copilot.core.speaker_bleed import BleedSegment, drop_speaker_bleed

# Map recorder WAV stems (see AudioRecorder / record_call.py) to display labels.
DEFAULT_LABELS = {
    "prospect": "PROSPECT",
    "self": "JIJ",
    "system": "PROSPECT",
    "mic": "JIJ",
}
_MIC_CHANNELS = ("self", "mic")
_SYSTEM_CHANNELS = ("prospect", "system")


# whisper.cpp prints silence and non-speech as "***", "[BLANK_AUDIO]", "[MUSIC]" or
# "(stilte)". Bracket groups without digits are markers; "(3)" is real text.
_MARKER_RE = re.compile(r"\[[^\]\d]*\]|\([^)\d]*\)")


def has_words(text: str) -> bool:
    """True when the segment holds at least one letter or digit outside a marker.

    ``***``, ``...``, ``[BLANK_AUDIO]`` and ``(stilte)`` carry no words. Text that
    merely contains an asterisk or a marker next to real words is kept as is.
    """
    return any(ch.isalnum() for ch in _MARKER_RE.sub(" ", text))


class _SegmentBackend(Protocol):
    async def transcribe_file_segments(self, path: Path) -> list[dict]: ...


def merge_labeled_segments(channels: dict[str, list[dict]]) -> list[dict]:
    """Interleave per-channel segments into one time-ordered dialogue.

    ``channels`` maps a channel label (WAV stem) to its ``[{start, end, text}]``
    segments. Returns ``[{start, label, text}]`` sorted by start time (stable, so
    equal-start segments keep channel insertion order). Segments without words
    (empty, ``***``, ``[BLANK_AUDIO]``) drop, and so do microphone lines that are
    loudspeaker echo of the system channel (``speaker_bleed``).
    """
    merged: list[dict] = []
    bleed: list[BleedSegment] = []
    for label, segments in channels.items():
        for seg in segments:
            text = str(seg.get("text", "")).strip()
            if not has_words(text):
                continue
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", start) or start)
            entry = {"start": start, "label": label, "text": text}
            merged.append(entry)
            bleed.append(BleedSegment(label, round(start * 1000), round(end * 1000), text, entry))
    mic_label = next((c for c in _MIC_CHANNELS if c in channels), None)
    system_label = next((c for c in _SYSTEM_CHANNELS if c in channels), None)
    if mic_label and system_label:
        # The segments here are in seconds; the echo filter works in milliseconds.
        kept = {id(b.source) for b in drop_speaker_bleed(bleed, mic_label, system_label).kept}
        merged = [m for m in merged if id(m) in kept]
    merged.sort(key=lambda m: m["start"])
    return merged


def format_transcript(merged: list[dict], label_map: dict[str, str] | None = None) -> str:
    """Render merged segments as ``[MM:SS] LABEL: text`` lines."""
    label_map = label_map or {}
    lines = [
        f"[{_fmt_ts(m['start'])}] {label_map.get(m['label'], m['label'])}: {m['text']}"
        for m in merged
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _fmt_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


async def transcribe_session(
    session_dir: str | Path,
    backend: _SegmentBackend,
    *,
    label_map: dict[str, str] | None = None,
) -> str:
    """Transcribe every WAV in a recorder session dir into one labeled transcript.

    One WAV per speaker (``prospect.wav``, ``self.wav``); each is decoded to
    timestamped segments and interleaved by time. Returns the formatted transcript.
    """
    session_dir = Path(session_dir)
    channels: dict[str, list[dict]] = {}
    for wav in sorted(session_dir.glob("*.wav")):
        channels[wav.stem] = await backend.transcribe_file_segments(wav)
    merged = merge_labeled_segments(channels)
    return format_transcript(merged, label_map or DEFAULT_LABELS)
