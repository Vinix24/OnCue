"""Tests for DiarizerProtocol, SpeakerSegment, factory — all mock-based, no HF token needed."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_speaker_segment_dataclass_defaults() -> None:
    from sales_copilot.modules.diarizer.protocol import SpeakerSegment

    seg = SpeakerSegment(speaker_id="SPEAKER_00", start_ms=0, end_ms=1000)
    assert seg.confidence == 1.0
    assert seg.speaker_id == "SPEAKER_00"
    assert seg.start_ms == 0
    assert seg.end_ms == 1000


def test_diarizer_protocol_factory_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIARIZATION_ENABLED", "0")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_realtoken_notplaceholder")

    from sales_copilot.modules.diarizer.factory import get_diarizer

    assert get_diarizer() is None


def test_diarizer_protocol_factory_returns_none_when_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIARIZATION_ENABLED", "1")
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)

    from sales_copilot.modules.diarizer.factory import get_diarizer

    assert get_diarizer() is None


def test_diarizer_protocol_factory_returns_none_when_token_is_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIARIZATION_ENABLED", "1")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_PLACEHOLDER")

    from sales_copilot.modules.diarizer.factory import get_diarizer

    assert get_diarizer() is None


def test_diarizer_protocol_factory_returns_pyannote_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIARIZATION_ENABLED", "1")
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_real_token_abc123")
    monkeypatch.setenv("DIARIZATION_MIN_SPEAKERS", "2")
    monkeypatch.setenv("DIARIZATION_MAX_SPEAKERS", "8")

    from sales_copilot.modules.diarizer.factory import get_diarizer
    from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer

    result = get_diarizer()
    assert isinstance(result, PyannoteV3Diarizer)
    assert result.hf_token == "hf_real_token_abc123"
    assert result.min_speakers == 2
    assert result.max_speakers == 8


@pytest.mark.asyncio
async def test_pyannote_diarizer_fails_gracefully_on_invalid_audio() -> None:
    """Pipeline raises on bad audio — diarize_chunk should return [] without re-raising."""
    from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer

    mock_pipeline = MagicMock(side_effect=ValueError("invalid audio format"))
    diarizer = PyannoteV3Diarizer(hf_token="hf_fake", min_speakers=1, max_speakers=2)
    diarizer._pipeline = mock_pipeline  # noqa: SLF001

    result = await diarizer.diarize_chunk(b"\x00" * 64, 16000)
    assert result == []


@pytest.mark.asyncio
async def test_pyannote_diarizer_times_out_to_empty() -> None:
    """A wedged diarize call is bounded by timeout_s and degrades to []."""
    import asyncio
    import time

    from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer

    diarizer = PyannoteV3Diarizer(
        hf_token="hf_fake", min_speakers=1, max_speakers=2, timeout_s=0.05
    )
    diarizer._pipeline = object()  # noqa: SLF001 — non-None so diarize_chunk proceeds

    def _slow(_audio_chunk: bytes, _sample_rate: int) -> list:
        time.sleep(0.3)
        return []

    diarizer._diarize_sync = _slow  # noqa: SLF001 — instance attr shadows the method

    result = await asyncio.wait_for(diarizer.diarize_chunk(b"\x00" * 64, 16000), timeout=2.0)
    assert result == []


@pytest.mark.asyncio
async def test_pyannote_diarizer_returns_empty_on_auth_fail() -> None:
    """When load() was never called (pipeline is None), diarize_chunk returns []."""
    from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer

    diarizer = PyannoteV3Diarizer(hf_token="hf_invalid_token", min_speakers=1, max_speakers=2)
    # _pipeline is None because load() was never called (simulates auth failure)
    assert diarizer._pipeline is None  # noqa: SLF001

    result = await diarizer.diarize_chunk(b"\x00" * 64, 16000)
    assert result == []
