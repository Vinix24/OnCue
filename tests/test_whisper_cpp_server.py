from __future__ import annotations

import asyncio
import subprocess

import httpx
import numpy as np
import pytest

from sales_copilot.core.config import TranscriberConfig
from sales_copilot.modules.transcriber.backends import whisper_cpp_backend as wcb
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend


@pytest.fixture(autouse=True)
def _no_orphan_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orphan scan shells out to ``ps``; these tests count subprocess.run calls."""
    monkeypatch.setattr(wcb, "stop_orphans", lambda _binary: [])


class FakePopen:
    """Stand-in for a live whisper-server subprocess."""

    def __init__(self, *_args, exit_code: int | None = None, **_kwargs) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.terminated or self.killed:
            return 0
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakeResponse:
    def __init__(self, *, status_code: int = 200, json_body: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self) -> dict:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]


class FakeClient:
    """Replaces ``httpx.Client``; records posts and returns a canned response."""

    last_instance: FakeClient | None = None

    def __init__(self, *_args, **_kwargs) -> None:
        self.posts: list[dict] = []
        self.closed = False
        self.response = FakeResponse(json_body={"text": "hallo vanaf de server"})
        self.raise_on_post: Exception | None = None
        FakeClient.last_instance = self

    def post(self, url: str, *, files=None, data=None) -> FakeResponse:
        self.posts.append({"url": url, "files": files, "data": data})
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return self.response

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_client() -> None:
    FakeClient.last_instance = None


def _make_backend(tmp_path, **overrides) -> WhisperCppBackend:
    cli = tmp_path / "whisper-cli"
    server = tmp_path / "whisper-server"
    model = tmp_path / "ggml-large-v3.bin"
    for binary in (cli, server):
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    model.write_text("model", encoding="utf-8")
    kwargs = {
        "whisper_cpp_binary": str(cli),
        "whisper_cpp_server_binary": str(server),
        "whisper_cpp_model_path": str(model),
        "whisper_cpp_threads": 4,
        "language": "nl",
        "whisper_cpp_server_enabled": True,
        "whisper_cpp_server_startup_timeout_s": 0.5,
    }
    kwargs.update(overrides)
    return WhisperCppBackend(TranscriberConfig(**kwargs), temp_dir=tmp_path / "chunks")


def _ready_get(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wcb.httpx, "get", lambda *a, **k: FakeResponse(status_code=200))


def test_server_transcribes_chunk_over_http(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(wcb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    def fail_run(*_a, **_k):  # CLI must not be touched on the server fast path
        raise AssertionError("whisper-cli should not run when the server handles the chunk")

    monkeypatch.setattr(wcb.subprocess, "run", fail_run)

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=1000, end_ms=2000)

    assert payload == {
        "text": "hallo vanaf de server",
        "start": 1.0,
        "end": 2.0,
        "is_final": True,
    }
    assert FakeClient.last_instance is not None
    post = FakeClient.last_instance.posts[0]
    assert post["url"].endswith("/inference")
    assert post["data"]["language"] == "nl"
    assert post["files"]["file"][0] == "chunk.wav"


def test_server_failure_falls_back_to_cli(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path)
    # Server process dies immediately (e.g. bad model) → readiness fails.
    monkeypatch.setattr(wcb.subprocess, "Popen", lambda *a, **k: FakePopen(exit_code=1))
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    cli_calls = {"n": 0}

    def fake_run(command, **_kwargs):
        cli_calls["n"] += 1
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[00:00:00.000 --> 00:00:01.000] cli antwoord\n",
            stderr="",
        )

    monkeypatch.setattr(wcb.subprocess, "run", fake_run)

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)

    assert payload is not None
    assert payload["text"] == "cli antwoord"
    assert cli_calls["n"] == 1
    # Server is disabled after a failed start; no FakeClient was ever built.
    assert FakeClient.last_instance is None


def test_http_error_falls_back_to_cli_without_disabling_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(wcb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[00:00:00.000 --> 00:00:01.000] cli redt het\n",
            stderr="",
        )

    monkeypatch.setattr(wcb.subprocess, "run", fake_run)

    assert backend._ensure_server() is True  # noqa: SLF001
    FakeClient.last_instance.raise_on_post = httpx.ConnectError("boom")  # type: ignore[union-attr]

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)

    assert payload is not None
    assert payload["text"] == "cli redt het"
    # Process is still alive, so the server stays enabled for later chunks.
    assert backend._server_disabled is False  # noqa: SLF001


def test_server_disabled_uses_cli_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path, whisper_cpp_server_enabled=False)

    def no_popen(*_a, **_k):
        raise AssertionError("server must not start when disabled")

    monkeypatch.setattr(wcb.subprocess, "Popen", no_popen)

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[00:00:00.000 --> 00:00:01.000] alleen cli\n",
            stderr="",
        )

    monkeypatch.setattr(wcb.subprocess, "run", fake_run)

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)

    assert payload is not None
    assert payload["text"] == "alleen cli"
    assert backend._ensure_server() is False  # noqa: SLF001


def test_empty_server_result_does_not_fall_back(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(wcb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    def fail_run(*_a, **_k):
        raise AssertionError("silence from the server must not trigger a CLI retry")

    monkeypatch.setattr(wcb.subprocess, "run", fail_run)

    assert backend._ensure_server() is True  # noqa: SLF001
    FakeClient.last_instance.response = FakeResponse(json_body={"text": "   "})  # type: ignore[union-attr]

    payload = backend.transcribe_chunk(np.zeros(16000, dtype=np.float32), start_ms=0, end_ms=1000)

    assert payload is None


def test_start_and_stop_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path)
    fake_proc = FakePopen()
    monkeypatch.setattr(wcb.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    async def drive() -> None:
        await backend.start(asyncio.Event())
        assert backend._server_started is True  # noqa: SLF001
        await backend.stop()

    asyncio.run(drive())

    assert fake_proc.terminated is True
    assert FakeClient.last_instance is not None
    assert FakeClient.last_instance.closed is True
    assert backend._server_started is False  # noqa: SLF001
