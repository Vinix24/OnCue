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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sales_copilot.core.paths import resolve_config_file

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


PROMPT_TOKEN_BUDGET = 223
"""Whisper's prompt budget: n_text_ctx // 2 - 1 (448 // 2 - 1)."""


class TokenizerUnavailableError(RuntimeError):
    """The Whisper tokenizer cannot be loaded, so tokens cannot be counted."""


@dataclass(frozen=True, slots=True)
class BudgetReport:
    """Token count of a rendered prompt against Whisper's prompt budget.

    ``over_terms`` are the trailing terms to cut so the prompt fits. ``lost_terms``
    and ``framing_lost`` are what whisper.cpp drops at runtime when nothing is
    cut: it keeps the last tokens, so the head of the prompt disappears.
    """

    tokens: int
    budget: int
    over_terms: tuple[str, ...]
    lost_terms: tuple[str, ...] = ()
    framing_lost: bool = False

    @property
    def margin(self) -> int:
        return self.budget - self.tokens

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget

    def describe(self) -> str:
        if not self.over_budget:
            return f"{self.tokens} of {self.budget} tokens, {self.margin} to spare"
        cut = ", ".join(self.over_terms) if self.over_terms else "(none)"
        lost = list(self.lost_terms)
        if self.framing_lost:
            lost.insert(0, "(framing sentence)")
        lost_str = ", ".join(lost) if lost else "(none)"
        return (
            f"{self.tokens} of {self.budget} tokens, {-self.margin} over budget. "
            f"To fit, cut the trailing terms: {cut}. "
            f"If nothing is cut, whisper.cpp keeps the last tokens and drops the head of the prompt; "
            f"lost: {lost_str}"
        )


def _encoding() -> Any:
    """Load the Whisper tokenizer; any failure means "cannot count", never a crash."""
    try:
        from mlx_whisper.tokenizer import get_encoding

        return get_encoding("multilingual")
    except ImportError as exc:
        raise TokenizerUnavailableError(
            "The Whisper tokenizer (mlx-whisper, with tiktoken) is not installed; cannot count prompt tokens."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a broken install must not stop startup
        raise TokenizerUnavailableError(
            f"The Whisper tokenizer could not be loaded ({exc!r}); cannot count prompt tokens."
        ) from exc


def check_budget(config: VocabularyConfig, budget: int = PROMPT_TOKEN_BUDGET) -> BudgetReport:
    """Measure ``config``'s rendered prompt against ``budget``.

    A leading space is counted the way Whisper adds it before the prompt text.
    Raises ``TokenizerUnavailableError`` when the tokenizer cannot load or count.
    """
    enc = _encoding()
    prompt = config.initial_prompt
    try:
        tokens = len(enc.encode(" " + prompt)) if prompt else 0
        over: list[str] = []
        lost: list[str] = []
        framing_lost = False
        if tokens > budget:
            prefix = f"{config.prompt_prefix} " if config.prompt_prefix else ""
            dropped = tokens - budget  # tokens whisper.cpp cuts from the head
            previous = len(enc.encode(" " + prefix.strip())) if prefix else 0
            framing_lost = bool(prefix)
            fits_found = False
            for i, term in enumerate(config.terms):
                running = len(enc.encode(" " + prefix + ", ".join(config.terms[: i + 1]) + "."))
                if previous < dropped:
                    lost.append(term)
                if running > budget and not fits_found:
                    over = list(config.terms[i:])
                    fits_found = True
                previous = running
                if fits_found and previous >= dropped:
                    break
    except Exception as exc:  # noqa: BLE001 - counting failure means "not checked"
        raise TokenizerUnavailableError(f"Prompt tokens could not be counted ({exc!r}).") from exc
    return BudgetReport(
        tokens=tokens, budget=budget, over_terms=tuple(over), lost_terms=tuple(lost), framing_lost=framing_lost
    )


_logged: set[tuple[str, str]] = set()
_logged_lock = threading.Lock()


def warn_if_over_budget(config: VocabularyConfig, source: str = "") -> BudgetReport | None:
    """Log the budget check once per vocabulary and source; return the report.

    Over budget is a WARNING. An unavailable tokenizer is a single INFO line
    (the packaged whisper.cpp app has no mlx-whisper). Never raises. Returns
    ``None`` when there is no prompt or the budget could not be checked.
    """
    if not config.enabled or not config.terms:
        return None
    key = (source, config.initial_prompt)
    with _logged_lock:
        first = key not in _logged
        _logged.add(key)
    try:
        report = check_budget(config)
    except Exception as exc:  # noqa: BLE001 - startup must never crash on the budget check
        if first:
            logger.info("Vocabulary budget not checked: %s", exc)
        return None
    if report.over_budget and first:
        logger.warning("Vocabulary %s is over the Whisper prompt budget: %s", source, report.describe())
    return report


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
    resolved = resolve_config_file(path)
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
