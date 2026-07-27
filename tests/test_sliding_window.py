from sales_copilot.modules.detector.sliding_window import SlidingWindowBuffer, TranscriptChunk


def _chunk(text: str, start_ms: int = 0, end_ms: int = 100) -> TranscriptChunk:
    return TranscriptChunk(text=text, speaker="prospect", start_ms=start_ms, end_ms=end_ms)


def test_window_adds_and_prunes_at_max_chunks() -> None:
    buf = SlidingWindowBuffer(max_chunks=3)
    for i in range(5):
        buf.add(_chunk(f"chunk {i}"))

    assert len(buf) == 3
    assert buf.context_text() == "chunk 2 chunk 3 chunk 4"


def test_context_text_concatenates_with_single_space() -> None:
    buf = SlidingWindowBuffer(max_chunks=5)
    buf.add(_chunk("hello"))
    buf.add(_chunk("world"))
    buf.add(_chunk("foo"))

    assert buf.context_text() == "hello world foo"


def test_latest_chunk_returns_last_added() -> None:
    buf = SlidingWindowBuffer(max_chunks=5)
    buf.add(_chunk("first"))
    last = _chunk("last", start_ms=500, end_ms=1000)
    buf.add(last)

    assert buf.latest_chunk() is last


def test_empty_window_context_text_returns_empty_string() -> None:
    buf = SlidingWindowBuffer(max_chunks=5)

    assert buf.context_text() == ""
    assert buf.latest_chunk() is None


def test_len_returns_chunk_count() -> None:
    buf = SlidingWindowBuffer(max_chunks=10)
    assert len(buf) == 0
    buf.add(_chunk("a"))
    buf.add(_chunk("b"))
    assert len(buf) == 2


def test_chunks_with_whitespace_only_text_excluded_from_context() -> None:
    buf = SlidingWindowBuffer(max_chunks=5)
    buf.add(_chunk("hello"))
    buf.add(_chunk("   "))
    buf.add(_chunk("world"))

    assert buf.context_text() == "hello world"
