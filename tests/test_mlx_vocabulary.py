"""Tests for vocabulary biasing in the MLX Whisper backend."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend


def _inject_fake_mlx_whisper(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> dict:
    captured: dict = {}

    class _FakeMlxWhisper:
        @staticmethod
        def transcribe(_audio, **kwargs):
            captured["kwargs"] = kwargs
            return result

    monkeypatch.setitem(sys.modules, "mlx_whisper", _FakeMlxWhisper())
    return captured


def test_mlx_backend_stores_initial_prompt() -> None:
    backend = MlxWhisperBackend(language="nl", initial_prompt="offerte, CRM")

    assert backend.initial_prompt == "offerte, CRM"


def test_mlx_backend_passes_initial_prompt_to_transcribe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend must forward the prompt as the `initial_prompt` kwarg."""
    backend = MlxWhisperBackend(language="nl", initial_prompt="offerte, CRM")
    captured = _inject_fake_mlx_whisper(monkeypatch, {"text": "test"})

    backend._transcribe_sync(audio=None)  # noqa: SLF001

    assert captured["kwargs"].get("initial_prompt") == "offerte, CRM"
    assert captured["kwargs"].get("language") == "nl"


def test_mlx_backend_omits_initial_prompt_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When initial_prompt is None the backend must not emit the kwarg."""
    backend = MlxWhisperBackend(language="nl", initial_prompt=None)
    captured = _inject_fake_mlx_whisper(monkeypatch, {"text": "test"})

    backend._transcribe_sync(audio=None)  # noqa: SLF001

    assert "initial_prompt" not in captured["kwargs"]
