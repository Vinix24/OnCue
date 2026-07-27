from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sales_copilot.core.config import TranscriberConfig, env, with_overrides
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend
from sales_copilot.modules.transcriber.backends.groq_backend import GroqBackend
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from sales_copilot.modules.transcriber.backends.parakeet_backend import ParakeetMlxBackend
from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config

try:  # pragma: no cover - optional backend path
    from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend
except Exception:  # pragma: no cover
    WhisperCppBackend = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _build_transcriber_config(cfg: dict[str, Any]) -> TranscriberConfig:
    """Merge optional backend overrides from a dict into the env-based config."""
    base = TranscriberConfig.from_env()
    return with_overrides(
        base,
        backend=cfg.get("backend"),
        language=cfg.get("language"),
        whisper_cpp_binary=cfg.get("whisper_cpp_binary"),
        whisper_cpp_model_path=cfg.get("whisper_cpp_model_path"),
        whisper_cpp_threads=cfg.get("whisper_cpp_threads"),
        whisper_cpp_server_binary=cfg.get("whisper_cpp_server_binary"),
        vocabulary_config=cfg.get("vocabulary_config"),
        vocabulary_enabled=cfg.get("vocabulary_enabled"),
        vocabulary_initial_prompt=cfg.get("vocabulary_initial_prompt"),
    )


def _effective_initial_prompt(config: TranscriberConfig) -> str | None:
    """Return the prompt string to use for mlx-whisper, or None when disabled."""
    if not config.vocabulary_enabled:
        return None
    if config.vocabulary_initial_prompt is not None:
        return config.vocabulary_initial_prompt
    vocab = load_vocabulary_config(config.vocabulary_config)
    return vocab.initial_prompt or None


def create_backend(config: dict[str, Any] | None = None) -> TranscriptionBackend:
    """Build the configured transcription backend.

    whisper.cpp is the documented stable default. When it is selected but the
    compiled binary (or its GGML model) is not on disk — e.g. a fresh install
    where ``scripts/install.sh`` could not build it — the transcriber degrades
    to mlx-whisper with a loud, actionable warning instead of raising
    ``FileNotFoundError`` and crashing the engine mid-call. A ``RuntimeError`` is
    raised only when neither backend can be constructed.
    """
    cfg = config or {}
    backend_name = str(cfg.get("backend", "whisper.cpp")).strip().lower()
    language = str(cfg.get("language", "nl"))
    model_repo = str(cfg.get("model_repo", "mlx-community/whisper-large-v3-turbo"))

    adapter_path_value = cfg.get("adapter_path")
    if adapter_path_value is None:
        adapter_path_value = env("WHISPER_FINE_TUNED_MODEL_PATH")
    adapter_path = str(adapter_path_value).strip() if adapter_path_value is not None else None
    if adapter_path == "":
        adapter_path = None

    # Vocabulary biasing is shared across backends. Compute the effective prompt
    # once so mlx-whisper and whisper.cpp receive the same text.
    transcriber_cfg = _build_transcriber_config(cfg)
    initial_prompt = _effective_initial_prompt(transcriber_cfg)

    def _build_mlx() -> TranscriptionBackend:
        return MlxWhisperBackend(
            model_repo=model_repo,
            language=language,
            adapter_path=adapter_path,
            initial_prompt=initial_prompt,
        )

    def _fallback_to_mlx() -> TranscriptionBackend:
        try:
            return _build_mlx()
        except Exception as exc:  # pragma: no cover - both backends unavailable
            raise RuntimeError(
                "No transcription backend available: whisper.cpp is not usable and "
                "mlx-whisper could not be constructed."
            ) from exc

    if backend_name in {"mlx-whisper", "mlx"}:
        return _build_mlx()

    if backend_name in {"parakeet", "parakeet-mlx"}:
        import importlib.util

        if importlib.util.find_spec("parakeet_mlx") is None:
            logger.warning(
                "parakeet backend selected but parakeet-mlx is not installed; install it "
                "with `pip install '.[parakeet]'`. Falling back to mlx-whisper.",
            )
            return _fallback_to_mlx()
        parakeet_repo = (
            model_repo
            if "parakeet" in model_repo.lower()
            else "mlx-community/parakeet-tdt-0.6b-v3"
        )
        logger.info(
            "Vocabulary biasing is not supported by the parakeet backend; "
            "terms in %s will be ignored.",
            transcriber_cfg.vocabulary_config,
        )
        return ParakeetMlxBackend(model_repo=parakeet_repo, language=language)

    if backend_name in {"groq", "groq-whisper", "groq_whisper"}:
        return GroqBackend(language=language)

    if backend_name in {"whisper.cpp", "whisper_cpp"}:
        cpp_config = with_overrides(
            transcriber_cfg,
            backend=backend_name,
            language=language,
        )
        binary_path = Path(cpp_config.whisper_cpp_binary).expanduser()
        if WhisperCppBackend is not None and binary_path.exists():
            try:
                return WhisperCppBackend(cpp_config)
            except FileNotFoundError as exc:
                # Binary present but the GGML model is missing — degrade rather
                # than crash the transcriber on the first Start Call.
                logger.warning(
                    "whisper.cpp backend could not start (%s); run scripts/install.sh to "
                    "(re)build it and fetch the GGML model. Falling back to mlx-whisper.",
                    exc,
                )
                return _fallback_to_mlx()

        if WhisperCppBackend is None:
            logger.warning(
                "whisper.cpp backend module is unavailable; falling back to mlx-whisper.",
            )
        else:
            logger.warning(
                "whisper.cpp binary not found at %s; run scripts/install.sh to build it. "
                "Falling back to mlx-whisper.",
                binary_path,
            )
        return _fallback_to_mlx()

    raise ValueError(f"Unsupported transcription backend: {backend_name}")


__all__ = [
    "TranscriptionBackend",
    "GroqBackend",
    "MlxWhisperBackend",
    "ParakeetMlxBackend",
    "WhisperCppBackend",
    "create_backend",
]
