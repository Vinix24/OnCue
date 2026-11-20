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


def test_merge_drops_segments_without_words() -> None:
    channels = {
        "prospect": [
            {"start": 0.0, "end": 1.0, "text": "***"},
            {"start": 1.0, "end": 2.0, "text": " * * "},
            {"start": 2.0, "end": 3.0, "text": "[BLANK_AUDIO]"},
            {"start": 3.0, "end": 4.0, "text": "[MUSIC]"},
            {"start": 4.0, "end": 5.0, "text": "(stilte)"},
            {"start": 5.0, "end": 6.0, "text": "(Music) [ Silence ]"},
            {"start": 6.0, "end": 7.0, "text": "... - !"},
            {"start": 7.0, "end": 8.0, "text": "geen tijd"},
        ],
    }
    merged = merge_labeled_segments(channels)
    assert [m["text"] for m in merged] == ["geen tijd"]


def test_merge_keeps_real_text_with_asterisk_or_marker() -> None:
    channels = {
        "self": [
            {"start": 0.0, "text": "dat kost 5 * 12 euro"},
            {"start": 1.0, "text": "het is *echt* duur"},
            {"start": 2.0, "text": "ja [MUSIC] oké"},
            {"start": 3.0, "text": "(3)"},
        ],
    }
    merged = merge_labeled_segments(channels)
    assert [m["text"] for m in merged] == [
        "dat kost 5 * 12 euro",
        "het is *echt* duur",
        "ja [MUSIC] oké",
        "(3)",
    ]


def test_transcribe_session_with_only_silence_placeholders_is_empty(tmp_path) -> None:
    (tmp_path / "prospect.wav").write_bytes(b"")
    (tmp_path / "self.wav").write_bytes(b"")

    class _Silent:
        async def transcribe_file_segments(self, path):
            return [{"start": 0.0, "end": 1.0, "text": "***"}]

    text = asyncio.run(transcribe_session(tmp_path, _Silent()))
    assert text == ""


_MARKER_SEGMENTS = [
    {"start": 5.0, "label": "self", "text": "een"},
    {"start": 70.0, "label": "prospect", "text": "twee"},
]


def test_marker_between_segments_lands_before_the_next_segment() -> None:
    out = format_transcript(_MARKER_SEGMENTS, markers=[30.0], marker_label="Markering")
    assert out.splitlines() == ["[00:05] self: een", "Markering 00:30", "[01:10] prospect: twee"]


def test_marker_at_a_segment_start_goes_before_that_segment() -> None:
    out = format_transcript(_MARKER_SEGMENTS, markers=[70.0])
    assert out.splitlines() == ["[00:05] self: een", "Marker 01:10", "[01:10] prospect: twee"]


def test_marker_after_last_segment_goes_at_the_end_in_time_order() -> None:
    out = format_transcript(_MARKER_SEGMENTS, markers=[200.0, 100.0])
    assert out.splitlines()[-2:] == ["Marker 01:40", "Marker 03:20"]
    assert len(out.splitlines()) == 4


def test_two_markers_keep_their_order_before_one_segment() -> None:
    out = format_transcript(_MARKER_SEGMENTS, markers=[40.0, 20.0])
    assert out.splitlines()[1:3] == ["Marker 00:20", "Marker 00:40"]


def test_no_markers_leaves_output_byte_for_byte_unchanged() -> None:
    expected = "[00:05] self: een\n[01:10] prospect: twee\n"
    assert format_transcript(_MARKER_SEGMENTS) == expected
    assert format_transcript(_MARKER_SEGMENTS, markers=[]) == expected
    assert format_transcript(_MARKER_SEGMENTS, markers=None) == expected


def test_markers_only_without_segments() -> None:
    assert format_transcript([], markers=[3.0]) == "Marker 00:03\n"


def test_transcribe_session_passes_markers_through(tmp_path) -> None:
    (tmp_path / "self.wav").write_bytes(b"")

    class Backend:
        async def transcribe_file_segments(self, path):
            return [{"start": 10.0, "end": 12.0, "text": "hallo"}]

    out = asyncio.run(
        transcribe_session(tmp_path, Backend(), label_map={"self": "Jij"}, markers=[4.0], marker_label="Markering")
    )
    assert out.splitlines() == ["Markering 00:04", "[00:10] Jij: hallo"]


def test_notes_render_as_timestamped_parenthesis_lines_in_time_order() -> None:
    out = format_transcript(_MARKER_SEGMENTS, markers=[30.0], notes=[(100.0, "back"), (20.0, "lost")])
    assert out.splitlines() == [
        "[00:05] self: een",
        "[00:20] (lost)",
        "Marker 00:30",
        "[01:10] prospect: twee",
        "[01:40] (back)",
    ]


def test_no_notes_leaves_output_unchanged() -> None:
    assert format_transcript(_MARKER_SEGMENTS, notes=[]) == format_transcript(_MARKER_SEGMENTS)
