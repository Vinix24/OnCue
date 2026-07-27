import numpy as np

from sales_copilot.modules.transcriber.chunker import AudioChunker


def _frames(samples: int) -> np.ndarray:
    return np.ones((samples, 1), dtype=np.float32)


def test_chunker_flushes_on_target_duration() -> None:
    chunker = AudioChunker(sample_rate=16000, chunk_ms=3000, min_chunk_ms=1200, max_chunk_ms=6000, silence_flush_ms=400)

    ready = []
    ready.extend(chunker.add_frames(_frames(16000), is_speech=True))
    ready.extend(chunker.add_frames(_frames(16000), is_speech=True))
    ready.extend(chunker.add_frames(_frames(16000), is_speech=True))

    assert len(ready) == 1
    assert ready[0].start_ms == 0
    assert ready[0].end_ms == 3000
    assert ready[0].audio.shape[0] == 48000


def test_chunker_flushes_after_silence_once_min_duration_met() -> None:
    chunker = AudioChunker(sample_rate=16000, chunk_ms=3000, min_chunk_ms=1200, max_chunk_ms=6000, silence_flush_ms=400)

    ready = []
    ready.extend(chunker.add_frames(_frames(16000), is_speech=True))
    ready.extend(chunker.add_frames(_frames(4800), is_speech=True))
    ready.extend(chunker.add_frames(_frames(8000), is_speech=False))

    assert len(ready) == 1
    assert ready[0].start_ms == 0
    assert ready[0].end_ms == 1300
    assert ready[0].audio.shape[0] == 20800


def test_chunker_requires_min_duration_before_flush() -> None:
    chunker = AudioChunker(sample_rate=16000, chunk_ms=3000, min_chunk_ms=1200, max_chunk_ms=6000, silence_flush_ms=400)

    ready = []
    ready.extend(chunker.add_frames(_frames(8000), is_speech=True))
    ready.extend(chunker.add_frames(_frames(8000), is_speech=False))

    assert ready == []
    assert chunker.buffer_duration_ms == 500
