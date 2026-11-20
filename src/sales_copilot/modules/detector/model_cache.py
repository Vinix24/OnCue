from __future__ import annotations

from pathlib import Path

CONFIG_FILE = "config.json"
WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")
FAST_TOKENIZER_FILE = "tokenizer.json"
SLOW_TOKENIZER_CONFIG_FILE = "tokenizer_config.json"
SLOW_TOKENIZER_VOCAB_FILES = ("vocab.txt", "vocab.json", "sentencepiece.bpe.model", "spiece.model")


def normalize_model_name(embedding_model: str) -> str:
    if "/" in embedding_model:
        return embedding_model
    return f"sentence-transformers/{embedding_model}"


def _snapshot_complete(snapshot: Path) -> bool:
    if not (snapshot / CONFIG_FILE).exists():
        return False
    if not any((snapshot / name).exists() for name in WEIGHT_FILES):
        return False
    if (snapshot / FAST_TOKENIZER_FILE).exists():
        return True
    return (snapshot / SLOW_TOKENIZER_CONFIG_FILE).exists() and any(
        (snapshot / name).exists() for name in SLOW_TOKENIZER_VOCAB_FILES
    )


def embedding_model_cached(embedding_model: str) -> bool:
    """True when the local HF cache holds config, weights and tokenizer of the model.

    ``huggingface_hub`` is an optional dependency (the Windows smoke job installs without
    it). Without it there is no cache to inspect, so the answer is False."""
    try:
        from huggingface_hub import constants as hf_constants
    except ImportError:
        return False

    name = normalize_model_name(embedding_model)
    snapshots = Path(hf_constants.HF_HUB_CACHE) / f"models--{name.replace('/', '--')}" / "snapshots"
    return snapshots.is_dir() and any(_snapshot_complete(snap) for snap in snapshots.iterdir())
