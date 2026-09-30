from __future__ import annotations

import importlib.util
import logging
import platform
import sys
from pathlib import Path
from typing import Any

from sales_copilot.core.config import TranscriberConfig, env, with_overrides
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from sales_copilot.modules.transcriber.backends.parakeet_backend import ParakeetMlxBackend
from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config

_whisper_cpp_import_error: Exception | None = None
try:  # pragma: no cover - optional backend path
    from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend
except Exception as _exc:  # pragma: no cover
    WhisperCppBackend = None  # type: ignore[assignment]
    _whisper_cpp_import_error = _exc

try:  # pragma: no cover - optional backend path
    # groq_backend imports httpx directly (it calls Groq's REST API without the
    # groq SDK). httpx is not a base/windows dependency -- guarded the same way
    # as WhisperCppBackend above so a capture-only install without it stays
    # importable; create_backend() below turns a selected-but-unavailable groq
    # backend into an actionable error instead of crashing this import.
    from sales_copilot.modules.transcriber.backends.groq_backend import GroqBackend
except Exception:  # pragma: no cover
    GroqBackend = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _missing_dependency_hint(exc: Exception | None) -> str:
    """Best-effort human-readable reason for an optional backend's import failure."""
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        return f"missing dependency `{exc.name}`"
    if exc is not None:
        return f"import error: {exc}"
    return "unavailable"


def _mlx_whisper_unusable_reason() -> str | None:
    """Return why mlx-whisper cannot run on this machine, or ``None`` if it can.

    mlx-whisper (and the mlx-backed parakeet backend) require Apple's MLX
    framework, which only runs on Apple Silicon macOS. Checked explicitly
    rather than just trying and catching, because ``MlxWhisperBackend``
    construction itself always succeeds (it imports ``mlx_whisper`` lazily,
    only inside ``_transcribe_sync``) -- so a backend that can never actually
    transcribe would otherwise look like a working fallback until the first
    real audio chunk.
    """
    if sys.platform != "darwin":
        return f"mlx-whisper requires macOS (Apple Silicon); this platform is {platform.system()}"
    if platform.machine() not in {"arm64", "arm64e"}:
        return f"mlx-whisper requires Apple Silicon (arm64); this Mac reports {platform.machine()}"
    if importlib.util.find_spec("mlx_whisper") is None:
        return 'mlx-whisper is not installed; install it with `pip install ".[transcriber]"`'
    return None


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
    where ``scripts/install.sh`` could not build it, or its module failed to
    import (missing httpx) — the transcriber degrades to mlx-whisper with a
    loud, actionable warning instead of raising ``FileNotFoundError`` and
    crashing the engine mid-call. That fallback only fires when mlx-whisper can
    actually run here (Apple Silicon macOS with the package installed): on any
    other platform, or a Mac without mlx-whisper installed, falling back would
    hand back a backend that can never transcribe with no indication anything
    is wrong, so ``create_backend()`` raises ``RuntimeError`` instead, naming
    both the original problem and why mlx-whisper cannot cover for it.
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

    def _fallback_to_mlx(reason: str) -> TranscriptionBackend:
        """Fall back to mlx-whisper, or refuse when that fallback is itself impossible.

        A fallback to a backend that cannot run on the current platform is not a
        fallback, it is a failure dressed up as one: it leaves the operator with
        no working transcription and no error telling them why. Refuse loudly
        instead, naming both the original problem and why mlx-whisper cannot
        cover for it here.
        """
        mlx_unusable = _mlx_whisper_unusable_reason()
        if mlx_unusable is not None:
            raise RuntimeError(
                f"{reason} Refusing to fall back to mlx-whisper: {mlx_unusable}. "
                "No transcription backend is available on this platform."
            )
        logger.warning("%s Falling back to mlx-whisper.", reason)
        try:
            return _build_mlx()
        except Exception as exc:  # pragma: no cover - mlx present but fails to construct
            raise RuntimeError(
                "No transcription backend available: whisper.cpp is not usable and "
                "mlx-whisper could not be constructed."
            ) from exc

    if backend_name in {"mlx-whisper", "mlx"}:
        return _build_mlx()

    if backend_name in {"parakeet", "parakeet-mlx"}:
        if importlib.util.find_spec("parakeet_mlx") is None:
            return _fallback_to_mlx(
                "parakeet backend selected but parakeet-mlx is not installed; install it "
                "with `pip install '.[parakeet]'`.",
            )
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
        if GroqBackend is None:
            raise RuntimeError(
                "Transcription backend 'groq' is selected but httpx is not installed. "
                "Install it with `pip install \".[groq]\"` (or `pip install httpx`)."
            )
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
                return _fallback_to_mlx(
                    f"whisper.cpp backend could not start ({exc}); run scripts/install.sh "
                    "to (re)build it and fetch the GGML model.",
                )

        if WhisperCppBackend is None:
            return _fallback_to_mlx(
                f"whisper.cpp backend module is unavailable ({_missing_dependency_hint(_whisper_cpp_import_error)}); "
                "install the missing package directly (e.g. `pip install httpx`), or reinstall "
                'with the platform extra that provides it (e.g. `pip install ".[windows]"` on '
                "Windows, `pip install \".[transcriber]\"` on macOS).",
            )
        return _fallback_to_mlx(
            f"whisper.cpp binary not found at {binary_path}; run scripts/install.sh to build it.",
        )

    raise ValueError(f"Unsupported transcription backend: {backend_name}")


__all__ = [
    "TranscriptionBackend",
    "GroqBackend",
    "MlxWhisperBackend",
    "ParakeetMlxBackend",
    "WhisperCppBackend",
    "create_backend",
]
