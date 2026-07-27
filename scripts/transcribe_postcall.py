#!/usr/bin/env python3
"""Post-call batch transcription of a recorded session, via the Parakeet backend.

Takes a recorder session dir (one WAV per speaker) or a single audio file and
writes one time-ordered, speaker-labeled transcript. Parakeet decodes in chunks
(``--chunk-duration``) so a multi-hour call never OOMs the Metal allocator.
Feeds the gespreksanalyse leerloop.

Usage:
    .venv/bin/python scripts/transcribe_postcall.py data/sessions/20260701-1200
    .venv/bin/python scripts/transcribe_postcall.py opname.wav --out transcript.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.modules.transcriber.backends.parakeet_backend import ParakeetMlxBackend  # noqa: E402
from sales_copilot.modules.transcriber.postcall import (  # noqa: E402
    DEFAULT_LABELS,
    format_transcript,
    merge_labeled_segments,
    transcribe_session,
)


async def _run(args) -> str:
    backend = ParakeetMlxBackend(
        model_repo=args.model,
        language=args.language,
        timeout_s=args.timeout,
        chunk_duration_s=args.chunk_duration,
    )
    target = Path(args.target)
    if target.is_dir():
        return await transcribe_session(target, backend, label_map=DEFAULT_LABELS)
    segments = await backend.transcribe_file_segments(target)
    merged = merge_labeled_segments({target.stem: segments})
    return format_transcript(merged, DEFAULT_LABELS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Recorder session dir (with *.wav) or a single audio file")
    parser.add_argument("--out", help="Output transcript path (default: <session>/transcript_postcall.txt)")
    parser.add_argument("--model", default="mlx-community/parakeet-tdt-0.6b-v3")
    parser.add_argument("--language", default="nl")
    parser.add_argument("--chunk-duration", type=float, default=120.0,
                        help="Decode window in seconds (guards Metal-OOM on long calls).")
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"FATAL: not found: {target}")
        return 1

    transcript = asyncio.run(_run(args))
    if not transcript.strip():
        print("Geen transcript geproduceerd (leeg resultaat).")
        return 1

    if args.out:
        out_path = Path(args.out)
    elif target.is_dir():
        out_path = target / "transcript_postcall.txt"
    else:
        out_path = target.with_suffix(".postcall.txt")
    out_path.write_text(transcript, encoding="utf-8")
    print(f"Transcript geschreven: {out_path} ({len(transcript.splitlines())} regels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
