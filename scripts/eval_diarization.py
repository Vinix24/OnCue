#!/usr/bin/env python3
"""Compare speaker diarization backends on our own recordings.

Backends (all run on the CPU unless ``--device`` says otherwise):

    pyannote-3.1            pyannote/speaker-diarization-3.1 (what the product loads today)
    pyannote-community-1    pyannote/speaker-diarization-community-1 (pyannote.audio 4.x)
    sherpa:<embedding>      sherpa-onnx: pyannote segmentation-3.0 (ONNX) + a speaker-embedding
                            ONNX model + sherpa-onnx clustering by threshold (speaker count unknown)
    sherpa:<embedding>@oracle   the same, clustering to the true number of speakers
    pyannote-3.1@oracle, ...    any backend can get the oracle suffix

Embeddings for sherpa: ``eres2net`` (3D-Speaker ERes2Net VoxCeleb), ``wespeaker`` (WeSpeaker
ResNet34 VoxCeleb), ``titanet`` (NeMo TitaNet small).

    # Two-track recorder sessions: the reference comes from the tracks themselves
    python scripts/eval_diarization.py --sessions-root data/sessions --limit 3 --out ~/diar-eval

    # One room recording with several people
    python scripts/eval_diarization.py --audio call.m4a --expected-speakers 3 \\
        --start 60 --end 360 --blind-transcript call.json --out ~/diar-eval

Privacy: audio is read only from the paths on the command line, results go only to ``--out``,
nothing is uploaded, and logs never carry transcript text or audio content (paths, durations,
numbers). ``--out`` inside the repository is refused when an input lies outside it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import json
import logging
import math
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from sales_copilot.modules.reports.diarize_sherpa import SherpaBackend as _SherpaBackend
from sales_copilot.modules.reports.diarize_sherpa import default_cache_dir

logger = logging.getLogger("eval_diarization")

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 16000
VAD_CHUNK = 512
COLLAR_S = 0.25
MIC_LABEL = "self"
SYSTEM_LABEL = "prospect"

# Loudspeaker-bleed test. A microphone that hears the other side through the loudspeakers
# correlates with the system track at a lag of a few hundred milliseconds at most, and most
# of its speech lies inside system speech. Either signal alone skips the session.
BLEED_MAX_LAG_S = 0.3
BLEED_NCC_THRESHOLD = 0.15
BLEED_OVERLAP_THRESHOLD = 0.6
BLEED_WINDOW_S = 10.0
BLEED_MAX_WINDOWS = 8

PYANNOTE_PIPELINES = {
    "pyannote-3.1": "pyannote/speaker-diarization-3.1",
    "pyannote-community-1": "pyannote/speaker-diarization-community-1",
}
DEFAULT_BACKENDS = (
    "pyannote-3.1,pyannote-community-1,sherpa:eres2net,sherpa:wespeaker,sherpa:eres2net@oracle,sherpa:wespeaker@oracle"
)


@dataclass(frozen=True)
class Seg:
    start: float
    end: float
    speaker: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class DiarizationBackend(Protocol):
    name: str
    device: str
    oracle: bool

    def prepare(self, num_speakers: int | None) -> None:
        """Load models and configure clustering. Untimed in the DER table, reported as load_s."""

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[Seg]: ...

    def close(self) -> None:
        """Release the models so the next backend's memory is measured on its own."""


# --------------------------------------------------------------------------- audio


