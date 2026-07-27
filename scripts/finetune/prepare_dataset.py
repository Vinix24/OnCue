#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from scripts.finetune import extract_transcript_text
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    ROOT_DIR = Path(__file__).resolve().parents[2]
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from scripts.finetune import extract_transcript_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a fine-tuning JSONL dataset from recorded sales call sessions.",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        required=True,
        help="Directory containing session folders with audio.wav and transcript JSON files.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output JSONL dataset path.",
    )
    return parser.parse_args()


def resolve_transcript_file(session_dir: Path) -> Path | None:
    corrected_path = session_dir / "transcript_corrected.json"
    if corrected_path.exists():
        return corrected_path
    default_path = session_dir / "transcript.json"
    if default_path.exists():
        return default_path
    return None


def collect_entries(sessions_dir: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    if not sessions_dir.exists():
        raise FileNotFoundError(f"Sessions directory not found: {sessions_dir}")

    session_paths = sorted(path for path in sessions_dir.iterdir() if path.is_dir())
    for session_dir in session_paths:
        audio_path = session_dir / "audio.wav"
        transcript_path = resolve_transcript_file(session_dir)
        if transcript_path is None or not audio_path.exists():
            continue

        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript_text = extract_transcript_text(payload)
        if not transcript_text:
            continue

        entries.append(
            {
                "audio_path": str(audio_path.resolve()),
                "text": transcript_text,
                "language": "nl",
            }
        )
    return entries


def write_jsonl(entries: list[dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    entries = collect_entries(args.sessions_dir)
    write_jsonl(entries, args.out)
    print(f"Prepared {len(entries)} dataset entries at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
