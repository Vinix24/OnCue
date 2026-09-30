"""Tests for scripts/transcribe_file.py: the --model override for whisper.cpp."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import transcribe_file  # noqa: E402
from transcribe_file import _resolve_ggml_path  # noqa: E402

from sales_copilot.core.config import TranscriberConfig  # noqa: E402


class _FakeBackend:
    async def warmup(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_resolve_ggml_path_no_override_returns_configured_path_unchanged() -> None:
    configured = "/some/vendor/whisper.cpp/models/ggml-large-v3-turbo.bin"
    assert _resolve_ggml_path(configured, None) == configured


def test_resolve_ggml_path_builds_filename_in_same_directory(tmp_path: Path) -> None:
    models_dir = tmp_path / "vendor" / "whisper.cpp" / "models"
    models_dir.mkdir(parents=True)
    configured = str(models_dir / "ggml-large-v3-turbo.bin")
    (models_dir / "ggml-large-v3.bin").write_bytes(b"fake-ggml")

    resolved = _resolve_ggml_path(configured, "large-v3")

    assert resolved == str(models_dir / "ggml-large-v3.bin")


def test_resolve_ggml_path_missing_model_raises_with_download_command(tmp_path: Path) -> None:
    models_dir = tmp_path / "vendor" / "whisper.cpp" / "models"
    models_dir.mkdir(parents=True)
    configured = str(models_dir / "ggml-large-v3-turbo.bin")

    with pytest.raises(FileNotFoundError) as exc_info:
        _resolve_ggml_path(configured, "nonexistent-model")

    message = str(exc_info.value)
    assert "nonexistent-model" in message
    assert "download-ggml-model.sh nonexistent-model" in message


def test_resolve_ggml_path_rejects_slash_in_model_name(tmp_path: Path) -> None:
    models_dir = tmp_path / "vendor" / "whisper.cpp" / "models"
    models_dir.mkdir(parents=True)
    configured = str(models_dir / "ggml-large-v3-turbo.bin")

    with pytest.raises(FileNotFoundError) as exc_info:
        _resolve_ggml_path(configured, "large/v3")

    assert "large/v3" in str(exc_info.value)


def test_resolve_ggml_path_rejects_dotdot_in_model_name(tmp_path: Path) -> None:
    models_dir = tmp_path / "vendor" / "whisper.cpp" / "models"
    models_dir.mkdir(parents=True)
    configured = str(models_dir / "ggml-large-v3-turbo.bin")

    with pytest.raises(FileNotFoundError) as exc_info:
        _resolve_ggml_path(configured, "../../etc/passwd")

    assert "../../etc/passwd" in str(exc_info.value)


def test_transcribe_mlx_backend_skips_ggml_resolution_when_model_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--model with --backend mlx-whisper must not require a GGML file at all."""
    captured_config: dict[str, object] = {}

    def _fake_create_backend(config: dict[str, object]) -> _FakeBackend:
        captured_config.update(config)
        return _FakeBackend()

    monkeypatch.setattr(transcribe_file, "create_backend", _fake_create_backend)
    monkeypatch.setattr(
        transcribe_file,
        "_load_wav_as_float32",
        lambda path: (np.zeros(0, dtype=np.float32), 16000),
    )

    transcribe_file.load_env()
    cfg = TranscriberConfig.from_env()

    asyncio.run(
        transcribe_file._transcribe(
            Path("irrelevant.wav"),
            backend_name="mlx-whisper",
            chunk_seconds=30,
            include_timestamps=True,
            model_name="medium",
        )
    )

    assert captured_config["backend"] == "mlx-whisper"
    assert captured_config["model_repo"] == "mlx-community/whisper-medium"
    assert captured_config["whisper_cpp_model_path"] == cfg.whisper_cpp_model_path


def test_transcribe_whisper_cpp_backend_still_raises_for_missing_model_override() -> None:
    """--model with --backend whisper.cpp must still fail hard on a missing GGML file."""
    with pytest.raises(FileNotFoundError):
        asyncio.run(
            transcribe_file._transcribe(
                Path("irrelevant.wav"),
                backend_name="whisper.cpp",
                chunk_seconds=30,
                include_timestamps=True,
                model_name="nonexistent-xyz",
            )
        )
