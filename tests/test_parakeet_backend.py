"""Unit tests for the Parakeet (parakeet-mlx) transcription backend.

The real ``parakeet_mlx`` model is never loaded here — the model object is
patched in (mirroring how ``test_transcriber_backends.py`` stubs the whisper.cpp
backend), so these tests run with no network and no model download.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import numpy as np
import pytest

from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from sales_copilot.modules.transcriber.backends.parakeet_backend import (
    _MISSING_DEP_HINT,
    ParakeetMlxBackend,
)


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.sentences: list[object] = []


class _FakeStreamer:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> _FakeStreamer:
        self.entered = True
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.exited = True
        return False


class _FakeModel:
    def __init__(self, text: str = "hallo wereld") -> None:
        self._text = text
        self.transcribe_calls: list[str] = []
        self.last_streamer: _FakeStreamer | None = None

    def transcribe(self, path: str, **_kwargs: object) -> _FakeResult:
        self.transcribe_calls.append(path)
        return _FakeResult(self._text)

    def transcribe_stream(self, context_size=(256, 256), depth=1) -> _FakeStreamer:  # noqa: ANN001
        self.last_streamer = _FakeStreamer()
        return self.last_streamer


@pytest.mark.asyncio
async def test_transcribe_array_returns_parsed_text() -> None:
    backend = ParakeetMlxBackend()
    backend._model = _FakeModel("hallo wereld")  # noqa: SLF001 — bypass model download

    out = await backend.transcribe(np.full(16000, 0.1, dtype=np.float32))

    assert out == "hallo wereld"
    assert backend._model.transcribe_calls  # noqa: SLF001 — the batch path was used


@pytest.mark.asyncio
async def test_transcribe_empty_audio_returns_empty() -> None:
    backend = ParakeetMlxBackend()
    backend._model = _FakeModel("ignored")  # noqa: SLF001

    out = await backend.transcribe(np.zeros(0, dtype=np.float32))

    assert out == ""


@pytest.mark.asyncio
async def test_transcribe_file_returns_parsed_text(tmp_path) -> None:
    backend = ParakeetMlxBackend()
    backend._model = _FakeModel("dank je wel")  # noqa: SLF001

    audio_path = tmp_path / "call.wav"
    audio_path.write_bytes(b"not-real-audio")

    out = await backend.transcribe_file(audio_path)

    assert out == "dank je wel"
    assert backend._model.transcribe_calls == [str(audio_path)]  # noqa: SLF001


def test_transcribe_stream_yields_streamer_from_model() -> None:
    backend = ParakeetMlxBackend()
    fake = _FakeModel()
    backend._model = fake  # noqa: SLF001

    with backend.transcribe_stream(context_size=(128, 128)) as streamer:
        assert streamer is fake.last_streamer
        assert streamer.entered is True

    assert fake.last_streamer is not None
    assert fake.last_streamer.exited is True


def _force_parakeet_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the factory's import-availability check see parakeet_mlx as installed.

    parakeet-mlx is an optional extra not present in the CI profile (.[full,dev]),
    so the factory's ``find_spec`` check would otherwise fall back to mlx-whisper.
    The backend constructor lazy-imports parakeet_mlx, so building it needs no
    real package.
    """
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args: object, **kwargs: object):
        if name == "parakeet_mlx":
            return real_find_spec("os")  # any valid (truthy) ModuleSpec
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)


def test_create_backend_parakeet_returns_parakeet_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_parakeet_available(monkeypatch)
    backend = create_backend({"backend": "parakeet", "language": "nl"})

    assert isinstance(backend, ParakeetMlxBackend)
    assert backend.model_repo == "mlx-community/parakeet-tdt-0.6b-v3"
    assert backend.language == "nl"


def test_create_backend_parakeet_mlx_alias_returns_parakeet_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_parakeet_available(monkeypatch)
    backend = create_backend({"backend": "parakeet-mlx"})

    assert isinstance(backend, ParakeetMlxBackend)


def test_create_backend_parakeet_missing_dep_falls_back_to_mlx(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # When parakeet-mlx is not importable, create_backend must degrade to
    # mlx-whisper with an actionable warning rather than crash the engine.
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def _fake_find_spec(name: str, *args: object, **kwargs: object):
        if name == "parakeet_mlx":
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    caplog.set_level(logging.WARNING)

    backend = create_backend({"backend": "parakeet"})

    assert isinstance(backend, MlxWhisperBackend)
    assert "parakeet-mlx is not installed" in caplog.text
    assert "pip install '.[parakeet]'" in caplog.text


def test_missing_parakeet_mlx_import_raises_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setting the module to None in sys.modules makes `import parakeet_mlx`
    # raise ImportError, which the backend wraps in a clear, actionable error.
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)
    backend = ParakeetMlxBackend()

    with pytest.raises(RuntimeError) as excinfo:
        backend._ensure_model()  # noqa: SLF001

    message = str(excinfo.value)
    assert message == _MISSING_DEP_HINT
    assert "pip install '.[parakeet]'" in message


@pytest.mark.asyncio
async def test_missing_dep_propagates_through_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "parakeet_mlx", None)
    backend = ParakeetMlxBackend()

    with pytest.raises(RuntimeError):
        await backend.warmup()


@pytest.mark.asyncio
async def test_start_stop_are_noops() -> None:
    backend = ParakeetMlxBackend()
    stop_event = asyncio.Event()

    await backend.start(stop_event)
    await backend.stop()  # must not raise; no model load triggered
