from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from sales_copilot.modules.reports.session import SessionData


def _make_session(transcript: list[dict] | None = None) -> SessionData:
    return SessionData(
        session_id="test-session",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:01:00+00:00",
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        transcript=list(transcript or []),
        pain_points=[],
        talk_time_snapshots=[],
        phase_transitions=[],
        coaching_alerts=[],
        summaries=[],
    )


def _write_wav(path: Path, duration_seconds: float = 2.0, sample_rate: int = 16000) -> None:
    n_samples = int(duration_seconds * sample_rate)
    silence = np.zeros(n_samples, dtype=np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(silence.tobytes())


async def test_post_call_batch_runs_when_no_self_transcripts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sales_copilot.modules.reports import __main__ as rm

    self_wav = tmp_path / "self.wav"
    _write_wav(self_wav, duration_seconds=2.0)

    transcribe_calls: list[int] = []

    class _FakeBackend:
        async def warmup(self) -> None:
            pass

        async def transcribe(self, audio: np.ndarray) -> str:
            transcribe_calls.append(len(audio))
            return "dit is een test"

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(rm, "create_backend", lambda _cfg: _FakeBackend())

    session = _make_session(
        transcript=[{"speaker": "prospect", "text": "Hoi", "start_ms": 0, "end_ms": 500}]
    )
    result = await rm._run_self_batch_if_needed(session, tmp_path)

    assert len(transcribe_calls) > 0
    self_entries = [t for t in result.transcript if t.get("speaker") == "self"]
    assert len(self_entries) > 0
    assert self_entries[0]["text"] == "dit is een test"


async def test_post_call_batch_skipped_when_self_already_in_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sales_copilot.modules.reports import __main__ as rm

    self_wav = tmp_path / "self.wav"
    _write_wav(self_wav, duration_seconds=2.0)

    transcribe_calls: list[bool] = []

    class _FakeBackend:
        async def warmup(self) -> None:
            pass

        async def transcribe(self, audio: np.ndarray) -> str:
            transcribe_calls.append(True)
            return "test"

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(rm, "create_backend", lambda _cfg: _FakeBackend())

    session = _make_session(
        transcript=[{"speaker": "self", "text": "al aanwezig", "start_ms": 0, "end_ms": 500}]
    )
    result = await rm._run_self_batch_if_needed(session, tmp_path)

    assert len(transcribe_calls) == 0
    assert result is session


async def test_post_call_batch_skipped_when_no_wav_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sales_copilot.modules.reports import __main__ as rm

    # No self.wav created in tmp_path
    assert not (tmp_path / "self.wav").exists()

    transcribe_calls: list[bool] = []

    class _FakeBackend:
        async def warmup(self) -> None:
            pass

        async def transcribe(self, audio: np.ndarray) -> str:
            transcribe_calls.append(True)
            return "test"

        async def stop(self) -> None:
            pass

    monkeypatch.setattr(rm, "create_backend", lambda _cfg: _FakeBackend())

    session = _make_session(transcript=[])
    result = await rm._run_self_batch_if_needed(session, tmp_path)

    assert len(transcribe_calls) == 0
    assert result is session
