#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
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

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune Whisper with a LoRA adapter on Dutch sales call data.")
    parser.add_argument("--dataset", type=Path, required=True, help="JSONL dataset created by prepare_dataset.py")
    parser.add_argument("--epochs", type=int, default=3, help="Number of epochs")
    parser.add_argument("--lr", type=float, default=1e-4, help="Learning rate")
    parser.add_argument(
        "--base-model",
        type=str,
        default=DEFAULT_MODEL,
        help="Base mlx-whisper model repository",
    )
    parser.add_argument(
        "--adapter-out",
        type=Path,
        default=Path("data/finetune/adapter"),
        help="Directory where the LoRA adapter is saved",
    )
    parser.add_argument("--eval-ratio", type=float, default=0.2, help="Validation split ratio")
    parser.add_argument("--seed", type=int, default=42, help="Split seed")
    parser.add_argument(
        "--allow-stub-adapter",
        action="store_true",
        help=(
            "Create a stub adapter manifest when direct MLX LoRA training helpers are unavailable. "
            "Use this for pipeline dry-runs."
        ),
    )
    return parser.parse_args()


def evaluate_dataset(
    *,
    eval_references: list[str],
    eval_audio_paths: list[Path],
    base_model: str,
    adapter_path: str | None,
) -> float:
    hypotheses: list[str] = []
    for audio_path in eval_audio_paths:
        hypotheses.append(
            transcribe_with_optional_adapter(
                audio_path=audio_path,
                model_repo=base_model,
                language="nl",
                adapter_path=adapter_path,
            )
        )
    return compute_wer(eval_references, hypotheses)


def write_stub_adapter(adapter_dir: Path, *, args: argparse.Namespace, train_size: int, eval_size: int) -> None:
    adapter_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "type": "stub_lora_adapter",
        "message": (
            "No direct MLX LoRA trainer detected. Install a compatible mlx-whisper LoRA trainer or "
            "replace this stub with a trained adapter."
        ),
        "base_model": args.base_model,
        "epochs": args.epochs,
        "learning_rate": args.lr,
        "train_size": train_size,
        "eval_size": eval_size,
    }
    (adapter_dir / "adapter_config.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    entries = load_dataset_jsonl(args.dataset)
    if len(entries) < 2:
        raise ValueError("Dataset must contain at least 2 entries for train/eval split")

    train_entries, eval_entries = split_train_eval(entries, eval_ratio=args.eval_ratio, seed=args.seed)
    if not train_entries or not eval_entries:
        raise ValueError("Unable to create non-empty train/eval split")

    eval_audio_paths = [Path(entry.audio_path) for entry in eval_entries]
    eval_references = [entry.text for entry in eval_entries]

    baseline_wer = evaluate_dataset(
        eval_references=eval_references,
        eval_audio_paths=eval_audio_paths,
        base_model=args.base_model,
        adapter_path=None,
    )
    logger.info("Baseline WER: %.4f", baseline_wer)

    if args.allow_stub_adapter:
        write_stub_adapter(args.adapter_out, args=args, train_size=len(train_entries), eval_size=len(eval_entries))
        logger.warning("Created stub adapter at %s (dry-run mode).", args.adapter_out)
    else:
        raise RuntimeError(
            "LoRA training backend not wired for this environment. Re-run with --allow-stub-adapter for "
            "pipeline validation, or integrate your MLX LoRA trainer in this script."
        )

    epoch_metrics: list[dict[str, float | int]] = []
    for epoch in range(1, args.epochs + 1):
        current_wer = evaluate_dataset(
            eval_references=eval_references,
            eval_audio_paths=eval_audio_paths,
            base_model=args.base_model,
            adapter_path=str(args.adapter_out),
        )
        logger.info("Epoch %d/%d - WER %.4f", epoch, args.epochs, current_wer)
        epoch_metrics.append({"epoch": epoch, "wer": current_wer})

    args.adapter_out.mkdir(parents=True, exist_ok=True)
    metrics_path = args.adapter_out / "training_metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "baseline_wer": baseline_wer,
                "epochs": epoch_metrics,
                "base_model": args.base_model,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Saved adapter artifacts to %s", args.adapter_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
