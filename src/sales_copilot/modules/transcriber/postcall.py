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

from pathlib import Path
from typing import Protocol

# Map recorder WAV stems (see AudioRecorder / record_call.py) to display labels.
DEFAULT_LABELS = {
    "prospect": "PROSPECT",
    "self": "JIJ",
    "system": "PROSPECT",
    "mic": "JIJ",
}


class _SegmentBackend(Protocol):
    async def transcribe_file_segments(self, path: Path) -> list[dict]: ...


def merge_labeled_segments(channels: dict[str, list[dict]]) -> list[dict]:
    """Interleave per-channel segments into one time-ordered dialogue.

    ``channels`` maps a channel label (WAV stem) to its ``[{start, end, text}]``
    segments. Returns ``[{start, label, text}]`` sorted by start time (stable, so
    equal-start segments keep channel insertion order). Empty-text segments drop.
    """
    merged: list[dict] = []
    for label, segments in channels.items():
        for seg in segments:
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            merged.append({"start": float(seg.get("start", 0.0) or 0.0), "label": label, "text": text})
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
