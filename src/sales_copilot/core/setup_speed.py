"""Setup speed check: transcribe a short fixed clip, get the real-time factor, pick the model.

Nothing is downloaded and the microphone is never used.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from sales_copilot.core import i18n
from sales_copilot.core.config import env, env_float
from sales_copilot.core.env_writer import read_env_value, set_env_var
from sales_copilot.core.paths import resolve_app_resource
from sales_copilot.modules.transcriber.backends.model_names import (
    MODEL_MAP,
    download_hint,
    resolve_ggml_filename,
    resolve_mlx_repo,
)

DEFAULT_MODEL = "large-v3-turbo"
TOO_SLOW_KEY = "LIVE_TRANSCRIPTION_TOO_SLOW"
CLIP_NAMES = ("mock_00.wav", "mock_01.wav", "mock_02.wav", "mock_03.wav")
SAMPLE_RATE = 16000
TIER_FAST, TIER_MEDIUM, TIER_TOO_SLOW = "fast", "medium", "too_slow"


class SpeedCheckError(RuntimeError):
    """The measurement could not run; there is no real-time factor to decide on."""


@dataclass(frozen=True)
class SpeedMeasurement:
    rtf: float
    audio_seconds: float
    processing_seconds: float


@dataclass(frozen=True)
class SpeedChoice:
    tier: str
    model: str
    live_ok: bool


@dataclass(frozen=True)
class SpeedThresholds:
    fast_max_rtf: float
    slow_min_rtf: float
    medium_model: str

    @classmethod
    def from_env(cls) -> SpeedThresholds:
        fast = env_float("SETUP_SPEED_FAST_MAX_RTF", 0.5)
        slow = env_float("SETUP_SPEED_SLOW_MIN_RTF", 1.0)
        medium = (env("SETUP_SPEED_MEDIUM_MODEL", "base") or "base").strip()
        if fast is None or slow is None or not 0 < fast < slow:
            raise ValueError("SETUP_SPEED_FAST_MAX_RTF must be above 0 and below SETUP_SPEED_SLOW_MIN_RTF")
        if medium not in MODEL_MAP:
            raise ValueError(f"SETUP_SPEED_MEDIUM_MODEL {medium!r} is not one of {sorted(MODEL_MAP)}")
        return cls(fast, slow, medium)


def decide(rtf: float, thresholds: SpeedThresholds, current_model: str = DEFAULT_MODEL) -> SpeedChoice:
    """Below fast_max keep the model, below slow_min take the smaller one, else too slow."""
    if isinstance(rtf, bool) or not isinstance(rtf, int | float) or not math.isfinite(rtf) or rtf <= 0:
        raise ValueError(f"real-time factor must be a positive finite number, got {rtf!r}")
    if rtf < thresholds.fast_max_rtf:
        return SpeedChoice(TIER_FAST, current_model, True)
    if rtf < thresholds.slow_min_rtf:
        return SpeedChoice(TIER_MEDIUM, thresholds.medium_model, True)
    return SpeedChoice(TIER_TOO_SLOW, current_model, False)


def clip_paths() -> list[Path]:
    root = resolve_app_resource("scripts/benchmark_assets")
    paths = [root / name for name in CLIP_NAMES]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise SpeedCheckError(f"speed-check clips not found: {', '.join(missing)}")
    return paths


def _read_clip(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SpeedCheckError(f"{path.name} is not 16 kHz mono 16-bit")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def measure_realtime_factor(backend: Any | None = None, clips: list[Path] | None = None) -> SpeedMeasurement:
    """Real-time factor of the fixed clips, measured after a warm-up like a live chunk.

    No text for any clip raises ``SpeedCheckError``: a failed run is never a fast one.
    """
    from sales_copilot.core.config import TranscriberConfig
    from sales_copilot.modules.transcriber.backends import create_backend

    audio_clips = [_read_clip(p) for p in (clips if clips is not None else clip_paths())]
    owned = backend is None
    if backend is None:
        try:
            backend = create_backend({"backend": TranscriberConfig.from_env().backend})
        except RuntimeError as exc:
            raise SpeedCheckError(f"no transcription backend is available: {exc}") from exc

    async def _run() -> tuple[float, int]:
        await backend.start(asyncio.Event())
        try:
            await backend.warmup()
            texts = 0
            started = time.perf_counter()
            for clip in audio_clips:
                if (await backend.transcribe(clip)).strip():
                    texts += 1
            return time.perf_counter() - started, texts
        finally:
            if owned:
                await backend.stop()

    try:
        processing, texts = asyncio.run(_run())
    except Exception as exc:
        raise SpeedCheckError(f"transcription failed during the speed check: {exc}") from exc
    if texts == 0:
        raise SpeedCheckError("the backend returned no text for any speed-check clip")
    audio_seconds = sum(len(c) for c in audio_clips) / SAMPLE_RATE
    if audio_seconds <= 0 or processing <= 0:
        raise SpeedCheckError("the speed check measured no time")
    return SpeedMeasurement(processing / audio_seconds, audio_seconds, processing)


def model_is_hand_set(env_path: str | Path | None = None) -> bool:
    value = (read_env_value("WHISPER_MODEL", env_path=env_path) or "").strip()
    return bool(value) and value != DEFAULT_MODEL


def _is_mlx() -> bool:
    from sales_copilot.core.config import TranscriberConfig

    return TranscriberConfig.from_env().backend.strip().lower() in {"mlx-whisper", "mlx_whisper", "mlx"}


def _ggml_present(model: str) -> bool:
    """Is the model available for the configured backend (never downloads)?"""
    if _is_mlx():
        from sales_copilot.wizard.detectors import _mlx_model_present

        return _mlx_model_present(model)
    return resolve_app_resource(f"vendor/whisper.cpp/models/{resolve_ggml_filename(model)}").is_file()


def _hint(model: str) -> str:
    if _is_mlx():
        return f"huggingface-cli download {resolve_mlx_repo(model)}"
    return download_hint(model)


def apply_choice(choice: SpeedChoice, env_path: str | Path | None = None) -> list[str]:
    """Write the choice to .env and return the i18n message keys to show.

    A WHISPER_MODEL the user set by hand is kept; the choice is then only
    reported. A smaller model is written only once its file is present.
    """
    set_env_var(TOO_SLOW_KEY, "false" if choice.live_ok else "true", env_path=env_path)
    if choice.tier == TIER_TOO_SLOW:
        return ["setup_speed.too_slow"]
    if choice.tier == TIER_FAST:
        return ["setup_speed.fast"]
    if model_is_hand_set(env_path):
        return ["setup_speed.kept_by_hand"]
    if not _ggml_present(choice.model):
        return ["setup_speed.medium_missing"]
    set_env_var("WHISPER_MODEL", choice.model, env_path=env_path)
    return ["setup_speed.medium"]


def run_speed_check(env_path: str | Path | None = None) -> tuple[SpeedChoice, SpeedMeasurement, str]:
    """Measure, decide, write to .env and return the plain-language message."""
    from sales_copilot.core.config import TranscriberConfig

    thresholds = SpeedThresholds.from_env()
    measurement = measure_realtime_factor()
    choice = decide(measurement.rtf, thresholds, TranscriberConfig.from_env().model)
    keys = apply_choice(choice, env_path)
    texts = (i18n.t(k, rtf=f"{measurement.rtf:.2f}", model=choice.model, hint=_hint(choice.model)) for k in keys)
    return choice, measurement, " ".join(texts)


def main(argv: list[str] | None = None) -> int:
    from sales_copilot.core.config import load_env

    parser = argparse.ArgumentParser(description="Measure transcription speed and pick the model.")
    parser.add_argument("--env-path", default=None, help="the .env file to write (default: the project .env)")
    args = parser.parse_args(argv)
    load_env()
    try:
        _choice, _measurement, message = run_speed_check(args.env_path)
    except (SpeedCheckError, ValueError) as exc:
        print(i18n.t("setup_speed.failed", detail=str(exc)), file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
