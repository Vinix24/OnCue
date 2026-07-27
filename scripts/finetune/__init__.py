from __future__ import annotations

import inspect
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetEntry:
    audio_path: str
    text: str
    language: str = "nl"


def load_dataset_jsonl(dataset_path: Path) -> list[DatasetEntry]:
    entries: list[DatasetEntry] = []
    with dataset_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"Dataset line {line_number} must be a JSON object")
            audio_path = payload.get("audio_path")
            text = payload.get("text")
            language = payload.get("language", "nl")
            if not isinstance(audio_path, str) or not audio_path:
                raise ValueError(f"Dataset line {line_number} has invalid audio_path")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Dataset line {line_number} has empty text")
            if not isinstance(language, str) or not language:
                raise ValueError(f"Dataset line {line_number} has invalid language")
            entries.append(DatasetEntry(audio_path=audio_path, text=text.strip(), language=language))
    return entries


def split_train_eval(
    entries: list[DatasetEntry],
    *,
    eval_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[list[DatasetEntry], list[DatasetEntry]]:
    if not entries:
        return [], []

    eval_size = max(1, int(round(len(entries) * eval_ratio)))
    if eval_size >= len(entries):
        eval_size = max(1, len(entries) - 1)

    indexed = list(enumerate(entries))
    random.Random(seed).shuffle(indexed)
    eval_indices = {index for index, _ in indexed[:eval_size]}

    train_entries = [entry for index, entry in enumerate(entries) if index not in eval_indices]
    eval_entries = [entry for index, entry in enumerate(entries) if index in eval_indices]
    return train_entries, eval_entries


def compute_wer(references: list[str], hypotheses: list[str]) -> float:
    if len(references) != len(hypotheses):
        raise ValueError("References and hypotheses must have the same length")

    total_words = 0
    total_edits = 0
    for reference, hypothesis in zip(references, hypotheses, strict=False):
        ref_tokens = _tokenize(reference)
        hyp_tokens = _tokenize(hypothesis)
        if not ref_tokens:
            continue
        total_words += len(ref_tokens)
        total_edits += _levenshtein_distance(ref_tokens, hyp_tokens)

    if total_words == 0:
        return 0.0
    return total_edits / total_words


def transcribe_with_optional_adapter(
    *,
    audio_path: Path,
    model_repo: str,
    language: str,
    adapter_path: str | None,
) -> str:
    import mlx_whisper

    kwargs: dict[str, Any] = {
        "path_or_hf_repo": model_repo,
        "language": language,
        "condition_on_previous_text": False,
        "compression_ratio_threshold": 2.4,
        "no_speech_threshold": 0.6,
    }
    if adapter_path:
        adapter_kwarg = find_adapter_kwarg(mlx_whisper.transcribe)
        if adapter_kwarg:
            kwargs[adapter_kwarg] = adapter_path
        else:
            logger.warning(
                "Adapter path provided (%s), but mlx_whisper.transcribe has no supported adapter keyword.",
                adapter_path,
            )

    result = mlx_whisper.transcribe(str(audio_path), **kwargs)
    if isinstance(result, dict):
        text = result.get("text", "")
        if isinstance(text, str):
            return text.strip()
    return ""


def find_adapter_kwarg(transcribe_fn: Any) -> str | None:
    try:
        signature = inspect.signature(transcribe_fn)
    except (TypeError, ValueError):
        return None

    for keyword in ("adapter_path", "lora_path", "adapter", "adapters_path"):
        if keyword in signature.parameters:
            return keyword

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return "adapter_path"
    return None


def extract_transcript_text(payload: Any) -> str:
    segments = _segments_from_payload(payload)
    texts: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            cleaned = _normalize_whitespace(segment)
            if cleaned:
                texts.append(cleaned)
            continue
        if not isinstance(segment, dict):
            continue
        for key in ("text", "utterance", "content"):
            value = segment.get(key)
            if isinstance(value, str):
                cleaned = _normalize_whitespace(value)
                if cleaned:
                    texts.append(cleaned)
                break
    return " ".join(texts).strip()


def _segments_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    for key in ("transcript", "segments", "utterances", "items", "events"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    if isinstance(payload.get("text"), str):
        return [payload]
    return []


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _tokenize(text: str) -> list[str]:
    return _normalize_whitespace(text.lower()).split(" ") if text.strip() else []


def _levenshtein_distance(reference: list[str], hypothesis: list[str]) -> int:
    rows = len(reference) + 1
    cols = len(hypothesis) + 1
    matrix = [[0] * cols for _ in range(rows)]

    for i in range(rows):
        matrix[i][0] = i
    for j in range(cols):
        matrix[0][j] = j

    for i in range(1, rows):
        for j in range(1, cols):
            substitution_cost = 0 if reference[i - 1] == hypothesis[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + substitution_cost,
            )
    return matrix[-1][-1]
