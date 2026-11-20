#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from sales_copilot.modules.transcriber.backends.model_names import MODEL_MAP


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a Whisper model snapshot into the Hugging Face cache.",
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_MAP.keys()),
        default="large-v3-turbo",
        help="Model alias to download.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show intended action without downloading.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Optional model revision/tag/sha.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec = MODEL_MAP[args.model]

    print(f"Selected model: {spec.alias} -> {spec.mlx_repo_id}")
    if args.revision:
        print(f"Revision: {args.revision}")

    if args.dry_run:
        print("Dry-run enabled; no download performed.")
        return 0

    try:
        from huggingface_hub import snapshot_download
    except Exception as exc:
        print("ERROR: huggingface_hub is required for model download.", file=sys.stderr)
        print(f"Detail: {exc}", file=sys.stderr)
        return 1

    try:
        snapshot_path = snapshot_download(
            repo_id=spec.mlx_repo_id,
            revision=args.revision,
            local_files_only=False,
            resume_download=True,
        )
    except Exception as exc:
        print(f"ERROR: failed to download model {spec.mlx_repo_id}: {exc}", file=sys.stderr)
        return 1

    print(f"Model ready in cache: {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
