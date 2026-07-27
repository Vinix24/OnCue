#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from scripts.finetune import (
        compute_wer,
        load_dataset_jsonl,
        split_train_eval,
        transcribe_with_optional_adapter,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    ROOT_DIR = Path(__file__).resolve().parents[2]
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from scripts.finetune import (
        compute_wer,
        load_dataset_jsonl,
        split_train_eval,
        transcribe_with_optional_adapter,
    )

DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate baseline vs fine-tuned Whisper adapter WER.")
    parser.add_argument("--dataset", type=Path, required=True, help="JSONL dataset")
    parser.add_argument(
        "--adapter",
        type=Path,
        default=Path("data/finetune/adapter"),
        help="Adapter directory created by train_whisper.py",
    )
    parser.add_argument(
        "--base-model",
        type=str,
        default=DEFAULT_MODEL,
        help="Base mlx-whisper model repository",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/finetune/eval_report.json"),
        help="Evaluation report path",
    )
    parser.add_argument("--eval-ratio", type=float, default=0.2, help="Held-out split ratio")
    parser.add_argument("--seed", type=int, default=42, help="Split seed")
    return parser.parse_args()


def evaluate_wer(entries: list, base_model: str, adapter_path: str | None) -> float:
    references: list[str] = []
    hypotheses: list[str] = []
    for entry in entries:
        references.append(entry.text)
        hypotheses.append(
            transcribe_with_optional_adapter(
                audio_path=Path(entry.audio_path),
                model_repo=base_model,
                language=entry.language,
                adapter_path=adapter_path,
            )
        )
    return compute_wer(references, hypotheses)


def main() -> int:
    args = parse_args()
    dataset_entries = load_dataset_jsonl(args.dataset)
    if len(dataset_entries) < 2:
        raise ValueError("Dataset must contain at least 2 entries")

    _, eval_entries = split_train_eval(dataset_entries, eval_ratio=args.eval_ratio, seed=args.seed)
    if not eval_entries:
        raise ValueError("Held-out split is empty")

    baseline_wer = evaluate_wer(eval_entries, args.base_model, None)

    adapter_path: str | None = None
    if args.adapter.exists():
        adapter_path = str(args.adapter)
    finetuned_wer = evaluate_wer(eval_entries, args.base_model, adapter_path)

    report = {
        "baseline_wer": baseline_wer,
        "finetuned_wer": finetuned_wer,
        "delta": baseline_wer - finetuned_wer,
        "eval_samples": len(eval_entries),
        "base_model": args.base_model,
        "adapter_path": adapter_path,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Saved evaluation report to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
