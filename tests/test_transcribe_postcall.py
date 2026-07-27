from __future__ import annotations

import asyncio

from sales_copilot.modules.transcriber.postcall import (
    format_transcript,
    merge_labeled_segments,
    transcribe_session,
)


def test_merge_interleaves_channels_by_start_time() -> None:
    channels = {
        "prospect": [
            {"start": 0.0, "end": 2.0, "text": "geen tijd"},
            {"start": 10.0, "end": 12.0, "text": "te duur"},
        ],
        "self": [
            {"start": 4.0, "end": 6.0, "text": "ik snap dat"},
        ],
    }
    merged = merge_labeled_segments(channels)

    assert [m["start"] for m in merged] == [0.0, 4.0, 10.0]
    assert [m["label"] for m in merged] == ["prospect", "self", "prospect"]
    assert merged[1]["text"] == "ik snap dat"


def test_merge_drops_empty_segments() -> None:
    channels = {"self": [{"start": 1.0, "text": "  "}, {"start": 2.0, "text": "hallo"}]}
    merged = merge_labeled_segments(channels)
    assert len(merged) == 1
    assert merged[0]["text"] == "hallo"


def test_format_transcript_labels_and_timestamps() -> None:
    merged = [
        {"start": 0.0, "label": "prospect", "text": "geen tijd"},
        {"start": 65.0, "label": "self", "text": "ik begrijp dat"},
    ]
    out = format_transcript(merged, {"prospect": "PROSPECT", "self": "JIJ"})
    lines = out.strip().splitlines()

    assert lines[0] == "[00:00] PROSPECT: geen tijd"
    assert lines[1] == "[01:05] JIJ: ik begrijp dat"


def test_format_transcript_empty_is_empty_string() -> None:
    assert format_transcript([]) == ""


def test_transcribe_session_reads_wavs_and_merges(tmp_path) -> None:
    # Two "recorder" WAVs (content irrelevant; the fake backend keys off the stem).
    (tmp_path / "prospect.wav").write_bytes(b"RIFF")
    (tmp_path / "self.wav").write_bytes(b"RIFF")

    class FakeBackend:
        async def transcribe_file_segments(self, path):
            if path.stem == "prospect":
                return [{"start": 0.0, "end": 2.0, "text": "wij hebben al een partij"}]
            return [{"start": 3.0, "end": 5.0, "text": "mooi dat je dat al geregeld hebt"}]

    transcript = asyncio.run(transcribe_session(tmp_path, FakeBackend()))
    lines = transcript.strip().splitlines()

    assert lines[0] == "[00:00] PROSPECT: wij hebben al een partij"
    assert lines[1] == "[00:03] JIJ: mooi dat je dat al geregeld hebt"