def load_audio(path: Path, start: float | None = None, end: float | None = None) -> np.ndarray:
    """Any format ffmpeg can read, as mono float32 at 16 kHz, optionally cut to [start, end]."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0.0):.3f}"]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found in PATH (brew install ffmpeg)") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read {path}: {proc.stderr[-300:].decode(errors='replace')}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def mix_tracks(a: np.ndarray, b: np.ndarray, ceiling: float = 0.99) -> np.ndarray:
    """Sum two tracks (shorter one zero-padded) and scale down only when the sum would clip."""
    n = max(len(a), len(b))
    mix = np.zeros(n, dtype=np.float32)
    mix[: len(a)] += a
    mix[: len(b)] += b
    peak = float(np.max(np.abs(mix))) if n else 0.0
    if peak > ceiling:
        mix *= ceiling / peak
    return mix


# --------------------------------------------------------------------------- VAD and reference

Region = tuple[float, float]


def vad_regions(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[Region]:
    """Speech regions in seconds from the product's Silero VAD (``VADProcessor``, default thresholds)."""
    from sales_copilot.modules.talk_time.vad import VADProcessor, load_silero_model

    processor = VADProcessor(load_silero_model(), MIC_LABEL, sample_rate=sample_rate)
    regions: list[Region] = []
    pad = (-len(audio)) % VAD_CHUNK
    padded = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
    flush = np.zeros(VAD_CHUNK, dtype=np.float32)
    chunks = (padded[i : i + VAD_CHUNK] for i in range(0, len(padded), VAD_CHUNK))
    for chunk in chunks:
        event = processor.process_chunk(chunk)
        if event is not None:
            regions.append((event.start_ms / 1000.0, event.end_ms / 1000.0))
    for _ in range(processor._debounce_ms // 32 + 2):  # noqa: SLF001 - flush the debounce tail
        event = processor.process_chunk(flush)
        if event is not None:
            regions.append((event.start_ms / 1000.0, event.end_ms / 1000.0))
            break
    return regions


def build_reference(mic_regions: Sequence[Region], system_regions: Sequence[Region]) -> list[Seg]:
    """Reference diarization from per-track speech: microphone is ``self``, system is ``prospect``.

    Overlapping speech stays as two overlapping segments, one per speaker.
    """
    segs = [Seg(s, e, MIC_LABEL) for s, e in mic_regions if e > s]
    segs += [Seg(s, e, SYSTEM_LABEL) for s, e in system_regions if e > s]
    return sorted(segs, key=lambda s: (s.start, s.end, s.speaker))


def _total(regions: Sequence[Region]) -> float:
    return sum(max(0.0, e - s) for s, e in regions)


def _inside_share(inner: Sequence[Region], outer: Sequence[Region]) -> float:
    total = _total(inner)
    if total <= 0:
        return 0.0
    spans = sorted(outer)
    inside = 0.0
    for s, e in inner:
        for os_, oe in spans:
            if oe <= s:
                continue
            if os_ >= e:
                break
            inside += min(e, oe) - max(s, os_)
    return inside / total


def peak_normalised_correlation(
    system: np.ndarray,
    mic: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
    *,
    max_lag_s: float = BLEED_MAX_LAG_S,
    window_s: float = BLEED_WINDOW_S,
    max_windows: int = BLEED_MAX_WINDOWS,
) -> float:
    """Highest |normalised cross-correlation| within +-max_lag_s, median over the loudest system windows."""
    win = int(window_s * sample_rate)
    lag = int(max_lag_s * sample_rate)
    n = min(len(system), len(mic))
    if n < win + lag or win <= lag:
        return 0.0
    starts = list(range(0, n - win + 1, win))
    energy = [float(np.sqrt(np.mean(system[s : s + win] ** 2))) for s in starts]
    loudest = [s for s, e in sorted(zip(starts, energy, strict=True), key=lambda p: -p[1])[:max_windows] if e > 1e-3]
    peaks: list[float] = []
    nfft = 1 << (2 * win - 1).bit_length()
    for s in loudest:
        x = system[s : s + win].astype(np.float64)
        y = mic[s : s + win].astype(np.float64)
        norm = float(np.linalg.norm(x) * np.linalg.norm(y))
        if norm <= 0:
            continue
        corr = np.fft.irfft(np.fft.rfft(y, nfft) * np.conj(np.fft.rfft(x, nfft)), nfft)
        lags = np.concatenate([corr[: lag + 1], corr[-lag:]])
        peaks.append(float(np.max(np.abs(lags))) / norm)
    return float(np.median(peaks)) if peaks else 0.0


@dataclass(frozen=True)
class BleedVerdict:
    bleed: bool
    peak_ncc: float
    mic_inside_system_share: float
    reason: str


def detect_loudspeaker_bleed(
    mic: np.ndarray,
    system: np.ndarray,
    mic_regions: Sequence[Region],
    system_regions: Sequence[Region],
    sample_rate: int = SAMPLE_RATE,
    *,
    ncc_threshold: float = BLEED_NCC_THRESHOLD,
    overlap_threshold: float = BLEED_OVERLAP_THRESHOLD,
) -> BleedVerdict:
    """True when the microphone carries the other side (no headset).

    Two independent tests, either one skips the session: the peak normalised cross-correlation
    between the tracks at a lag within +-0.3 s reaches ``ncc_threshold``, or at least
    ``overlap_threshold`` of the microphone's speech time lies inside system speech.
    """
    ncc = peak_normalised_correlation(system, mic, sample_rate)
    share = _inside_share(mic_regions, system_regions)
    reasons = []
    if ncc >= ncc_threshold:
        reasons.append(f"cross-correlation {ncc:.3f} >= {ncc_threshold}")
    if share >= overlap_threshold:
        reasons.append(f"mic speech inside system speech {share:.2f} >= {overlap_threshold}")
    return BleedVerdict(bool(reasons), ncc, share, "; ".join(reasons) or "tracks independent")


# --------------------------------------------------------------------------- RTTM and scoring


def write_rttm(path: Path, uri: str, segments: Sequence[Seg]) -> None:
    lines = [
        f"SPEAKER {uri} 1 {s.start:.3f} {s.duration:.3f} <NA> <NA> {s.speaker} <NA> <NA>"
        for s in sorted(segments, key=lambda s: (s.start, s.end))
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def read_rttm(path: Path, start: float = 0.0, end: float | None = None) -> list[Seg]:
    """RTTM segments clipped to [start, end] and shifted so ``start`` becomes 0."""
    segs: list[Seg] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        s, e = float(parts[3]), float(parts[3]) + float(parts[4])
        s, e = max(s, start), min(e, end) if end is not None else e
        if e > s:
            segs.append(Seg(s - start, e - start, parts[7]))
    return segs


@dataclass(frozen=True)
class Score:
    der: float
    missed: float
    false_alarm: float
    confusion: float
    reference_speech_s: float


SCORER_INSTALL_HINT = "uv sync --extra diar-eval --extra diarization"


class ScorerUnavailableError(RuntimeError):
    """pyannote.metrics is not installed, so DER cannot be computed."""


def require_scorer() -> None:
    """Raise ScorerUnavailableError with the install command when the DER scorer cannot be imported."""
    try:
        import pyannote.core  # noqa: F401
        import pyannote.metrics.diarization  # noqa: F401
    except ImportError as exc:
        raise ScorerUnavailableError(
            f"DER scoring needs pyannote.metrics (the diar-eval extra). Install it with: {SCORER_INSTALL_HINT}"
        ) from exc


def score_der(
    reference: Sequence[Seg], hypothesis: Sequence[Seg], duration_s: float, collar: float = COLLAR_S
) -> Score:
    """DER via pyannote.metrics: collar 0.25 s, overlapped speech scored. Components are shares of reference speech."""
    require_scorer()
    from pyannote.core import Annotation, Segment, Timeline
    from pyannote.metrics.diarization import DiarizationErrorRate

    def annotation(segs: Sequence[Seg]) -> Annotation:
        ann = Annotation()
        for i, s in enumerate(segs):
            if s.end > s.start:
                ann[Segment(s.start, s.end), i] = s.speaker
        return ann

    metric = DiarizationErrorRate(collar=collar, skip_overlap=False)
    parts = metric(
        annotation(reference), annotation(hypothesis), uem=Timeline([Segment(0.0, duration_s)]), detailed=True
    )
    total = parts["total"]
    if total <= 0:
        return Score(math.nan, math.nan, math.nan, math.nan, 0.0)
    return Score(
        der=parts["diarization error rate"],
        missed=parts["missed detection"] / total,
        false_alarm=parts["false alarm"] / total,
        confusion=parts["confusion"] / total,
        reference_speech_s=total,
    )


def count_speakers(segments: Sequence[Seg]) -> int:
    return len({s.speaker for s in segments})


# --------------------------------------------------------------------------- run and measure


@dataclass
class Row:
    input: str
    backend: str
    device: str
    der: float | None = None
    missed: float | None = None
    false_alarm: float | None = None
    confusion: float | None = None
    speakers_found: int | None = None
    speakers_expected: int | None = None
    load_s: float | None = None
    seconds: float | None = None
    rtf: float | None = None
    peak_rss_mb: float | None = None
    audio_s: float = 0.0
    note: str = ""


class _RssSampler:
    """Peak resident memory of this process while a block runs (sampled, so a lower bound).

    psutil comes with the diar-eval extra; without it `peak` stays None and memory is not reported.
    """

    def __init__(self, interval: float = 0.02) -> None:
        try:
            import psutil
        except ImportError:
            self._proc = None
        else:
            self._proc = psutil.Process()
        self._interval = interval
        self._stop = threading.Event()
        self.peak: int | None = self._proc.memory_info().rss if self._proc else None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak or 0, self._proc.memory_info().rss)
            self._stop.wait(self._interval)

    def __enter__(self) -> _RssSampler:
        if self._proc:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if not self._proc:
            return
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak or 0, self._proc.memory_info().rss)


