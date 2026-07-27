"""NL-B2B vocabulary / hotword biasing for local transcription backends.

Whisper-family models support biasing via the ``initial_prompt`` mechanism:
a short text prefix that steers the model toward domain vocabulary without
changing model weights. whisper.cpp exposes this as ``--prompt`` (CLI) and
``prompt`` (server form data); mlx-whisper exposes it as the
``initial_prompt`` kwarg. Parakeet does not expose prompt biasing, so this
feature has no effect for that backend.

The vocabulary list lives in a YAML file (default:
``config/transcription_vocabulary.yaml``) and is rendered into a single prompt
string. Keeping the list config-driven means a deployment can bias the same
base model toward its own company names, product names and vertical jargon
without fine-tuning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_VOCABULARY_PATH = Path("config/transcription_vocabulary.yaml")


@dataclass(frozen=True, slots=True)
class VocabularyConfig:
    """Loaded vocabulary configuration.

    ``initial_prompt`` is the string that should be passed to the backend.
    It is empty when the vocabulary is disabled or no terms are present.
    """

    enabled: bool
    language: str
    description: str
    prompt_prefix: str
    terms: tuple[str, ...]

    @property
    def initial_prompt(self) -> str:
        if not self.enabled or not self.terms:
            return ""
        terms_str = ", ".join(self.terms)
        if self.prompt_prefix:
            return f"{self.prompt_prefix} {terms_str}."
        return f"{terms_str}."


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Vocabulary config must be a mapping: {path}")
    return data


def load_vocabulary_config(path: str | Path) -> VocabularyConfig:
    """Load a vocabulary YAML file into a ``VocabularyConfig``.

    Missing or malformed files are logged and return an empty/disabled config
    so a bad vocabulary file cannot break transcription startup.
    """
    resolved = Path(path).expanduser()
    try:
        data = _load_yaml(resolved)
    except FileNotFoundError:
        logger.warning("Vocabulary config not found at %s; biasing disabled.", resolved)
        return VocabularyConfig(
            enabled=False,
            language="nl",
            description="",
            prompt_prefix="",
            terms=(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load vocabulary config %s (%s); biasing disabled.", resolved, exc)
        return VocabularyConfig(
            enabled=False,
            language="nl",
            description="",
            prompt_prefix="",
            terms=(),
        )

    terms = data.get("terms", [])
    if not isinstance(terms, list):
        terms = []
    cleaned = [str(t).strip() for t in terms if str(t).strip()]

    return VocabularyConfig(
        enabled=bool(data.get("enabled", True)),
        language=str(data.get("language", "nl")).strip().lower() or "nl",
        description=str(data.get("description", "")).strip(),
        prompt_prefix=str(data.get("prompt_prefix", "")).strip(),
        terms=tuple(cleaned),
    )


def default_vocabulary_config() -> VocabularyConfig:
    """Load the default vocabulary config from ``config/transcription_vocabulary.yaml``."""
    return load_vocabulary_config(DEFAULT_VOCABULARY_PATH)
