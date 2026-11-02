"""Offline diarization merge — pure post-processor for speaker-labeled transcripts.

Two layers:

1. Pure merge logic (no models, no I/O):
   ``merge_diarization(transcript_segments, diarization_turns)`` assigns each
   transcript segment to the speaker with the greatest temporal overlap and
   collapses consecutive same-speaker segments into blocks.

2. Batch diarization helpers:
   ``diarize_wav(wav_path, ...)`` runs pyannote on a whole WAV offline and
   returns ``(start, end, speaker)`` turn tuples.  ``diarize_audio(...)`` picks
   between pyannote (needs a Hugging Face token) and sherpa-onnx (token-free,
   see ``diarize_sherpa``).  Heavy imports (torch / pyannote / sherpa_onnx) are
   deferred inside the functions so module import stays cheap.

Both layers together power ``scripts/post_process_call.py``.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptSegment:
    """A single Whisper segment: start/end in seconds, raw text."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class DiarizationTurn:
    """A single pyannote speaker turn: start/end in seconds, speaker label."""

    start: float
    end: float
    speaker: str


@dataclass(frozen=True)
class LabeledBlock:
    """One or more consecutive same-speaker transcript segments merged together."""

    speaker: str
    text: str
    start: float
    end: float


# ---------------------------------------------------------------------------
# Pure merge logic
# ---------------------------------------------------------------------------

_UNKNOWN_SPEAKER = "SPEAKER_?"


def _speaker_for(
    seg: TranscriptSegment,
    turns: list[DiarizationTurn],
    fallback: str,
) -> str:
    """Return the speaker label with the greatest temporal overlap with *seg*.

    When no turn overlaps at all, *fallback* is returned (nearest-turn logic
    would add complexity without measurable gain for post-call use; callers
    may pass a custom fallback if needed).
    """
    overlap: dict[str, float] = defaultdict(float)
    for turn in turns:
        ov = min(seg.end, turn.end) - max(seg.start, turn.start)
        if ov > 0:
            overlap[turn.speaker] += ov
    if not overlap:
        return fallback
    best = max(overlap, key=overlap.__getitem__)
    return _normalize_speaker_label(best)


def _normalize_speaker_label(raw: str) -> str:
    """Convert pyannote speaker labels to 'SPEAKER_XX' form.

    pyannote typically emits 'SPEAKER_00', 'SPEAKER_01', etc. already.
    The SRC script used 'SPREKER_' (Dutch); we normalise to 'SPEAKER_'.
    """
    parts = raw.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return f"SPEAKER_{parts[1].zfill(2)}"
    return raw


def merge_diarization(
    transcript_segments: list[dict | TranscriptSegment],
    diarization_turns: list[tuple | DiarizationTurn],
    unknown_fallback: str = _UNKNOWN_SPEAKER,
) -> list[LabeledBlock]:
    """Assign speaker labels to transcript segments and group into blocks.

    Parameters
    ----------
    transcript_segments:
        List of ``{start, end, text}`` dicts *or* ``TranscriptSegment``
        dataclass instances.  Start/end are **seconds** (float).
    diarization_turns:
        List of ``(start, end, speaker)`` tuples *or* ``DiarizationTurn``
        dataclass instances.  Start/end are **seconds** (float).
    unknown_fallback:
        Speaker label to assign when no diarization turn overlaps a segment.

    Returns
    -------
    list[LabeledBlock]
        Consecutive same-speaker segments are merged into single blocks.
        Empty-text segments are skipped.
    """
    # Normalise inputs so the rest of the function works with typed objects.
    segs: list[TranscriptSegment] = []
    for item in transcript_segments:
        if isinstance(item, TranscriptSegment):
            segs.append(item)
        else:
            segs.append(TranscriptSegment(start=item["start"], end=item["end"], text=item["text"]))

    turns: list[DiarizationTurn] = []
    for item in diarization_turns:
        if isinstance(item, DiarizationTurn):
            turns.append(item)
        else:
            turns.append(DiarizationTurn(start=item[0], end=item[1], speaker=item[2]))

    blocks: list[LabeledBlock] = []
    current_speaker: str | None = None
    current_texts: list[str] = []
    current_start: float = 0.0
    current_end: float = 0.0

    for seg in segs:
        text = seg.text.strip()
        if not text:
            continue
        speaker = _speaker_for(seg, turns, unknown_fallback)
        if speaker != current_speaker:
            if current_speaker is not None and current_texts:
                blocks.append(
                    LabeledBlock(
                        speaker=current_speaker,
                        text=" ".join(current_texts),
                        start=current_start,
                        end=current_end,
                    )
                )
            current_speaker = speaker
            current_texts = [text]
            current_start = seg.start
            current_end = seg.end
        else:
            current_texts.append(text)
            current_end = seg.end

    if current_speaker is not None and current_texts:
        blocks.append(
            LabeledBlock(
                speaker=current_speaker,
                text=" ".join(current_texts),
                start=current_start,
                end=current_end,
            )
        )

    return blocks