def run_backend(
    backend: DiarizationBackend,
    audio: np.ndarray,
    label: str,
    reference: Sequence[Seg] | None,
    expected_speakers: int | None,
    out_dir: Path,
    sample_rate: int = SAMPLE_RATE,
) -> tuple[Row, list[Seg]]:
    duration = len(audio) / sample_rate
    row = Row(input=label, backend=backend.name, device=backend.device, audio_s=duration)
    expected = expected_speakers if expected_speakers else (count_speakers(reference) if reference else None)
    row.speakers_expected = expected
    if backend.oracle and not expected:
        row.note = "skipped: oracle needs a reference or --expected-speakers"
        return row, []
    try:
        with _RssSampler() as rss:
            t0 = time.perf_counter()
            backend.prepare(expected if backend.oracle else None)
            row.load_s = time.perf_counter() - t0
            t1 = time.perf_counter()
            hyp = backend.diarize(audio, sample_rate)
            row.seconds = time.perf_counter() - t1
        row.peak_rss_mb = rss.peak / (1024 * 1024) if rss.peak is not None else None
    except Exception as exc:  # a backend that cannot load must not stop the others
        row.note = f"failed: {type(exc).__name__}"
        logger.warning("backend %s failed on %s: %s", backend.name, label, type(exc).__name__)
        return row, []
    finally:
        backend.close()
        gc.collect()
    row.rtf = row.seconds / duration if duration > 0 else None
    row.speakers_found = count_speakers(hyp)
    if reference:
        try:
            score = score_der(reference, hyp, duration)
        except ScorerUnavailableError:
            row.note = "DER not measured: pyannote.metrics missing (diar-eval extra)"
        else:
            row.der, row.missed, row.false_alarm, row.confusion = (
                score.der,
                score.missed,
                score.false_alarm,
                score.confusion,
            )
    safe = backend.name.replace(":", "-").replace("@", "-") + f"-{backend.device}"
    write_rttm(out_dir / label / f"{safe}.rttm", label, hyp)
    return row, hyp


