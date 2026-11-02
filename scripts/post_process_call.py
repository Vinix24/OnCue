#!/usr/bin/env python3
"""Post-process a recorded call: diarize + merge + write speaker-labeled transcript.

Chains three stages:
  1. Transcription  — if no --transcript-json is given, run the configured
                      backend's transcribe_file_segments (whisper.cpp by
                      default) to produce {start, end, text} segments.
  2. Diarization    — separate speakers on the WAV (whole file, offline) with
                      sherpa-onnx (no token) or pyannote (Hugging Face token)
                      and extract (start, end, speaker) turns.
  3. Merge          — assign each transcript segment to the speaker with the
                      greatest temporal overlap and collapse same-speaker runs.

No GCP, no network beyond the model download on first use (GitHub releases
for sherpa-onnx, Hugging Face for pyannote).

Usage:
  python scripts/post_process_call.py recording.wav
  python scripts/post_process_call.py recording.wav --output labeled.txt
  python scripts/post_process_call.py recording.wav \\
      --transcript-json whisper_output.json \\
      --model pyannote/speaker-diarization-community-1 \\
      --num-speakers 2 \\
      --output call_labeled.txt

Arguments:
  wav                     Path to the WAV recording (16 kHz mono recommended).
  --transcript-json       Pre-computed Whisper JSON (skips transcription step).
                          Expected shape: {"segments": [{start, end, text}, ...]}
  --output / -o           Output path for the labeled transcript (default:
                          <wav stem>-labeled.txt next to the WAV).
  --backend               auto | sherpa | pyannote (default: auto).
                          auto = sherpa when --num-speakers is 2 or less;
                          otherwise (3 or more, or not given) pyannote when a
                          Hugging Face token is available, else sherpa with a
                          warning.
  --cluster-threshold     Sherpa clustering threshold (default: 0.8). Sherpa
                          ignores --num-speakers and clusters by this threshold.
  --model                 Pyannote model ID (pyannote backend only).
                          Default: pyannote/speaker-diarization-3.1
                          Benchmark option: pyannote/speaker-diarization-community-1
  --num-speakers          Fix the number of speakers (pyannote; default: auto-detect).
                          With auto it also selects the backend.
  --device                Torch device: mps | cuda | cpu (default: auto).
  --hf-token              HuggingFace token. Falls back to HUGGINGFACE_TOKEN
                          env var or cached huggingface-cli login.
  --verbose / -v          Enable debug logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transcription helpers (lazy — only imported when no transcript-json given)
# ---------------------------------------------------------------------------


def _transcribe_to_segments(wav_path: Path) -> list[dict]:
    """Run offline transcription via the repo's backend and return segments.

    Returns a list of {start, end, text} dicts with timestamps in seconds, from
    the backend's own ``transcribe_file_segments`` (the post-call path shared
    with ``scripts/transcribe_postcall.py``). A backend that cannot give
    timestamped segments raises ``RuntimeError``: a single segment for the whole
    file would hand every word to one speaker and pass as a success.
    Heavy imports (mlx_whisper / whisper.cpp) are local to this function.
    """
    import asyncio

    from sales_copilot.core.config import TranscriberConfig, load_env
    from sales_copilot.modules.transcriber.backends import create_backend

    load_env()
    cfg = TranscriberConfig.from_env()
    backend = create_backend({
        "backend": cfg.backend,
        "language": cfg.language,
        "model_repo": f"mlx-community/whisper-{cfg.model}",
        "whisper_cpp_binary": cfg.whisper_cpp_binary,
        "whisper_cpp_model_path": cfg.whisper_cpp_model_path,
        "whisper_cpp_threads": cfg.whisper_cpp_threads,
    })

    transcribe = getattr(backend, "transcribe_file_segments", None)
    if transcribe is None:
        raise RuntimeError(
            f"Transcription backend {type(backend).__name__} cannot produce "
            "timestamped segments, so speakers cannot be assigned. Use the "
            "whisper.cpp or parakeet backend, or pass --transcript-json."
        )

    async def _run() -> list[dict]:
        try:
            segments = await transcribe(wav_path)
        finally:
            await backend.stop()
        if not segments:
            raise RuntimeError(f"No transcript segments were produced for {wav_path}.")
        return segments

    return asyncio.run(_run())


def _load_segments_from_json(json_path: Path) -> list[dict]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        raise ValueError(f"No 'segments' key found in {json_path}")
    normalized: list[dict] = []
    for seg in segments:
        normalized.append({
            "start": float(seg["start"]),
            "end": float(seg["end"]),
            "text": str(seg.get("text", "")),
        })
    return normalized


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from sales_copilot.modules.reports.diarize_merge import (  # noqa: PLC0415
        BACKEND_CHOICES,
        diarize_audio,
        merge_diarization,
        render_labeled,
    )
    from sales_copilot.modules.reports.diarize_sherpa import DEFAULT_CLUSTER_THRESHOLD  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        description="Post-process a recorded call: diarize + merge -> speaker-labeled transcript.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("wav", type=Path, help="Path to WAV recording.")
    parser.add_argument(
        "--transcript-json",
        type=Path,
        default=None,
        metavar="JSON",
        help="Pre-computed Whisper JSON (skips transcription).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output path (default: <wav stem>-labeled.txt).",
    )
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default="auto",
        help=(
            "Speaker separation backend (default: auto). auto uses sherpa-onnx when --num-speakers "
            "is 2 or less; otherwise (3 or more, or not given) pyannote when a Hugging Face token "
            "is available, else sherpa-onnx with a warning."
        ),
    )
    parser.add_argument(
        "--cluster-threshold",
        type=float,
        default=DEFAULT_CLUSTER_THRESHOLD,
        metavar="T",
        help="Sherpa clustering threshold (default: 0.8; sherpa only).",
    )
    parser.add_argument(
        "--model",
        default="pyannote/speaker-diarization-3.1",
        metavar="MODEL_ID",
        help=(
            "Pyannote model ID (pyannote backend only). "
            "Default: pyannote/speaker-diarization-3.1. "
            "Community benchmark: pyannote/speaker-diarization-community-1."
        ),
    )
    parser.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        metavar="N",
        help="Fix speaker count (default: auto-detect).",
    )
    parser.add_argument(
        "--device",
        default=None,
        metavar="DEVICE",
        help="Torch device: mps | cuda | cpu (default: auto).",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        metavar="TOKEN",
        help="HuggingFace token. Falls back to HUGGINGFACE_TOKEN env var.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    wav_path: Path = args.wav.expanduser().resolve()
    if not wav_path.exists():
        print(f"Error: WAV file not found: {wav_path}", file=sys.stderr)
        return 1

    output_path: Path = args.output or wav_path.with_name(wav_path.stem + "-labeled.txt")

    # --- Stage 1: Transcript segments ---
    if args.transcript_json is not None:
        json_path: Path = args.transcript_json.expanduser().resolve()
        if not json_path.exists():
            print(f"Error: transcript JSON not found: {json_path}", file=sys.stderr)
            return 1
        logger.info("Loading transcript from %s", json_path)
        segments = _load_segments_from_json(json_path)
    else:
        logger.info("Transcribing %s (this may take a while)...", wav_path.name)
        try:
            segments = _transcribe_to_segments(wav_path)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    logger.info("Transcript: %d segment(s)", len(segments))

    # --- Stage 2: Diarization ---
    logger.info("Diarizing (backend=%s, this may take a while)...", args.backend)
    turns, backend_used = diarize_audio(
        wav_path,
        backend=args.backend,
        hf_token=args.hf_token,
        model_id=args.model,
        num_speakers=args.num_speakers,
        device=args.device,
        cluster_threshold=args.cluster_threshold,
    )
    logger.info("Diarization (%s): %d turn(s)", backend_used, len(turns))

    # --- Stage 3: Merge ---
    turn_tuples = [(t.start, t.end, t.speaker) for t in turns]
    blocks = merge_diarization(segments, turn_tuples)
    logger.info("Merged into %d speaker block(s)", len(blocks))

    # --- Write output ---
    labeled_text = render_labeled(blocks)
    output_path.write_text(labeled_text + "\n", encoding="utf-8")

    speakers = sorted({b.speaker for b in blocks})
    print(
        f"[post-process] backend: {backend_used}, "
        f"{len(turns)} diarization turns, "
        f"{len(segments)} transcript segments, "
        f"speakers: {', '.join(speakers)}",
        file=sys.stderr,
    )
    print(f"[post-process] -> {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