def render_labeled(blocks: list[LabeledBlock]) -> str:
    """Format labeled blocks as 'SPEAKER_00: <text>' lines.

    Each speaker change starts a new line.  Suitable for writing to a
    text file or displaying in a terminal.
    """
    return "\n".join(f"{b.speaker}: {b.text}" for b in blocks)


# ---------------------------------------------------------------------------
# Batch diarization helper (heavy imports inside — lazy)
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "pyannote/speaker-diarization-3.1"


def diarize_wav(
    wav_path: str | Path,
    *,
    hf_token: str | None = None,
    model_id: str = _DEFAULT_MODEL,
    num_speakers: int | None = None,
    device: str | None = None,
) -> list[DiarizationTurn]:
    """Run pyannote diarization on *wav_path* and return speaker turns.

    All heavy imports (torch, pyannote) happen inside this function so that
    importing this module is always cheap.

    Parameters
    ----------
    wav_path:
        Path to a WAV file.  16 kHz mono is recommended but pyannote handles
        resampling internally.
    hf_token:
        HuggingFace token.  Falls back to ``HUGGINGFACE_TOKEN`` env var and
        then to the cached ``huggingface-cli login`` token.
    model_id:
        Pyannote model to use.  Defaults to ``pyannote/speaker-diarization-3.1``
        (the repo's established model).  Pass
        ``pyannote/speaker-diarization-community-1`` to use the newer community
        model (useful for benchmarking).
    num_speakers:
        When set, forces pyannote to use exactly this many speakers.
    device:
        Torch device string (``'mps'``, ``'cuda'``, ``'cpu'``).  Auto-detected
        when ``None``: MPS on Apple Silicon, CPU otherwise.

    Returns
    -------
    list[DiarizationTurn]
        One entry per detected speaker turn, in chronological order.
    """
    import torch  # noqa: PLC0415 — lazy, pyannote is optional
    from pyannote.audio import Pipeline  # noqa: PLC0415 — lazy, pyannote is optional

    tok = hf_token or os.environ.get("HUGGINGFACE_TOKEN")

    # Support both pyannote 3.x (use_auth_token) and 4.x (token) APIs.
    try:
        pipeline = Pipeline.from_pretrained(model_id, token=tok)
    except TypeError:
        pipeline = Pipeline.from_pretrained(
            model_id,
            use_auth_token=(tok if tok else True),
        )

    # Device selection
    if device is None:
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    pipeline.to(torch.device(device))

    kwargs: dict = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers

    diarization = pipeline(str(wav_path), **kwargs)

    # pyannote 4.x wraps output; 3.x returns Annotation directly.
    annotation = getattr(diarization, "speaker_diarization", diarization)

    return [
        DiarizationTurn(start=turn.start, end=turn.end, speaker=speaker)
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]


# ---------------------------------------------------------------------------
# Backend selection: pyannote (token) or sherpa-onnx (token-free)
# ---------------------------------------------------------------------------

Backend = Literal["auto", "sherpa", "pyannote"]
BACKEND_CHOICES: tuple[str, ...] = ("auto", "sherpa", "pyannote")

# Voices in a group on one microphone collapse into one sherpa cluster; pyannote
# spreads them. At this many speakers and up, or when the count is unknown, ``auto``
# prefers pyannote if a token is available.
GROUP_MIN_SPEAKERS = 3


