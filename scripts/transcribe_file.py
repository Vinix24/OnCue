#!/usr/bin/env python3
"""Batch-transcribe a single audio file using the configured Whisper backend.

Usage:
    python scripts/transcribe_file.py path/to/recording.wav
    python scripts/transcribe_file.py path/to/recording.mp3 --output out.txt
    python scripts/transcribe_file.py path/to/recording.wav --backend whisper.cpp
    python scripts/transcribe_file.py path/to/recording.wav --model large-v3

The script reads .env for backend config (WHISPER_BACKEND, model paths, etc.)
and writes the transcript to stdout (or --output path). `.env` keeps the live
default (large-v3-turbo); `--model` overrides it for this run only, e.g. for
higher-quality post-call transcription.

Supports .wav natively; for .mp3/.m4a/.mp4/.ogg it shells out to ffmpeg to
convert to 16 kHz mono PCM first (requires ffmpeg in PATH).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from sales_copilot.core.config import TranscriberConfig, load_env
from sales_copilot.modules.transcriber.backends import create_backend

logger = logging.getLogger(__name__)


_SUPPORTED_DIRECT = {".wav"}
_SUPPORTED_VIA_FFMPEG = {".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".aac", ".webm"}


def _convert_to_wav_16k_mono(source: Path, dest: Path) -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install via `brew install ffmpeg` to "
            "transcribe non-wav files."
        )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-sample_fmt",
        "s16",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-500:]}")


def _load_wav_as_float32(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        if channels != 1:
            raise ValueError(f"Expected mono WAV, got {channels} channels")
        if rate != 16000:
            raise ValueError(f"Expected 16kHz WAV, got {rate} Hz")
        raw = wf.readframes(wf.getnframes())
    int16 = np.frombuffer(raw, dtype=np.int16)
    return int16.astype(np.float32) / 32768.0, rate


def _chunk_audio(audio: np.ndarray, sample_rate: int, chunk_seconds: int = 30) -> list[tuple[np.ndarray, int, int]]:
    """Split audio into chunks of `chunk_seconds`. Returns (audio, start_ms, end_ms)."""
    chunk_frames = chunk_seconds * sample_rate
    chunks: list[tuple[np.ndarray, int, int]] = []
    offset = 0
    total = len(audio)
    while offset < total:
        end = min(offset + chunk_frames, total)
        start_ms = int(offset / sample_rate * 1000)
        end_ms = int(end / sample_rate * 1000)
        chunks.append((audio[offset:end], start_ms, end_ms))
        offset = end
    return chunks


def _resolve_ggml_path(configured_path: str, model_name: str | None) -> str:
    """Resolve the whisper.cpp GGML model path for this run.

    When `model_name` overrides `.env`'s WHISPER_MODEL, the file name changes
    but the directory from WHISPER_CPP_MODEL_PATH stays authoritative (an
    operator may point it somewhere other than the default vendor/ layout).
    Falling back silently to the configured model on a missing override would
    let an operator believe they measured large-v3 while actually measuring
    turbo, so this raises instead.
    """
    if model_name is None:
        return configured_path
    if "/" in model_name or "\\" in model_name or ".." in model_name:
        raise FileNotFoundError(
            f"Invalid --model {model_name!r}: a model name must be a bare "
            "filename component (no '/', '\\', or '..'), not a path."
        )
    candidate = Path(configured_path).with_name(f"ggml-{model_name}.bin")
    if not candidate.exists():
        raise FileNotFoundError(
            f"GGML model not found for --model {model_name}: expected at "
            f"{candidate}. Download it with: "
            f"bash vendor/whisper.cpp/models/download-ggml-model.sh {model_name}"
        )
    return str(candidate)


async def _transcribe(
    audio_path: Path,
    backend_name: str | None,
    chunk_seconds: int,
    include_timestamps: bool,
    model_name: str | None = None,
) -> str:
    load_env()
    cfg = TranscriberConfig.from_env()
    model = model_name or cfg.model
    effective_backend = backend_name or cfg.backend
    whisper_cpp_model_path = (
        _resolve_ggml_path(cfg.whisper_cpp_model_path, model_name)
        if effective_backend == "whisper.cpp"
        else cfg.whisper_cpp_model_path
    )
    backend = create_backend({
        "backend": effective_backend,
        "language": cfg.language,
        "model_repo": f"mlx-community/whisper-{model}",
        "whisper_cpp_binary": cfg.whisper_cpp_binary,
        "whisper_cpp_model_path": whisper_cpp_model_path,
        "whisper_cpp_threads": cfg.whisper_cpp_threads,
    })

    logger.info("Loading audio: %s", audio_path)
    audio, sample_rate = _load_wav_as_float32(audio_path)
    duration_s = len(audio) / float(sample_rate)
    logger.info("Audio loaded: %.1f seconds (%d samples)", duration_s, len(audio))

    logger.info("Warming up backend: %s", effective_backend)
    await backend.warmup()

    try:
        chunks = _chunk_audio(audio, sample_rate, chunk_seconds=chunk_seconds)
        logger.info("Transcribing %d chunks of %d seconds each...", len(chunks), chunk_seconds)
        lines: list[str] = []
        for idx, (chunk_audio, start_ms, end_ms) in enumerate(chunks):
            text = await backend.transcribe(chunk_audio)
            text = text.strip()
            if not text:
                continue
            if include_timestamps:
                mm = start_ms // 60000
                ss = (start_ms % 60000) // 1000
                lines.append(f"[{mm:02d}:{ss:02d}] {text}")
            else:
                lines.append(text)
            logger.info("  chunk %d/%d (%.0f%%): %s", idx + 1, len(chunks), 100.0 * (idx + 1) / len(chunks), text[:80])
    finally:
        await backend.stop()

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch-transcribe an audio file via Whisper.")
    parser.add_argument("audio_path", type=Path, help="Path to .wav/.mp3/.m4a/.mp4/.ogg/.flac")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output file (defaults to stdout)")
    parser.add_argument(
        "--backend",
        choices=["mlx-whisper", "whisper.cpp"],
        default=None,
        help="Override WHISPER_BACKEND from .env",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override WHISPER_MODEL from .env for this run (e.g. large-v3 for "
             "higher-quality post-call transcription; .env keeps the live default)",
    )
    parser.add_argument("--chunk-seconds", type=int, default=30, help="Chunk size in seconds (default 30)")
    parser.add_argument("--no-timestamps", action="store_true", help="Omit [MM:SS] timestamps")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    source = args.audio_path.expanduser().resolve()
    if not source.exists():
        print(f"Error: audio file not found: {source}", file=sys.stderr)
        return 1

    suffix = source.suffix.lower()
    if suffix in _SUPPORTED_DIRECT:
        wav_path = source
        cleanup = None
    elif suffix in _SUPPORTED_VIA_FFMPEG:
        tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
        try:
            logger.info("Converting %s → %s via ffmpeg...", source.name, tmp.name)
            _convert_to_wav_16k_mono(source, tmp)
        except Exception as exc:
            print(f"Error converting audio: {exc}", file=sys.stderr)
            tmp.unlink(missing_ok=True)
            return 1
        wav_path = tmp
        cleanup = tmp
    else:
        print(f"Error: unsupported audio format: {suffix}", file=sys.stderr)
        return 1

    try:
        transcript = asyncio.run(
            _transcribe(
                wav_path,
                backend_name=args.backend,
                chunk_seconds=args.chunk_seconds,
                include_timestamps=not args.no_timestamps,
                model_name=args.model,
            )
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)

    if args.output:
        args.output.write_text(transcript, encoding="utf-8")
        print(f"Transcript written to {args.output} ({len(transcript)} chars)", file=sys.stderr)
    else:
        print(transcript)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