COLUMNS = [
    "input", "backend", "device", "der", "missed", "false_alarm", "confusion",
    "speakers_found", "speakers_expected", "load_s", "seconds", "rtf", "peak_rss_mb", "audio_s", "note",
]  # fmt: skip


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.3f}"
    return str(value)


def write_table(
    rows: Sequence[Row], columns: Sequence[str], csv_path: Path, md_path: Path, dicts: bool = False
) -> None:
    data = [r if dicts else asdict(r) for r in rows]
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        writer.writerows([[_fmt(d.get(c)) for c in columns] for d in data])
    md = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    md += ["| " + " | ".join(_fmt(d.get(c)) for c in columns) + " |" for d in data]
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")


AGGREGATE_COLUMNS = ["backend", "device", "inputs", "der_mean", "der_median", "rtf_total", "audio_s_total"]


def aggregate(rows: Sequence[Row]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        if r.seconds is not None:
            groups.setdefault((r.backend, r.device), []).append(r)
    out: list[dict[str, object]] = []
    for (backend, device), rs in groups.items():
        ders = [r.der for r in rs if r.der is not None and not math.isnan(r.der)]
        audio = sum(r.audio_s for r in rs)
        out.append(
            {
                "backend": backend,
                "device": device,
                "inputs": len(rs),
                "der_mean": statistics.fmean(ders) if ders else None,
                "der_median": statistics.median(ders) if ders else None,
                "rtf_total": sum(r.seconds or 0.0 for r in rs) / audio if audio else None,
                "audio_s_total": audio,
            }
        )
    return out


# --------------------------------------------------------------------------- backends


class SherpaBackend(_SherpaBackend):
    """The product's sherpa-onnx backend (``diarize_sherpa``), with segments as eval ``Seg`` values."""

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[Seg]:
        return [Seg(start, end, speaker) for start, end, speaker in self.process(audio, sample_rate)]


@dataclass
class PyannoteBackend:
    name: str
    pipeline_id: str
    oracle: bool
    device: str
    hf_token: str | None
    _pipeline: object | None = field(init=False, default=None, repr=False)
    _num_speakers: int | None = field(init=False, default=None, repr=False)

    def prepare(self, num_speakers: int | None) -> None:
        import inspect

        import torch
        from pyannote.audio import Pipeline

        params = inspect.signature(Pipeline.from_pretrained).parameters
        # pyannote.audio 4.x takes `token=`, 3.x takes `use_auth_token=`.
        auth_arg = "token" if "token" in params else "use_auth_token"
        pipeline = Pipeline.from_pretrained(self.pipeline_id, **{auth_arg: self.hf_token})
        if pipeline is None:
            raise RuntimeError("pipeline unavailable (model card not accepted or no valid token)")
        pipeline.to(torch.device(self.device))
        self._pipeline = pipeline
        self._num_speakers = num_speakers

    def close(self) -> None:
        self._pipeline = None

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[Seg]:
        import torch

        kwargs = {"num_speakers": self._num_speakers} if self._num_speakers else {}
        out = self._pipeline({"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": sample_rate}, **kwargs)  # type: ignore[misc]
        annotation = getattr(out, "speaker_diarization", out)  # 4.x returns an output object
        return [Seg(t.start, t.end, str(spk)) for t, _, spk in annotation.itertracks(yield_label=True)]


def build_backends(
    spec: str, devices: Sequence[str], cache_dir: Path, threshold: float, threads: int
) -> list[DiarizationBackend]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    backends: list[DiarizationBackend] = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        base, _, mode = item.partition("@")
        if mode not in ("", "oracle"):
            raise ValueError(f"unknown backend mode {mode!r} in {item!r}")
        for device in devices:
            try:
                if base in PYANNOTE_PIPELINES:
                    name = base + ("@oracle" if mode else "")
                    backends.append(PyannoteBackend(name, PYANNOTE_PIPELINES[base], bool(mode), device, token))
                elif base.startswith("sherpa:"):
                    backends.append(
                        SherpaBackend(base.split(":", 1)[1], bool(mode), device, cache_dir, threshold, threads)
                    )
                else:
                    raise ValueError(f"unknown backend {base!r}")
            except ValueError as exc:
                if "unknown" in str(exc):
                    raise
                logger.warning("skipping %s on %s: %s", item, device, exc)
    return backends


# --------------------------------------------------------------------------- blinded comparison


@dataclass(frozen=True)
class TextSeg:
    start: float
    end: float
    text: str


def load_transcript(path: Path) -> list[TextSeg]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("segments", []) if isinstance(data, dict) else data
    return [
        TextSeg(float(i["start"]), float(i["end"]), str(i["text"]).strip())
        for i in items
        if str(i.get("text", "")).strip()
    ]


def assign_speaker(segment: TextSeg, diarization: Sequence[Seg]) -> str | None:
    """Speaker with the largest total overlap with the segment, or None without any overlap."""
    overlap: dict[str, float] = {}
    for d in diarization:
        o = min(segment.end, d.end) - max(segment.start, d.start)
        if o > 0:
            overlap[d.speaker] = overlap.get(d.speaker, 0.0) + o
    if not overlap:
        return None
    return max(sorted(overlap), key=lambda k: overlap[k])


def render_blind_text(transcript: Sequence[TextSeg], diarization: Sequence[Seg]) -> str:
    names: dict[str, str] = {}
    lines = []
    for seg in sorted(transcript, key=lambda s: s.start):
        raw = assign_speaker(seg, diarization)
        if raw is None:
            label = "SPEAKER_?"
        else:
            label = names.setdefault(raw, f"SPEAKER_{len(names) + 1}")
        mm, ss = divmod(int(seg.start), 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {label}: {seg.text}")
    return "\n".join(lines) + "\n"


def write_blind(
    out_dir: Path,
    transcript: Sequence[TextSeg],
    outputs: dict[str, Sequence[Seg]],
    rng: random.Random | None = None,
) -> dict[str, str]:
    """One ``version-<letter>.txt`` per backend in random order plus ``key.json`` (letter -> backend)."""
    rng = rng or random.Random()
    names = list(outputs)
    rng.shuffle(names)
    key: dict[str, str] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        letter = chr(ord("A") + i)
        (out_dir / f"version-{letter}.txt").write_text(render_blind_text(transcript, outputs[name]), encoding="utf-8")
        key[letter] = name
    (out_dir / "key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
    return key


async def _transcribe_regions(audio: np.ndarray, regions: Sequence[Region], max_len_s: float = 15.0) -> list[TextSeg]:
    from sales_copilot.core.config import TranscriberConfig, load_env
    from sales_copilot.modules.transcriber.backends import create_backend

    load_env()
    cfg = TranscriberConfig.from_env()
    backend = create_backend(
        {
            "backend": cfg.backend,
            "language": cfg.language,
            "model_repo": f"mlx-community/whisper-{cfg.model}",
            "whisper_cpp_binary": cfg.whisper_cpp_binary,
            "whisper_cpp_model_path": cfg.whisper_cpp_model_path,
            "whisper_cpp_threads": cfg.whisper_cpp_threads,
        }
    )
    await backend.warmup()
    out: list[TextSeg] = []
    try:
        for start, end in regions:
            t = start
            while t < end:
                stop = min(end, t + max_len_s)
                text = (await backend.transcribe(audio[int(t * SAMPLE_RATE) : int(stop * SAMPLE_RATE)])).strip()
                if text:
                    out.append(TextSeg(t, stop, text))
                t = stop
    finally:
        await backend.stop()
    return out


# --------------------------------------------------------------------------- privacy guard


def check_output_dir(out: Path, inputs: Sequence[Path], repo_root: Path = REPO_ROOT) -> None:
    """Refuse an output directory inside the repository when any input lies outside it."""
    out_r, root = out.expanduser().resolve(), repo_root.resolve()
    if not out_r.is_relative_to(root):
        return
    for path in inputs:
        if not path.expanduser().resolve().is_relative_to(root):
            raise ValueError(
                f"--out {out_r} is inside the repository but {path} is not: "
                "customer recordings must not land in the repo tree"
            )


# --------------------------------------------------------------------------- inputs and main


@dataclass
class Prepared:
    label: str
    audio: np.ndarray
    reference: list[Seg] | None
    expected_speakers: int | None
    source: str = ""


def prepare_session(label: str, session: Path, args: argparse.Namespace) -> Prepared | None:
    mic_path, sys_path = session / f"{MIC_LABEL}.wav", session / f"{SYSTEM_LABEL}.wav"
    if not (mic_path.exists() and sys_path.exists()):
        logger.info("skip %s: missing %s.wav or %s.wav", label, MIC_LABEL, SYSTEM_LABEL)
        return None
    mic, system = load_audio(mic_path), load_audio(sys_path)
    if args.max_session_minutes and max(len(mic), len(system)) / SAMPLE_RATE / 60 > args.max_session_minutes:
        logger.info("skip %s: longer than %s minutes", label, args.max_session_minutes)
        return None
    mic_regions, system_regions = vad_regions(mic), vad_regions(system)
    verdict = detect_loudspeaker_bleed(
        mic, system, mic_regions, system_regions, ncc_threshold=args.bleed_ncc, overlap_threshold=args.bleed_overlap
    )
    logger.info(
        "%s: ncc=%.3f mic_inside_system=%.2f -> %s",
        label, verdict.peak_ncc, verdict.mic_inside_system_share, "SKIP bleed" if verdict.bleed else "keep",
    )  # fmt: skip
    if verdict.bleed:
        logger.info("skip %s: loudspeaker bleed (%s)", label, verdict.reason)
        return None
    reference = build_reference(mic_regions, system_regions)
    return Prepared(label, mix_tracks(mic, system), reference, 2, str(session))


def prepare_single(args: argparse.Namespace) -> Prepared:
    audio = load_audio(args.audio, args.start, args.end)
    reference = None
    if args.reference:
        reference = read_rttm(args.reference, args.start or 0.0, args.end)
    return Prepared("file-1", audio, reference, args.expected_speakers, str(args.audio))


def evaluate(
    prepared: Sequence[Prepared], backends: Sequence[DiarizationBackend], out: Path
) -> tuple[list[Row], dict[str, dict[str, list[Seg]]]]:
    rows: list[Row] = []
    hyps: dict[str, dict[str, list[Seg]]] = {}
    for item in prepared:
        item_rows: list[Row] = []
        for backend in backends:
            row, hyp = run_backend(backend, item.audio, item.label, item.reference, item.expected_speakers, out)
            item_rows.append(row)
            if hyp:
                hyps.setdefault(item.label, {})[f"{backend.name}[{backend.device}]"] = hyp
            logger.info(
                "%s %s[%s]: der=%s rtf=%s %s",
                item.label, backend.name, backend.device, _fmt(row.der), _fmt(row.rtf), row.note,
            )  # fmt: skip
        write_table(item_rows, COLUMNS, out / item.label / "results.csv", out / item.label / "results.md")
        if item.reference:
            write_rttm(out / item.label / "reference.rttm", item.label, item.reference)
        rows += item_rows
    return rows, hyps


def transcribe_fragment_for_blind(audio: np.ndarray, offset: float) -> list[TextSeg]:
    """Transcribe the fragment once with the default backend; times are shifted to the source timeline."""
    regions = vad_regions(audio)
    segs = asyncio.run(_transcribe_regions(audio, regions))
    return [TextSeg(s.start + offset, s.end + offset, s.text) for s in segs]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare speaker diarization backends.", formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = p.add_argument_group("two-track session mode")
    src.add_argument(
        "--session", type=Path, action="append", default=[], help="recorder session directory (repeatable)"
    )
    src.add_argument("--sessions-root", type=Path, help="directory of session directories")
    src.add_argument(
        "--limit", type=int, default=3, help="evaluate at most N sessions from --sessions-root (default 3)"
    )
    src.add_argument("--max-session-minutes", type=float, default=None, help="skip longer sessions")
    src.add_argument(
        "--bleed-ncc",
        type=float,
        default=BLEED_NCC_THRESHOLD,
        help="skip when peak normalised cross-correlation reaches this",
    )
    src.add_argument(
        "--bleed-overlap",
        type=float,
        default=BLEED_OVERLAP_THRESHOLD,
        help="skip when this share of mic speech lies inside system speech",
    )
    one = p.add_argument_group("single-file mode")
    one.add_argument("--audio", type=Path, help="any format ffmpeg reads")
    one.add_argument("--reference", type=Path, help="hand-made RTTM")
    one.add_argument("--expected-speakers", type=int, help="true number of people who spoke")
    one.add_argument("--start", type=float, help="fragment start (s)")
    one.add_argument("--end", type=float, help="fragment end (s)")
    one.add_argument(
        "--blind",
        action="store_true",
        help="write the blinded comparison; transcribes the fragment once when no transcript is given",
    )
    one.add_argument(
        "--blind-transcript",
        type=Path,
        help="transcript JSON (segments with start, end, text, in seconds on the source timeline); implies --blind",
    )
    p.add_argument("--out", type=Path, required=True, help="output directory (outside the repo for customer audio)")
    p.add_argument("--backends", default=DEFAULT_BACKENDS, help=f"comma list (default {DEFAULT_BACKENDS})")
    p.add_argument("--device", action="append", choices=["cpu", "mps", "cuda"], help="repeatable; default cpu only")
    p.add_argument(
        "--model-dir", type=Path, default=default_cache_dir(), help="sherpa-onnx model cache (default: user cache dir)"
    )
    p.add_argument("--cluster-threshold", type=float, default=0.5, help="sherpa-onnx clustering threshold")
    p.add_argument("--threads", type=int, default=4, help="sherpa-onnx threads")
    p.add_argument("--seed", type=int, default=None, help="seed for the blinded letter order")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.audio and (args.session or args.sessions_root):
        p.error("--audio (single-file mode) cannot be combined with --session/--sessions-root")
    if not (args.audio or args.session or args.sessions_root):
        p.error("give --session, --sessions-root or --audio")
    if (args.blind or args.blind_transcript) and not (args.audio and args.start is not None and args.end is not None):
        p.error("--blind and --blind-transcript need --audio with --start and --end")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(levelname)s %(message)s"
    )
    logger.setLevel(logging.INFO)
    sessions: list[Path] = list(args.session)
    if args.sessions_root:
        sessions += sorted(d for d in args.sessions_root.iterdir() if d.is_dir())
    inputs = [args.audio] if args.audio else sessions
    try:
        check_output_dir(
            args.out,
            [
                *inputs,
                *([args.blind_transcript] if args.blind_transcript else []),
                *([args.reference] if args.reference else []),
            ],
        )
        backends = build_backends(
            args.backends, args.device or ["cpu"], args.model_dir, args.cluster_threshold, args.threads
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    out = args.out.expanduser().resolve()

    prepared: list[Prepared] = []
    skipped = 0
    if args.audio:
        prepared.append(prepare_single(args))
    else:
        limit = None if args.session and not args.sessions_root else args.limit
        for session in sessions:
            if limit is not None and len(prepared) >= limit:
                break
            item = prepare_session(f"s{len(prepared) + skipped + 1}", session, args)
            if item is None:
                skipped += 1
            else:
                prepared.append(item)
        print(f"sessions evaluated: {len(prepared)}, skipped (bleed, missing track or too long): {skipped}")
    if not prepared:
        print("Error: no usable input", file=sys.stderr)
        return 1
    if any(item.reference for item in prepared):
        try:
            require_scorer()
        except ScorerUnavailableError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / "inputs.json").write_text(
        json.dumps({p.label: p.source for p in prepared}, indent=2) + "\n", encoding="utf-8"
    )

    rows, hyps = evaluate(prepared, backends, out)
    agg = aggregate(rows)
    write_table(agg, AGGREGATE_COLUMNS, out / "aggregate.csv", out / "aggregate.md", dicts=True)  # type: ignore[arg-type]
    write_table(rows, COLUMNS, out / "results.csv", out / "results.md")
    print((out / "results.md").read_text(encoding="utf-8"))
    print((out / "aggregate.md").read_text(encoding="utf-8"))

    if args.blind or args.blind_transcript:
        item = prepared[0]
        offset = args.start or 0.0
        transcript = (
            load_transcript(args.blind_transcript)
            if args.blind_transcript
            else transcribe_fragment_for_blind(item.audio, offset)
        )
        shifted = {
            k: [Seg(s.start + offset, s.end + offset, s.speaker) for s in v]
            for k, v in hyps.get(item.label, {}).items()
        }
        in_window = [t for t in transcript if t.end > offset and t.start < (args.end or math.inf)]
        write_blind(out / "blind", in_window, shifted, random.Random(args.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