def find_hf_token(explicit: str | None = None) -> str | None:
    """Hugging Face token: argument, ``HUGGINGFACE_TOKEN``, ``HF_TOKEN``, then the cached login.

    The cached login is the file ``huggingface-cli login`` writes
    (``$HF_TOKEN_PATH``, else ``$HF_HOME/token``, else ``~/.cache/huggingface/token``).
    Returns ``None`` when none of them holds a non-empty token.
    """
    for candidate in (explicit, os.environ.get("HUGGINGFACE_TOKEN"), os.environ.get("HF_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    token_path = os.environ.get("HF_TOKEN_PATH")
    if token_path:
        path = Path(token_path)
    else:
        hf_home = os.environ.get("HF_HOME")
        path = (Path(hf_home) if hf_home else Path.home() / ".cache" / "huggingface") / "token"
    try:
        cached = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return cached or None


def resolve_backend(
    backend: Backend,
    num_speakers: int | None,
    hf_token: str | None,
) -> tuple[Literal["sherpa", "pyannote"], str | None]:
    """The backend that will run and an optional warning for the caller to log.

    ``auto`` decides on the speaker count and the token (see :func:`find_hf_token`):

    - ``num_speakers`` given and 2 or less: sherpa, no warning.
    - otherwise (3 or more, or not given) with a token: pyannote, no warning.
    - otherwise (no token): sherpa with a warning. For ``num_speakers`` of 3 or more
      the warning names the likely group; when the count is not given it says the
      count is unknown and that sherpa may put several voices under one label.
    """
    if backend not in BACKEND_CHOICES:
        raise ValueError(f"unknown backend {backend!r}; choose from {', '.join(BACKEND_CHOICES)}")
    if backend != "auto":
        return backend, None  # type: ignore[return-value]
    if num_speakers is not None and num_speakers < GROUP_MIN_SPEAKERS:
        return "sherpa", None
    if find_hf_token(hf_token):
        return "pyannote", None
    if num_speakers is not None:
        return "sherpa", (
            "Likely a group (--num-speakers >= 3) but no Hugging Face token: using sherpa-onnx. "
            "Voices in a group may end up under one label. A token (--hf-token, HUGGINGFACE_TOKEN "
            "or huggingface-cli login) enables the pyannote group model."
        )
    return "sherpa", (
        "Speaker count unknown and no Hugging Face token: using sherpa-onnx. In a group, sherpa may "
        "put several voices under one label. --num-speakers 2 or a token (--hf-token, "
        "HUGGINGFACE_TOKEN or huggingface-cli login) avoids that."
    )


def diarize_audio(
    wav_path: str | Path,
    *,
    backend: Backend = "auto",
    hf_token: str | None = None,
    model_id: str = _DEFAULT_MODEL,
    num_speakers: int | None = None,
    device: str | None = None,
    cluster_threshold: float | None = None,
) -> tuple[list[DiarizationTurn], str]:
    """Diarize *wav_path* with the backend :func:`resolve_backend` picks.

    Returns ``(turns, backend_used)`` where ``backend_used`` is ``"sherpa"`` or
    ``"pyannote"``.  The sherpa path clusters by ``cluster_threshold`` (default 0.8)
    and ignores *num_speakers*: forcing a count made sherpa put nearly all talk time
    in one cluster on a two-person recording.  The pyannote path is
    :func:`diarize_wav` unchanged.
    """
    chosen, warning = resolve_backend(backend, num_speakers, hf_token)
    if warning:
        logger.warning(warning)
    if chosen == "pyannote":
        turns = diarize_wav(
            wav_path, hf_token=hf_token, model_id=model_id, num_speakers=num_speakers, device=device
        )
        return turns, "pyannote"

    from sales_copilot.modules.reports.diarize_sherpa import (  # noqa: PLC0415 - optional extra, lazy
        DEFAULT_CLUSTER_THRESHOLD,
        diarize_wav_sherpa,
    )

    if num_speakers is not None:
        logger.info(
            "--num-speakers is ignored for sherpa: forcing the speaker count gave a wrong split "
            "on a two-person recording, so sherpa clusters by threshold"
        )
    raw = diarize_wav_sherpa(
        wav_path,
        threshold=DEFAULT_CLUSTER_THRESHOLD if cluster_threshold is None else cluster_threshold,
    )
    return [DiarizationTurn(start=s, end=e, speaker=spk) for s, e, spk in raw], "sherpa"
