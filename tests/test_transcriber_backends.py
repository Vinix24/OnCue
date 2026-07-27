import asyncio
import logging

import numpy as np
import pytest

from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend


def _make_whisper_cpp_artifacts(tmp_path):
    """Create an executable stub binary + model file for the whisper.cpp backend.

    WhisperCppBackend only validates that both paths exist and that the binary is
    executable at construction; it does not load the model, so stub files suffice.
    """
    binary = tmp_path / "whisper-cli"
    model = tmp_path / "ggml-large-v3-turbo.bin"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    model.write_text("model", encoding="utf-8")
    return binary, model


def test_create_backend_default_is_whisper_cpp_with_graceful_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The default backend is now whisper.cpp. When the compiled binary is absent,
    # create_backend must degrade to mlx-whisper instead of crashing the engine.
    missing = tmp_path / "nope" / "whisper-cli"
    monkeypatch.setenv("WHISPER_CPP_BINARY", str(missing))
    caplog.set_level(logging.WARNING)

    backend = create_backend()  # no backend key -> default whisper.cpp

    assert isinstance(backend, MlxWhisperBackend)
    assert "whisper.cpp binary not found" in caplog.text


def test_create_backend_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError):
        create_backend({"backend": "unknown"})


def test_create_backend_whisper_cpp_missing_binary_falls_back_to_mlx(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # whisper.cpp explicitly selected, but the binary path points at nothing:
    # graceful fallback to mlx-whisper, with an actionable warning, no raise.
    missing = tmp_path / "build" / "bin" / "whisper-cli"
    caplog.set_level(logging.WARNING)

    backend = create_backend(
        {
            "backend": "whisper.cpp",
            "language": "en",
            "whisper_cpp_binary": str(missing),
        }
    )

    assert isinstance(backend, MlxWhisperBackend)
    assert backend.language == "en"
    assert "whisper.cpp binary not found" in caplog.text
    assert "scripts/install.sh" in caplog.text
    assert str(missing) in caplog.text


def test_create_backend_whisper_cpp_present_returns_whisper_cpp_backend(
    tmp_path,
) -> None:
    # When the binary (and model) exist, the real whisper.cpp backend is used.
    binary, model = _make_whisper_cpp_artifacts(tmp_path)

    backend = create_backend(
        {
            "backend": "whisper.cpp",
            "language": "en",
            "whisper_cpp_binary": str(binary),
            "whisper_cpp_model_path": str(model),
            "whisper_cpp_threads": 8,
        }
    )

    assert isinstance(backend, WhisperCppBackend)
    assert backend._config.language == "en"  # noqa: SLF001
    assert backend._config.whisper_cpp_threads == 8  # noqa: SLF001


def test_create_backend_supports_whisper_cpp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class _FakeWhisperCppBackend:
        def __init__(self, config):
            created["language"] = config.language
            created["threads"] = config.whisper_cpp_threads

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.backends.WhisperCppBackend",
        _FakeWhisperCppBackend,
    )

    binary, model = _make_whisper_cpp_artifacts(tmp_path)
    backend = create_backend(
        {
            "backend": "whisper.cpp",
            "language": "en",
            "whisper_cpp_binary": str(binary),
            "whisper_cpp_model_path": str(model),
            "whisper_cpp_threads": 8,
        }
    )

    assert isinstance(backend, _FakeWhisperCppBackend)
    assert created["language"] == "en"
    assert created["threads"] == 8


@pytest.mark.asyncio
async def test_mlx_backend_start_stop_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MlxWhisperBackend()
    stop_event = asyncio.Event()
    monkeypatch.setattr(MlxWhisperBackend, "_transcribe_sync", lambda self, _audio: "")

    await backend.start(stop_event)
    out = await backend.transcribe(np.zeros(16000, dtype=np.float32))
    await backend.stop()

    assert isinstance(out, str)


@pytest.mark.asyncio
async def test_mlx_backend_transcribe_times_out_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    backend = MlxWhisperBackend(timeout_s=0.05)

    def _slow(self, _audio: np.ndarray) -> str:
        time.sleep(0.3)
        return "te traag"

    monkeypatch.setattr(MlxWhisperBackend, "_transcribe_sync", _slow)

    # Bounded: a wedged decode degrades to empty instead of stalling forever.
    out = await asyncio.wait_for(
        backend.transcribe(np.zeros(16000, dtype=np.float32)), timeout=2.0
    )

    assert out == ""


@pytest.mark.asyncio
async def test_mlx_backend_warmup_uses_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MlxWhisperBackend()
    seen: dict[str, object] = {}

    def _fake_transcribe_sync(self, audio: np.ndarray) -> str:
        seen["shape"] = audio.shape
        seen["dtype"] = audio.dtype
        return ""

    monkeypatch.setattr(MlxWhisperBackend, "_transcribe_sync", _fake_transcribe_sync)

    await backend.warmup()

    assert seen["shape"] == (16000,)
    assert seen["dtype"] == np.float32


@pytest.mark.asyncio
async def test_whisper_cpp_backend_warmup_uses_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = object.__new__(WhisperCppBackend)
    seen: dict[str, object] = {}

    def _fake_transcribe_chunk(audio: np.ndarray, *, start_ms: int, end_ms: int):
        seen["shape"] = audio.shape
        seen["dtype"] = audio.dtype
        seen["start_ms"] = start_ms
        seen["end_ms"] = end_ms
        return None

    monkeypatch.setattr(backend, "transcribe_chunk", _fake_transcribe_chunk)

    await backend.warmup()

    assert seen["shape"] == (16000,)
    assert seen["dtype"] == np.float32
    assert seen["start_ms"] == 0
    assert seen["end_ms"] == 1000
