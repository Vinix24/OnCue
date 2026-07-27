from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sales_copilot.core.config import TranscriberConfig
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend


def _make_backend(tmp_path, *, vocabulary_enabled: bool = False) -> WhisperCppBackend:
    binary = tmp_path / "whisper-cli"
    model = tmp_path / "ggml-large-v3.bin"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    model.write_text("model", encoding="utf-8")
    config = TranscriberConfig(
        whisper_cpp_binary=str(binary),
        whisper_cpp_model_path=str(model),
        whisper_cpp_threads=6,
        language="nl",
        whisper_cpp_server_enabled=False,
        vocabulary_enabled=vocabulary_enabled,
    )
    return WhisperCppBackend(config, temp_dir=tmp_path / "chunks")


def test_backend_builds_expected_command(tmp_path) -> None:
    backend = _make_backend(tmp_path)

    command = backend.describe_command()

    assert "-m" in command
    assert "-mc 0" in command
    assert "-tp 0" in command
    assert "-t 6" in command


def test_backend_includes_initial_prompt_in_command_when_vocab_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    backend = _make_backend(tmp_path, vocabulary_enabled=True)
    monkeypatch.setattr(backend, "_initial_prompt", "Dutch sales terms: offerte, CRM")

    command = backend._command_for(Path("<chunk.wav>"))  # noqa: SLF001

    assert "--prompt" in command
    prompt_index = command.index("--prompt")
    assert command[prompt_index + 1] == "Dutch sales terms: offerte, CRM"


def test_backend_omits_prompt_when_vocab_disabled(tmp_path) -> None:
    backend = _make_backend(tmp_path, vocabulary_enabled=False)

    command = backend._command_for(Path("<chunk.wav>"))  # noqa: SLF001

    assert "--prompt" not in command


def test_backend_uses_default_vocabulary_prompt_when_enabled(tmp_path) -> None:
    backend = _make_backend(tmp_path, vocabulary_enabled=True)

    command = backend._command_for(Path("<chunk.wav>"))  # noqa: SLF001

    assert "--prompt" in command
    prompt_index = command.index("--prompt")
    assert "offerte" in command[prompt_index + 1]


def test_backend_transcribes_chunk_and_parses_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backend = _make_backend(tmp_path)

    def fake_run(command: list[str], **kwargs):
        assert command[0].endswith("whisper-cli")
        assert command[-2:] == ["-tp", "0"]
        wav_path = command[command.index("-f") + 1]
        assert wav_path.endswith(".wav")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="[00:00:00.000 --> 00:00:01.000] hallo wereld\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=1000, end_ms=2000)

    assert payload == {
        "text": "hallo wereld",
        "start": 1.0,
        "end": 2.0,
        "is_final": True,
    }


def test_backend_skips_failed_chunk_and_logs_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _make_backend(tmp_path)

    def fake_run(command: list[str], **kwargs):
        return subprocess.CompletedProcess(args=command, returncode=2, stdout="", stderr="bad chunk")

    monkeypatch.setattr(subprocess, "run", fake_run)
    caplog.set_level(logging.WARNING)

    payload = backend.transcribe_chunk(np.ones(8000, dtype=np.float32), start_ms=0, end_ms=500)

    assert payload is None
    assert "bad chunk" in caplog.text


def test_backend_drops_chunk_on_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _make_backend(tmp_path)

    def fake_run(command: list[str], **kwargs):
        # The transcription call must be bounded by the configured wall-clock timeout.
        assert kwargs.get("timeout") == backend._config.whisper_cpp_timeout_s  # noqa: SLF001
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    caplog.set_level(logging.WARNING)

    payload = backend.transcribe_chunk(np.ones(16000, dtype=np.float32), start_ms=0, end_ms=1000)

    assert payload is None
    assert "timed out" in caplog.text


def test_backend_default_timeout_is_thirty_seconds(tmp_path) -> None:
    backend = _make_backend(tmp_path)

    assert backend._config.whisper_cpp_timeout_s == 30.0  # noqa: SLF001


def test_backend_fails_fast_when_binary_missing(tmp_path) -> None:
    model = tmp_path / "ggml-large-v3.bin"
    model.write_text("model", encoding="utf-8")
    config = TranscriberConfig(
        whisper_cpp_binary=str(tmp_path / "missing-whisper-cli"),
        whisper_cpp_model_path=str(model),
    )

    with pytest.raises(FileNotFoundError):
        WhisperCppBackend(config)
