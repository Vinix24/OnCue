from __future__ import annotations

import httpx
import numpy as np
import pytest

from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.backends.groq_backend import (
    GROQ_DEFAULT_BASE_URL,
    GROQ_WHISPER_MODEL,
    GroqBackend,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(
        self,
        calls: list[dict],
        *,
        payload: dict | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self._calls = calls
        self._payload = payload if payload is not None else {"text": "hallo wereld"}
        self._raise_error = raise_error
        self.closed = False

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self._calls.append({"url": url, **kwargs})
        if self._raise_error is not None:
            raise self._raise_error
        return _FakeResponse(self._payload)

    async def aclose(self) -> None:
        self.closed = True


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeAsyncClient) -> None:
    import sales_copilot.modules.transcriber.backends.groq_backend as groq_backend_module

    monkeypatch.setattr(groq_backend_module.httpx, "AsyncClient", lambda **kwargs: fake_client)


def test_groq_backend_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqBackend(api_key=None)


def test_groq_backend_reads_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk-from-env")

    backend = GroqBackend()

    assert backend._api_key == "gk-from-env"  # noqa: SLF001


def test_create_backend_groq_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        create_backend({"backend": "groq"})


def test_create_backend_returns_groq_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")

    backend = create_backend({"backend": "groq", "language": "nl"})

    assert isinstance(backend, GroqBackend)


def test_create_backend_groq_without_httpx_raises_actionable_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When httpx is absent, GroqBackend resolves to None (see backends/__init__.py's
    try/except around the import, mirroring WhisperCppBackend). Selecting 'groq' must
    then fail loudly with the missing dependency and the fix -- not a bare ImportError
    surfacing from deep inside a live call.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gk-test")
    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.backends.GroqBackend",
        None,
    )

    with pytest.raises(RuntimeError, match="httpx") as exc_info:
        create_backend({"backend": "groq"})

    assert "pip install" in str(exc_info.value)


async def test_groq_backend_posts_to_correct_endpoint_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    fake_client = _FakeAsyncClient(calls)
    _patch_async_client(monkeypatch, fake_client)
    backend = GroqBackend(api_key="gk-test", language="nl")

    text = await backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert text == "hallo wereld"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == f"{GROQ_DEFAULT_BASE_URL}/audio/transcriptions"
    assert call["data"]["model"] == GROQ_WHISPER_MODEL
    assert call["data"]["language"] == "nl"
    assert call["headers"]["Authorization"] == "Bearer gk-test"
    assert "file" in call["files"]
    assert fake_client.closed is True


async def test_groq_backend_network_error_raises_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeAsyncClient([], raise_error=httpx.ConnectError("boom"))
    _patch_async_client(monkeypatch, fake_client)
    backend = GroqBackend(api_key="gk-test")

    with pytest.raises(RuntimeError, match="Groq transcription request failed"):
        await backend.transcribe(np.zeros(16000, dtype=np.float32))


async def test_groq_backend_reuses_persistent_client_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    fake_client = _FakeAsyncClient(calls)
    _patch_async_client(monkeypatch, fake_client)
    backend = GroqBackend(api_key="gk-test")

    import asyncio

    await backend.start(asyncio.Event())
    await backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert fake_client.closed is False
    await backend.stop()
    assert fake_client.closed is True
