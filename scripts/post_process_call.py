#!/usr/bin/env python3
"""Post-process a recorded call: diarize + merge + write speaker-labeled transcript.

Chains three stages:
  1. Transcription  — if no --transcript-json is given, run offline Whisper
                      via the existing transcribe_file backend to produce
                      {start, end, text} segments.
  2. Diarization    — run pyannote on the WAV (whole file, offline) and
                      extract (start, end, speaker) turns.
  3. Merge          — assign each transcript segment to the speaker with the
                      greatest temporal overlap and collapse same-speaker runs.

No GCP, no network beyond the HuggingFace model download on first use.

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
  --model                 Pyannote model ID.
                          Default: pyannote/speaker-diarization-3.1
                          Benchmark option: pyannote/speaker-diarization-community-1
  --num-speakers          Fix the number of speakers (default: auto-detect).
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
    """Run offline Whisper via the repo's existing backend and return segments.

    Returns a list of {start, end, text} dicts with timestamps in seconds.
    Heavy imports (mlx_whisper / whisper.cpp) are local to this function.
    """
    import asyncio

    # Reuse the internal transcription helpers from transcribe_file.py.
    # We import from that module rather than duplicating logic.
    sys.path.insert(0, str(Path(__file__).parent))
    from transcribe_file import _load_wav_as_float32  # noqa: PLC0415

    from sales_copilot.core.config import TranscriberConfig, load_env  # noqa: PLC0415
    from sales_copilot.modules.transcriber.backends import create_backend  # noqa: PLC0415

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

    audio, sample_rate = _load_wav_as_float32(wav_path)

    async def _run() -> list[dict]:
        await backend.warmup()
        try:
            # Request structured output including timestamps.  Not all backends
            # surface per-segment timestamps; we fall back to a single-segment
            # representation so the merge still works.
            result = await backend.transcribe_with_segments(audio)
            if result:
                return result
            # Fallback: treat the whole file as one segment.
            text = await backend.transcribe(audio)
            duration = len(audio) / float(sample_rate)
            return [{"start": 0.0, "end": duration, "text": text.strip()}]
        finally:
            await backend.stop()

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
        "--model",
        default="pyannote/speaker-diarization-3.1",
        metavar="MODEL_ID",
        help=(
            "Pyannote model ID. "
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
        segments = _transcribe_to_segments(wav_path)

    logger.info("Transcript: %d segment(s)", len(segments))

    # --- Stage 2: Diarization ---
    logger.info("Diarizing with model=%s (this may take a while)...", args.model)
    from sales_copilot.modules.reports.diarize_merge import (  # noqa: PLC0415
        diarize_wav,
        merge_diarization,
        render_labeled,
    )

    turns = diarize_wav(
        wav_path,
        hf_token=args.hf_token,
        model_id=args.model,
        num_speakers=args.num_speakers,
        device=args.device,
    )
    logger.info("Diarization: %d turn(s)", len(turns))

    # --- Stage 3: Merge ---
    turn_tuples = [(t.start, t.end, t.speaker) for t in turns]
    blocks = merge_diarization(segments, turn_tuples)
    logger.info("Merged into %d speaker block(s)", len(blocks))

    # --- Write output ---
    labeled_text = render_labeled(blocks)
    output_path.write_text(labeled_text + "\n", encoding="utf-8")

    speakers = sorted({b.speaker for b in blocks})
    print(
        f"[post-process] {len(turns)} diarization turns, "
        f"{len(segments)} transcript segments, "
        f"speakers: {', '.join(speakers)}",
        file=sys.stderr,
    )
    print(f"[post-process] -> {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
