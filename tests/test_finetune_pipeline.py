from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from scripts.finetune.prepare_dataset import collect_entries, write_jsonl


def test_prepare_dataset_prefers_corrected_transcript(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_one = sessions_dir / "session-1"
    session_one.mkdir(parents=True)

    (session_one / "audio.wav").write_bytes(b"RIFF")
    (session_one / "transcript.json").write_text(
        json.dumps({"transcript": [{"text": "auto versie"}]}),
        encoding="utf-8",
    )
    (session_one / "transcript_corrected.json").write_text(
        json.dumps({"transcript": [{"text": "gecorrigeerde offerte tekst"}]}),
        encoding="utf-8",
    )

    session_two = sessions_dir / "session-2"
    session_two.mkdir(parents=True)
    (session_two / "audio.wav").write_bytes(b"RIFF")
    (session_two / "transcript.json").write_text(
        json.dumps({"transcript": [{"text": "propositie en pain points"}]}),
        encoding="utf-8",
    )

    entries = collect_entries(sessions_dir)

    assert len(entries) == 2
    assert entries[0]["text"] == "gecorrigeerde offerte tekst"
    assert entries[1]["text"] == "propositie en pain points"
    assert entries[0]["language"] == "nl"

    out_path = tmp_path / "dataset.jsonl"
    write_jsonl(entries, out_path)

    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["text"] == "gecorrigeerde offerte tekst"


def test_create_backend_uses_adapter_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter_path = "/tmp/non-existing-lora-adapter"
    monkeypatch.setenv("WHISPER_FINE_TUNED_MODEL_PATH", adapter_path)

    backend = create_backend({"backend": "mlx-whisper"})

    assert isinstance(backend, MlxWhisperBackend)
    assert backend.adapter_path == adapter_path


@pytest.mark.asyncio
async def test_mlx_backend_passes_adapter_when_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter_path = tmp_path / "adapter"
    adapter_path.mkdir(parents=True)

    captured: dict[str, object] = {}

    def _fake_transcribe(audio, **kwargs):
        captured["audio"] = audio
        captured.update(kwargs)
        return {"text": "ok"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "mlx_whisper",
        type("FakeMlx", (), {"transcribe": staticmethod(_fake_transcribe)})(),
    )

    backend = MlxWhisperBackend(adapter_path=str(adapter_path))
    out = await backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert out == "ok"
    assert captured["adapter_path"] == str(adapter_path)


@pytest.mark.asyncio
async def test_mlx_backend_ignores_invalid_adapter_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_transcribe(audio, **kwargs):
        captured["audio"] = audio
        captured.update(kwargs)
        return {"text": "ok"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "mlx_whisper",
        type("FakeMlx", (), {"transcribe": staticmethod(_fake_transcribe)})(),
    )

    backend = MlxWhisperBackend(adapter_path="/tmp/does-not-exist")
    out = await backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert out == "ok"
    assert "adapter_path" not in captured
