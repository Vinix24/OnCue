import asyncio
import json

import numpy as np
import pytest

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.backends.mlx_backend import MlxWhisperBackend
from sales_copilot.modules.transcriber.engine import TranscriptEvent
from sales_copilot.modules.transcriber.whisper_direct import DirectWhisperEngine


class _FakeAudioStream:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self._frames = list(frames)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> np.ndarray | None:
        if self._frames:
            return self._frames.pop(0)
        return None


class _FakeWs:
    def __init__(self, stop_event: asyncio.Event) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._stop_event = stop_event

    async def send(self, data: str) -> None:
        self.sent.append(data)
        self._stop_event.set()

    async def close(self) -> None:
        self.closed = True


class _FinalTranscriptWs:
    """Records all events; sets stop_event only when a final transcript arrives."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._stop_event = stop_event

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if json.loads(data).get("type") == "transcript":
            self._stop_event.set()

    async def close(self) -> None:
        self.closed = True


class _FailingWs(_FakeWs):
    def __init__(self) -> None:
        super().__init__(asyncio.Event())
        self._failed = False

    async def send(self, data: str) -> None:
        if not self._failed:
            self._failed = True
            self.closed = True
            raise RuntimeError("send failed")
        await super().send(data)


class _FakeBackend:
    def __init__(self, text: str = "zelfde tekst") -> None:
        self._text = text
        self.started = False
        self.stopped = False

    async def start(self, _stop_event: asyncio.Event) -> None:
        self.started = True

    async def transcribe(self, _audio: np.ndarray) -> str:
        return self._text

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_direct_engine_publishes_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    stop_event = asyncio.Event()
    frames = np.ones((1600, 1), dtype=np.float32)
    audio_stream = _FakeAudioStream([frames, None, frames, None])
    # Stop only on final transcript so partial events don't terminate the loop early
    ws = _FinalTranscriptWs(stop_event)

    async def _connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    monkeypatch.setattr(DirectWhisperEngine, "_is_speech", lambda self, chunk: bool(np.mean(chunk) > 0))
    backend = _FakeBackend(text="zelfde tekst")

    engine = DirectWhisperEngine(
        audio_stream=audio_stream,
        ws_config=WebSocketConfig(),
        speaker="self",
        backend=backend,
        silence_gap_seconds=0.0,
    )
    await engine.run(stop_event)

    assert backend.started is True
    assert backend.stopped is True
    assert audio_stream.started is True
    assert audio_stream.stopped is True
    messages = [json.loads(m) for m in ws.sent]
    final_events = [m for m in messages if m["type"] == "transcript"]
    assert len(final_events) == 1, f"Expected 1 final (dedup), got {len(final_events)}"
    assert final_events[0]["text"] == "zelfde tekst"
    assert final_events[0]["speaker"] == "self"


@pytest.mark.asyncio
async def test_mlx_backend_uses_antihallucination_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_transcribe(audio, **kwargs):
        captured["audio"] = audio
        captured.update(kwargs)
        return {"text": "resultaat"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "mlx_whisper",
        type("FakeMlx", (), {"transcribe": staticmethod(_fake_transcribe)})(),
    )
    backend = MlxWhisperBackend()
    out = await backend.transcribe(np.zeros(16000, dtype=np.float32))

    assert out == "resultaat"
    assert captured["path_or_hf_repo"] == "mlx-community/whisper-large-v3-turbo"
    assert captured["language"] == "nl"
    assert captured["condition_on_previous_text"] is False
    assert captured["compression_ratio_threshold"] == 2.4
    assert captured["no_speech_threshold"] == 0.6


@pytest.mark.asyncio
async def test_run_preconnects_before_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    connect_calls = 0

    async def _connect(*_args, **_kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return _FakeWs(asyncio.Event())

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    backend = _FakeBackend()

    stream = _FakeAudioStream([])
    stop_event = asyncio.Event()
    stop_event.set()
    engine = DirectWhisperEngine(
        audio_stream=stream,
        ws_config=WebSocketConfig(),
        speaker="self",
        backend=backend,
    )

    await engine.run(stop_event)

    assert connect_calls == 1
    assert stream.started is True
    assert stream.stopped is True


@pytest.mark.asyncio
async def test_publish_reconnects_after_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _FailingWs()
    second = _FakeWs(asyncio.Event())
    sockets = [first, second]

    async def _connect(*_args, **_kwargs):
        return sockets.pop(0)

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    backend = _FakeBackend()

    engine = DirectWhisperEngine(
        audio_stream=_FakeAudioStream([]),
        ws_config=WebSocketConfig(),
        speaker="self",
        backend=backend,
    )
    await engine._publish(
        TranscriptEvent(
            type="transcript",
            text="hallo",
            speaker="self",
            start_ms=0,
            end_ms=100,
            is_final=True,
        )
    )  # noqa: SLF001

    assert len(second.sent) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("***", True),
        ("...", True),
        ("*", True),
        ("   ", True),
        ("Ondertitels ingericht door", True),
        ("MBC뉴스", True),
        ("ご視聴ありがとうございました", True),
        ("Test, test, test.", False),
        ("Mijn naam is Bart", False),
    ],
)
def test_is_hallucination_filters_known_patterns(text: str, expected: bool) -> None:
    assert DirectWhisperEngine._is_hallucination(text) is expected  # noqa: SLF001
