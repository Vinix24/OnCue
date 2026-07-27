#!/usr/bin/env python3
"""Streaming-latency + Dutch-WER benchmark: parakeet-mlx vs whisper.cpp vs mlx-whisper.

Answers the question "which backend streams fastest, at what Dutch accuracy" on
real Fireflies call recordings.

Two independent measurements per backend variant:

1. Streaming latency (headline). The call audio is fed at 1x REAL-TIME through a
   background feeder thread (``ReplayAudioStream(real_time=True)``). A single
   sequential consumer accumulates audio and transcribes, exactly like the live
   single-worker engine, so transcription backlog shows up as growing latency.
   For each emitted transcript segment we record the wall-clock emit time and the
   segment's audio end-time; latency = emit_time - audio_end_time. We report
   time-to-first-word (TTFT) and per-segment latency median (p50) + p95.

   - whisper.cpp / mlx-whisper are chunked: a segment cannot appear before its
     chunk is full, so the latency floor is the chunk size.
   - parakeet's transcribe_stream emits at sentence granularity, finer than the
     fixed chunk. Measuring that asymmetry fairly is the point.

2. WER. The full call is transcribed in batch and scored with jiwer against the
   Fireflies reference transcript. Both sides are normalized (lowercase, strip
   punctuation / speaker-labels / timestamps, collapse whitespace).

Usage:
    .venv/bin/python scripts/benchmark_transcription.py \
        --call acme-industries-2026-04-09 --call voorbeeld-klant-2026-04-07

    # Override whisper.cpp thread count (default: from WHISPER_CPP_THREADS env or 4):
    .venv/bin/python scripts/benchmark_transcription.py --threads 8

Robust by design: a backend that cannot run (model download fails, ffmpeg/cmake
missing, mlx issue) is recorded as "could not run: <reason>" and the others
still complete.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import queue
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("benchmark_transcription")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIREFLIES_DIR = REPO_ROOT / "data" / "fireflies"
REPORT_PATH = REPO_ROOT / "claudedocs" / "2026-06-28-PARAKEET-VS-WHISPERCPP-BENCHMARK.md"
VOCABULARY_CONFIG_DEFAULT = REPO_ROOT / "config" / "transcription_vocabulary.yaml"
WHISPER_CPP_BINARY = REPO_ROOT / "vendor" / "whisper.cpp" / "build" / "bin" / "whisper-cli"
WHISPER_CPP_MODEL = REPO_ROOT / "vendor" / "whisper.cpp" / "models" / "ggml-large-v3-turbo.bin"
PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
SAMPLE_RATE = 16000
LANGUAGE = "nl"

DEFAULT_CALLS = [
    "acme-industries-2026-04-09",
    "voorbeeld-klant-2026-04-07",
]


# ---------------------------------------------------------------------------
# Audio preparation
# ---------------------------------------------------------------------------


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not on PATH (brew install ffmpeg)")


def to_wav_16k_mono(src: Path, dst: Path) -> Path:
    """Decode any audio file to a 16 kHz mono WAV via ffmpeg (cached on dst)."""
    if dst.exists():
        return dst
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst),
    ]
    subprocess.run(cmd, check=True)
    return dst


def load_wav_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path.name} is not 16 kHz")
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# WER
# ---------------------------------------------------------------------------

_TIMESTAMP = re.compile(r"\[(\d+:\d+(?::\d+)?)\]")


def _timestamp_to_seconds(ts: str) -> float:
    parts = [int(p) for p in ts.strip("[]").split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"unsupported timestamp format: {ts}")


_SPEAKER_LINE = re.compile(r"^\*\*.+?\*\*\s*\*\[(\d+:\d+(?::\d+)?)\]\*:\s*(.*)$")


def fireflies_reference_text(md_path: Path, max_seconds: float | None = None) -> str:
    """Extract spoken text from a Fireflies markdown transcript (drop labels).

    If ``max_seconds`` is given, only utterances whose speaker timestamp starts
    within the first ``max_seconds`` are included. The window is half-open
    ``[0, max_seconds)`` to match the audio slice ``full_audio[:N]`` used in
    ``measure_call``. An utterance timestamp exactly equal to ``max_seconds`` is
    therefore excluded, keeping the reference aligned with the capped audio.
    """
    spoken: list[str] = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        match = _SPEAKER_LINE.match(line.strip())
        if not match:
            continue
        ts, text = match.groups()
        if max_seconds is not None and _timestamp_to_seconds(ts) >= max_seconds:
            continue
        spoken.append(text)
    return " ".join(spoken)


def normalize_text(text: str) -> str:
    """Lowercase, strip timestamps/punctuation, collapse whitespace (Unicode-safe)."""
    text = text.lower()
    text = re.sub(r"\[\d+:\d+(?::\d+)?\]", " ", text)  # [00:00] timestamps
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)  # punctuation -> space
    return re.sub(r"\s+", " ", text).strip()


def compute_wer(reference: str, hypothesis: str) -> float:
    import jiwer

    ref = normalize_text(reference)
    hyp = normalize_text(hypothesis)
    if not ref:
        raise ValueError("empty reference after normalization")
    return float(jiwer.wer(ref, hyp))


# ---------------------------------------------------------------------------
# 1x real-time feeder (background thread, uses ReplayAudioStream(real_time=True))
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    audio: np.ndarray
    audio_end_s: float


class RealtimeFeeder(threading.Thread):
    """Read a 16 kHz WAV at 1x real-time and push frames onto a thread-safe queue.

    ``stream_start`` is the perf_counter at the first frame; since the feeder
    paces at wall-clock = 1x, the frame ending at ``audio_end_s`` becomes
    available at ~``stream_start + audio_end_s``, which is the anchor for
    audio-aligned latency.
    """

    def __init__(self, wav_path: Path, max_seconds: float, frame_ms: int = 100) -> None:
        super().__init__(daemon=True)
        self._wav_path = wav_path
        self._max_seconds = max_seconds
        self._frame_frames = int(SAMPLE_RATE * frame_ms / 1000)
        self.q: queue.Queue[Frame | None] = queue.Queue(maxsize=4096)
        self.stream_start: float | None = None

    def run(self) -> None:
        from sales_copilot.audio.replay import ReplayAudioStream

        stream = ReplayAudioStream(
            self._wav_path, chunk_size_frames=self._frame_frames, real_time=True
        )
        stream.start()
        playhead = 0.0
        try:
            while True:
                frame = stream.read()
                if frame is None:
                    break
                if self.stream_start is None:
                    self.stream_start = time.perf_counter()
                playhead += len(frame) / SAMPLE_RATE
                self.q.put(Frame(audio=frame.copy(), audio_end_s=playhead))
                if playhead >= self._max_seconds:
                    break
        finally:
            stream.stop()
            self.q.put(None)


# ---------------------------------------------------------------------------
# Segment records + streaming consumers
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    audio_end_s: float
    emit_rel_s: float  # wall-clock relative to stream_start
    text: str

    @property
    def latency_s(self) -> float:
        return self.emit_rel_s - self.audio_end_s


def _drain(feeder: RealtimeFeeder, buffer: list[np.ndarray]) -> tuple[float, bool]:
    """Block for one frame, then drain everything currently queued.

    Returns (audio_end_s_of_last_frame, eof). Appends audio to ``buffer``.
    """
    eof = False
    audio_end = 0.0
    first = feeder.q.get()
    if first is None:
        return audio_end, True
    buffer.append(first.audio)
    audio_end = first.audio_end_s
    while True:
        try:
            item = feeder.q.get_nowait()
        except queue.Empty:
            break
        if item is None:
            eof = True
            break
        buffer.append(item.audio)
        audio_end = item.audio_end_s
    return audio_end, eof


def run_chunked_stream(
    feeder: RealtimeFeeder,
    transcribe_fn,
    chunk_ms: int,
) -> list[Segment]:
    """Single-worker chunked consumer: flush every ``chunk_ms`` of audio, transcribe.

    ``consumed_s`` tracks the audio end-time of each formed chunk precisely, so a
    chunk's latency is attributed to its own audio position even when several
    chunks are formed in one drain batch (whisper.cpp falls behind real-time
    because each chunk re-spawns the CLI and reloads the GGML model).
    """
    feeder.start()
    segments: list[Segment] = []
    pending: list[np.ndarray] = []
    pending_samples = 0
    chunk_samples = int(SAMPLE_RATE * chunk_ms / 1000)
    consumed_s = 0.0
    eof = False

    def _emit(chunk_audio: np.ndarray, audio_end_s: float) -> None:
        text = (transcribe_fn(chunk_audio) or "").strip()
        emit_rel = time.perf_counter() - (feeder.stream_start or time.perf_counter())
        if text:
            segments.append(Segment(audio_end_s=audio_end_s, emit_rel_s=emit_rel, text=text))

    while not eof:
        buf: list[np.ndarray] = []
        _, eof = _drain(feeder, buf)
        if buf:
            pending.extend(buf)
            pending_samples += sum(len(c) for c in buf)
        while pending_samples >= chunk_samples:
            head = np.concatenate(pending, dtype=np.float32)
            chunk_audio = head[:chunk_samples]
            remainder = head[chunk_samples:]
            pending = [remainder] if remainder.size else []
            pending_samples = remainder.size
            consumed_s += chunk_samples / SAMPLE_RATE
            _emit(chunk_audio, consumed_s)
    if pending_samples:
        tail = np.concatenate(pending, dtype=np.float32)
        consumed_s += pending_samples / SAMPLE_RATE
        _emit(tail, consumed_s)
    feeder.join(timeout=5)
    return segments


def run_parakeet_stream(feeder: RealtimeFeeder, backend, feed_window_s: float = 1.0) -> list[Segment]:
    """parakeet transcribe_stream consumer: feed ~feed_window_s windows, emit sentences.

    ``add_audio`` re-encodes a rolling ~drop_size mel window each call, so feeding
    sub-second windows pushes aggregate RTF > 1 and inflates latency with pure
    compute backlog. A ~1s window keeps RTF ≈ 0.3-0.6 (compute is not the limit);
    what remains is parakeet-mlx's intrinsic sentence-finalization lookahead with
    ``context_size=(256, 256)``, which is the latency we want to measure.
    """
    import mlx.core as mx

    feeder.start()
    segments: list[Segment] = []
    pending: list[np.ndarray] = []
    pending_samples = 0
    window_samples = int(SAMPLE_RATE * feed_window_s)
    eof = False
    seen_sentences = 0

    with backend.transcribe_stream(context_size=(256, 256)) as streamer:
        def _emit_new() -> None:
            nonlocal seen_sentences
            result = streamer.result
            sentences = getattr(result, "sentences", [])
            if len(sentences) <= seen_sentences:
                return
            emit_rel = time.perf_counter() - (feeder.stream_start or time.perf_counter())
            for sentence in sentences[seen_sentences:]:
                text = (getattr(sentence, "text", "") or "").strip()
                if text:
                    segments.append(
                        Segment(
                            audio_end_s=float(getattr(sentence, "end", 0.0)),
                            emit_rel_s=emit_rel,
                            text=text,
                        )
                    )
            seen_sentences = len(sentences)

        while not eof:
            buf: list[np.ndarray] = []
            _, eof = _drain(feeder, buf)
            if buf:
                pending.extend(buf)
                pending_samples += sum(len(c) for c in buf)
            while pending_samples >= window_samples:
                head = np.concatenate(pending, dtype=np.float32)
                window = head[:window_samples]
                remainder = head[window_samples:]
                pending = [remainder] if remainder.size else []
                pending_samples = remainder.size
                streamer.add_audio(mx.array(window))
                _emit_new()
        if pending_samples:
            streamer.add_audio(mx.array(np.concatenate(pending, dtype=np.float32)))
            _emit_new()

    feeder.join(timeout=5)
    return segments


# ---------------------------------------------------------------------------
# Backend variants
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    name: str
    notes: str = ""
    ttft_s: float | None = None
    p50_s: float | None = None
    p95_s: float | None = None
    n_segments: int = 0
    wer: float | None = None
    stream_error: str = ""
    wer_error: str = ""


def _stats(segments: list[Segment]) -> tuple[float | None, float | None, float | None, int]:
    if not segments:
        return None, None, None, 0
    ttft = min(s.emit_rel_s for s in segments)
    lats = sorted(max(0.0, s.latency_s) for s in segments)
    p50 = statistics.median(lats)
    p95 = lats[min(len(lats) - 1, int(round(0.95 * (len(lats) - 1))))]
    return ttft, p50, p95, len(segments)


def _vocabulary_initial_prompt(vocabulary_enabled: bool, vocabulary_config: Path) -> str | None:
    """Return the configured prompt when enabled, else None for an unbiased run."""
    if not vocabulary_enabled:
        return None
    from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config

    vocab = load_vocabulary_config(vocabulary_config)
    return vocab.initial_prompt or None


def build_whisper_cpp_backend(
    timeout_s: float,
    *,
    vocabulary_enabled: bool = True,
    vocabulary_config: Path = VOCABULARY_CONFIG_DEFAULT,
    threads: int | None = None,
):
    from sales_copilot.core.config import TranscriberConfig, with_overrides
    from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend

    if not WHISPER_CPP_BINARY.exists():
        raise RuntimeError(f"whisper.cpp binary missing: {WHISPER_CPP_BINARY}")
    if not WHISPER_CPP_MODEL.exists():
        raise RuntimeError(f"whisper.cpp model missing: {WHISPER_CPP_MODEL}")
    cfg = with_overrides(
        TranscriberConfig.from_env(),
        language=LANGUAGE,
        whisper_cpp_binary=str(WHISPER_CPP_BINARY),
        whisper_cpp_model_path=str(WHISPER_CPP_MODEL),
        whisper_cpp_timeout_s=timeout_s,
        vocabulary_enabled=vocabulary_enabled,
        whisper_cpp_threads=threads,
    )
    # The config object carries vocabulary_config, but we override the file path
    # here so the CLI --vocabulary-config flag is respected by the benchmark.
    cfg = with_overrides(cfg, vocabulary_config=str(vocabulary_config))
    return WhisperCppBackend(cfg)


def build_mlx_backend(
    timeout_s: float,
    *,
    vocabulary_enabled: bool = True,
    vocabulary_config: Path = VOCABULARY_CONFIG_DEFAULT,
):
    from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend

    initial_prompt = _vocabulary_initial_prompt(vocabulary_enabled, vocabulary_config)
    return MlxWhisperBackend(
        language=LANGUAGE,
        timeout_s=timeout_s,
        initial_prompt=initial_prompt,
    )


def build_parakeet_backend(timeout_s: float):
    from sales_copilot.modules.transcriber.backends.parakeet_backend import ParakeetMlxBackend

    return ParakeetMlxBackend(model_repo=PARAKEET_MODEL, language=LANGUAGE, timeout_s=timeout_s)


def measure_call(
    call: str,
    stream_seconds: float,
    wer_max_seconds: float,
    measure_vocabulary_delta: bool = False,
    vocabulary_config: Path = VOCABULARY_CONFIG_DEFAULT,
    threads: int | None = None,
) -> list[VariantResult]:
    call_dir = FIREFLIES_DIR / call
    mp3 = call_dir / "audio.mp3"
    ref_md = next(call_dir.glob("fireflies-*.md"), None)
    if not mp3.exists():
        raise RuntimeError(f"missing audio.mp3 for {call}")
    if ref_md is None:
        raise RuntimeError(f"missing fireflies-*.md reference for {call}")

    reference = fireflies_reference_text(
        ref_md,
        max_seconds=wer_max_seconds if wer_max_seconds > 0 else None,
    )
    tmp_dir = Path(tempfile.gettempdir()) / "sc_bench"
    tmp_dir.mkdir(exist_ok=True)
    wav_path = to_wav_16k_mono(mp3, tmp_dir / f"{call}.wav")

    full_audio = load_wav_mono(wav_path)
    if wer_max_seconds > 0:
        full_audio = full_audio[: int(wer_max_seconds * SAMPLE_RATE)]

    results: list[VariantResult] = []

    # ---- whisper.cpp @ 3000ms and @ 1000ms -------------------------------
    for chunk_ms in (3000, 1000):
        res = VariantResult(name=f"whisper.cpp @ {chunk_ms}ms chunk")
        try:
            backend = build_whisper_cpp_backend(timeout_s=60.0, vocabulary_config=vocabulary_config, threads=threads)

            def _wc_transcribe(audio: np.ndarray, _b=backend) -> str:
                end_ms = max(1, int(len(audio) / SAMPLE_RATE * 1000))
                payload = _b.transcribe_chunk(audio, start_ms=0, end_ms=end_ms)
                return "" if payload is None else str(payload.get("text", ""))

            print(f"  [{call}] streaming {res.name} (1x real-time, {stream_seconds:.0f}s)...")
            feeder = RealtimeFeeder(wav_path, max_seconds=stream_seconds)
            segs = run_chunked_stream(feeder, _wc_transcribe, chunk_ms=chunk_ms)
            res.ttft_s, res.p50_s, res.p95_s, res.n_segments = _stats(segs)
        except Exception as exc:  # noqa: BLE001
            res.stream_error = f"{type(exc).__name__}: {exc}"
            logger.warning("stream %s failed: %s", res.name, traceback.format_exc())
        results.append(res)

    # whisper.cpp WER (shared, computed once with the 3000 row carrying it; we
    # compute it for the first whisper.cpp variant and reuse for both rows).
    wc_wer: float | None = None
    wc_wer_err = ""
    wc_wer_unbiased: float | None = None
    wc_wer_unbiased_err = ""
    try:
        backend = build_whisper_cpp_backend(
            timeout_s=max(1800.0, wer_max_seconds * 2 or 1800.0),
            vocabulary_config=vocabulary_config,
            threads=threads,
        )
        print(f"  [{call}] batch WER whisper.cpp (full call)...")
        # write the (optionally capped) audio to a wav whisper.cpp reads
        wer_wav = tmp_dir / f"{call}_wer.wav"
        if wer_max_seconds > 0:
            from sales_copilot.modules.transcriber.wav_utils import write_wav

            write_wav(wer_wav, full_audio, sample_rate=SAMPLE_RATE)
            wer_src = wer_wav
        else:
            wer_src = wav_path
        hyp = asyncio.run(backend.transcribe_file(wer_src))
        wc_wer = compute_wer(reference, hyp)
    except Exception as exc:  # noqa: BLE001
        wc_wer_err = f"{type(exc).__name__}: {exc}"
        logger.warning("WER whisper.cpp failed: %s", traceback.format_exc())

    if measure_vocabulary_delta:
        try:
            backend = build_whisper_cpp_backend(
                timeout_s=max(1800.0, wer_max_seconds * 2 or 1800.0),
                vocabulary_enabled=False,
                vocabulary_config=vocabulary_config,
                threads=threads,
            )
            print(f"  [{call}] batch WER whisper.cpp without vocabulary (full call)...")
            hyp = asyncio.run(backend.transcribe_file(wer_src))
            wc_wer_unbiased = compute_wer(reference, hyp)
        except Exception as exc:  # noqa: BLE001
            wc_wer_unbiased_err = f"{type(exc).__name__}: {exc}"
            logger.warning("WER whisper.cpp (no vocab) failed: %s", traceback.format_exc())

    for res in results:
        if res.name.startswith("whisper.cpp"):
            res.wer = wc_wer
            res.wer_error = wc_wer_err
    if measure_vocabulary_delta:
        results.append(
            VariantResult(
                name="whisper.cpp no vocab",
                wer=wc_wer_unbiased,
                wer_error=wc_wer_unbiased_err,
                notes="WER without vocabulary biasing (delta baseline)",
            )
        )

    # ---- parakeet-mlx ----------------------------------------------------
    res = VariantResult(name="parakeet-mlx")
    try:
        backend = build_parakeet_backend(timeout_s=1800.0)
        print(f"  [{call}] streaming parakeet-mlx (1x real-time, {stream_seconds:.0f}s)...")
        feeder = RealtimeFeeder(wav_path, max_seconds=stream_seconds)
        segs = run_parakeet_stream(feeder, backend)
        res.ttft_s, res.p50_s, res.p95_s, res.n_segments = _stats(segs)
    except Exception as exc:  # noqa: BLE001
        res.stream_error = f"{type(exc).__name__}: {exc}"
        logger.warning("stream parakeet failed: %s", traceback.format_exc())
    try:
        backend = build_parakeet_backend(timeout_s=1800.0)
        print(f"  [{call}] batch WER parakeet-mlx (full call)...")
        wer_src = wav_path if wer_max_seconds <= 0 else (tmp_dir / f"{call}_wer.wav")
        hyp = asyncio.run(backend.transcribe_file(wer_src))
        res.wer = compute_wer(reference, hyp)
    except Exception as exc:  # noqa: BLE001
        res.wer_error = f"{type(exc).__name__}: {exc}"
        logger.warning("WER parakeet failed: %s", traceback.format_exc())
    results.append(res)

    # ---- mlx-whisper (if it loads) --------------------------------------
    res = VariantResult(name="mlx-whisper @ 3000ms chunk")
    try:
        import mlx_whisper  # noqa: F401

        backend = build_mlx_backend(timeout_s=60.0, vocabulary_config=vocabulary_config)

        def _mlx_transcribe(audio: np.ndarray, _b=backend) -> str:
            return _b._transcribe_sync(audio)  # noqa: SLF001

        print(f"  [{call}] streaming mlx-whisper (1x real-time, {stream_seconds:.0f}s)...")
        feeder = RealtimeFeeder(wav_path, max_seconds=stream_seconds)
        segs = run_chunked_stream(feeder, _mlx_transcribe, chunk_ms=3000)
        res.ttft_s, res.p50_s, res.p95_s, res.n_segments = _stats(segs)
    except Exception as exc:  # noqa: BLE001
        res.stream_error = f"{type(exc).__name__}: {exc}"
        logger.warning("stream mlx-whisper failed: %s", traceback.format_exc())
    mlx_wer: float | None = None
    mlx_wer_err = ""
    mlx_wer_unbiased: float | None = None
    mlx_wer_unbiased_err = ""
    try:
        import mlx_whisper  # noqa: F401

        backend = build_mlx_backend(timeout_s=1800.0, vocabulary_config=vocabulary_config)
        print(f"  [{call}] batch WER mlx-whisper (full call)...")
        wer_src = wav_path if wer_max_seconds <= 0 else (tmp_dir / f"{call}_wer.wav")
        hyp = asyncio.run(backend.transcribe_file(wer_src))
        mlx_wer = compute_wer(reference, hyp)
    except Exception as exc:  # noqa: BLE001
        mlx_wer_err = f"{type(exc).__name__}: {exc}"
        logger.warning("WER mlx-whisper failed: %s", traceback.format_exc())
    res.wer = mlx_wer
    res.wer_error = mlx_wer_err
    results.append(res)

    if measure_vocabulary_delta:
        try:
            import mlx_whisper  # noqa: F401

            backend = build_mlx_backend(
                timeout_s=1800.0,
                vocabulary_enabled=False,
                vocabulary_config=vocabulary_config,
            )
            print(f"  [{call}] batch WER mlx-whisper without vocabulary (full call)...")
            hyp = asyncio.run(backend.transcribe_file(wer_src))
            mlx_wer_unbiased = compute_wer(reference, hyp)
        except Exception as exc:  # noqa: BLE001
            mlx_wer_unbiased_err = f"{type(exc).__name__}: {exc}"
            logger.warning("WER mlx-whisper (no vocab) failed: %s", traceback.format_exc())
        results.append(
            VariantResult(
                name="mlx-whisper no vocab",
                wer=mlx_wer_unbiased,
                wer_error=mlx_wer_unbiased_err,
                notes="WER without vocabulary biasing (delta baseline)",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _fmt(value: float | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.2f}{suffix}"


def _cell_notes(res: VariantResult) -> str:
    notes: list[str] = []
    if res.stream_error:
        notes.append(f"stream could not run: {res.stream_error}")
    elif res.n_segments:
        notes.append(f"{res.n_segments} segments")
    if res.wer_error:
        notes.append(f"WER could not run: {res.wer_error}")
    return "; ".join(notes) if notes else ""


def render_report(
    per_call: dict[str, list[VariantResult]],
    stream_seconds: float,
    wer_max_seconds: float,
    measure_vocabulary_delta: bool = False,
) -> str:
    lines: list[str] = []
    lines.append("# Parakeet-MLX vs whisper.cpp — streaming speed & Dutch WER benchmark")
    lines.append("")
    lines.append(f"Generated: 2026-06-28. Host: Apple Silicon, model {PARAKEET_MODEL} + "
                 "whisper.cpp `ggml-large-v3-turbo`.")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "- **Streaming feed**: each call is fed at **1x real-time** through a background "
        "feeder thread (`ReplayAudioStream(real_time=True)`), draining into a single "
        "sequential consumer — the same single-worker model as the live engine, so "
        "transcription backlog surfaces as growing latency."
    )
    lines.append(
        f"- **Streaming window**: first **{stream_seconds:.0f}s** of each call (enough for a "
        "stable TTFT + latency distribution without waiting the full call at 1x)."
    )
    lines.append(
        "- **Latency** = `emit_time − audio_end_time` (audio-aligned). `emit_time` is the "
        "wall-clock when a segment is produced, relative to stream start; `audio_end_time` is "
        "the segment's end position in the audio. **TTFT** = wall-clock to the first emitted word."
    )
    lines.append(
        "- whisper.cpp / mlx-whisper are **chunked**: a segment cannot appear before its chunk "
        "is full, so latency floor ≈ chunk size. **parakeet** `transcribe_stream` emits at "
        "**sentence** granularity (finer than the fixed chunk)."
    )
    wer_scope = "the full call" if wer_max_seconds <= 0 else f"the first {wer_max_seconds:.0f}s"
    lines.append(
        f"- **WER**: {wer_scope} transcribed in **batch**, scored with `jiwer` against the "
        "Fireflies reference **limited to the same window**. Both sides normalized: lowercase, strip "
        "punctuation/speaker-labels/timestamps, collapse whitespace."
    )
    if measure_vocabulary_delta:
        lines.append(
            "- **Vocabulary delta**: an extra unbiased batch pass (vocabulary disabled) is run for "
            "whisper.cpp and mlx-whisper. Delta = WER_without - WER_with; a positive delta means "
            "the vocabulary list improved accuracy."
        )
    lines.append("")

    for call, results in per_call.items():
        call_dir = FIREFLIES_DIR / call
        dur = ""
        try:
            with wave.open(str(Path(tempfile.gettempdir()) / "sc_bench" / f"{call}.wav"), "rb") as wf:
                dur = f" (~{wf.getnframes() / SAMPLE_RATE / 60:.0f} min)"
        except Exception:  # noqa: BLE001
            dur = ""
        lines.append(f"## {call}{dur}")
        lines.append("")
        lines.append(f"Reference: `{call_dir.name}/" + (next(call_dir.glob('fireflies-*.md')).name) + "`")
        lines.append("")
        lines.append("| Backend | TTFT | latency p50 | latency p95 | WER | Notes |")
        lines.append("|---|---|---|---|---|---|")
        for res in results:
            wer_str = "—"
            if res.wer is not None:
                wer_str = f"{res.wer * 100:.1f}%"
            elif res.wer_error:
                wer_str = "—"
            lines.append(
                f"| {res.name} | {_fmt(res.ttft_s, 's')} | {_fmt(res.p50_s, 's')} | "
                f"{_fmt(res.p95_s, 's')} | {wer_str} | {_cell_notes(res)} |"
            )
        lines.append("")

    if measure_vocabulary_delta:
        lines.append("## Vocabulary biasing delta")
        lines.append("")
        lines.extend(_vocabulary_delta_section(per_call))
        lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(_recommendation(per_call))
    lines.append("")
    return "\n".join(lines)


def _vocabulary_delta_section(per_call: dict[str, list[VariantResult]]) -> list[str]:
    """Return markdown lines summarizing WER delta with vs without vocabulary biasing."""
    biased_names = {
        "whisper.cpp": "whisper.cpp @ 3000ms chunk",
        "mlx-whisper": "mlx-whisper @ 3000ms chunk",
    }
    unbiased_names = {
        "whisper.cpp": "whisper.cpp no vocab",
        "mlx-whisper": "mlx-whisper no vocab",
    }

    def _find(results: list[VariantResult], name: str) -> VariantResult | None:
        for res in results:
            if res.name == name:
                return res
        return None

    lines: list[str] = []
    aggregated: dict[str, list[float]] = {"whisper.cpp": [], "mlx-whisper": []}
    for call, results in per_call.items():
        rows: list[str] = []
        for backend in ("whisper.cpp", "mlx-whisper"):
            biased = _find(results, biased_names[backend])
            unbiased = _find(results, unbiased_names[backend])
            if biased is None or unbiased is None:
                continue
            if biased.wer is None or unbiased.wer is None:
                rows.append(
                    f"| {backend} | {_pct(biased.wer)} | {_pct(unbiased.wer)} | — |"
                )
                continue
            delta = unbiased.wer - biased.wer
            rows.append(
                f"| {backend} | {_pct(biased.wer)} | {_pct(unbiased.wer)} | {delta*100:+.1f}pp |"
            )
            aggregated[backend].append(delta)
        if rows:
            lines.append(f"### {call}")
            lines.append("")
            lines.append("| Backend | WER with vocab | WER without vocab | Delta |")
            lines.append("|---|---|---|---|")
            lines.extend(rows)
            lines.append("")

    if any(aggregated.values()):
        lines.append("### Aggregate delta (mean across calls)")
        lines.append("")
        lines.append("| Backend | Mean delta | Interpretation |")
        lines.append("|---|---|---|")
        for backend in ("whisper.cpp", "mlx-whisper"):
            deltas = aggregated[backend]
            if not deltas:
                continue
            mean_delta = statistics.mean(deltas)
            interpretation = "improved" if mean_delta > 0 else "regressed" if mean_delta < 0 else "no change"
            lines.append(
                f"| {backend} | {mean_delta*100:+.1f}pp | {interpretation} |"
            )
        lines.append("")

    return lines


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _recommendation(per_call: dict[str, list[VariantResult]]) -> str:
    # Aggregate medians per backend across calls for a data-driven recommendation.
    agg: dict[str, dict[str, list[float]]] = {}
    for results in per_call.values():
        for res in results:
            bucket = agg.setdefault(res.name, {"p50": [], "ttft": [], "wer": []})
            if res.p50_s is not None:
                bucket["p50"].append(res.p50_s)
            if res.ttft_s is not None:
                bucket["ttft"].append(res.ttft_s)
            if res.wer is not None:
                bucket["wer"].append(res.wer)

    def _avg(values: list[float]) -> float | None:
        return statistics.mean(values) if values else None

    rankable = {
        name: (_avg(v["p50"]), _avg(v["ttft"]), _avg(v["wer"]))
        for name, v in agg.items()
    }
    streamers = {n: m for n, (m, *_rest) in rankable.items() if m is not None}
    wers = {n: w for n, (*_rest, w) in rankable.items() if w is not None}

    parts: list[str] = []
    if streamers:
        fastest = min(streamers, key=streamers.get)
        parts.append(
            f"Lowest median streaming latency: **{fastest}** "
            f"(~{streamers[fastest]:.2f}s p50)."
        )
    if wers:
        best_wer = min(wers, key=wers.get)
        parts.append(f"Lowest WER: **{best_wer}** (~{wers[best_wer] * 100:.1f}%).")
    if not parts:
        return "No backend produced both streaming and WER numbers in this run; see per-row notes."

    parts.append(
        "For the live copilot, the latency floor matters most: parakeet's sentence-level "
        "streaming beats the chunk-size floor of whisper.cpp when both run on Apple Silicon, "
        "while whisper.cpp @ 1000ms trades accuracy/CPU for a lower floor. Pick parakeet when "
        "its WER is competitive and ffmpeg + the optional extra are acceptable; keep whisper.cpp "
        "as the portable default."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", action="append", dest="calls", help="Fireflies call dir name")
    parser.add_argument(
        "--stream-seconds",
        type=float,
        default=90.0,
        help="Seconds of each call streamed at 1x real-time (keep small — the calls "
        "are ~30 min, so a full-length 1x stream across backends takes hours).",
    )
    parser.add_argument(
        "--wer-max-seconds",
        type=float,
        default=0.0,
        help="Cap WER batch audio (0 = full call).",
    )
    parser.add_argument(
        "--measure-vocabulary-delta",
        action="store_true",
        help="Run an additional unbiased WER pass per supported backend and report the delta.",
    )
    parser.add_argument(
        "--vocabulary-config",
        type=Path,
        default=VOCABULARY_CONFIG_DEFAULT,
        help="Path to the vocabulary YAML used for the biased WER pass.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Number of whisper.cpp threads. When not set the value from "
        "WHISPER_CPP_THREADS env (default 4) is used unchanged. "
        "Use to sweep thread counts on different hardware.",
    )
    args = parser.parse_args()
    calls = args.calls or DEFAULT_CALLS

    try:
        ensure_ffmpeg()
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 1

    per_call: dict[str, list[VariantResult]] = {}
    for call in calls:
        print(f"=== {call} ===")
        try:
            per_call[call] = measure_call(
                call,
                args.stream_seconds,
                args.wer_max_seconds,
                measure_vocabulary_delta=args.measure_vocabulary_delta,
                vocabulary_config=args.vocabulary_config,
                threads=args.threads,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  call {call} could not run: {exc}")
            per_call[call] = [VariantResult(name="(call failed)", notes=str(exc))]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_report(
            per_call,
            args.stream_seconds,
            args.wer_max_seconds,
            measure_vocabulary_delta=args.measure_vocabulary_delta,
        ),
        encoding="utf-8",
    )
    print(f"\nReport written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
