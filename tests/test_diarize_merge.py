"""Unit tests for the pure merge logic in diarize_merge.

All tests operate on synthetic segments and turns — no models, no audio,
no network access.  Tests that would require pyannote/torch are marked
with pytest.mark.skip so CI collection stays cheap.
"""

from __future__ import annotations

import pytest

from sales_copilot.modules.reports.diarize_merge import (
    DiarizationTurn,
    LabeledBlock,
    TranscriptSegment,
    merge_diarization,
    render_labeled,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def _turn(start: float, end: float, speaker: str) -> tuple:
    return (start, end, speaker)


# ---------------------------------------------------------------------------
# Single speaker
# ---------------------------------------------------------------------------


def test_single_speaker_produces_one_block() -> None:
    segs = [_seg(0.0, 2.0, "Hello world"), _seg(2.0, 4.0, "How are you")]
    turns = [_turn(0.0, 5.0, "SPEAKER_00")]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].speaker == "SPEAKER_00"
    assert blocks[0].text == "Hello world How are you"


def test_single_speaker_start_end_preserved() -> None:
    segs = [_seg(1.0, 3.0, "First"), _seg(3.0, 5.5, "Second")]
    turns = [_turn(0.5, 6.0, "SPEAKER_00")]
    blocks = merge_diarization(segs, turns)
    assert blocks[0].start == pytest.approx(1.0)
    assert blocks[0].end == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# Two speakers alternating
# ---------------------------------------------------------------------------


def test_two_speakers_alternating_produces_correct_blocks() -> None:
    segs = [
        _seg(0.0, 2.0, "Good morning"),
        _seg(2.5, 4.5, "Hi there"),
        _seg(5.0, 7.0, "We should talk"),
        _seg(7.5, 9.5, "Agreed"),
    ]
    turns = [
        _turn(0.0, 2.2, "SPEAKER_00"),
        _turn(2.3, 4.7, "SPEAKER_01"),
        _turn(4.9, 7.2, "SPEAKER_00"),
        _turn(7.3, 9.8, "SPEAKER_01"),
    ]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 4
    assert [b.speaker for b in blocks] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
        "SPEAKER_01",
    ]


def test_two_speakers_texts_correct() -> None:
    segs = [_seg(0.0, 1.0, "Alpha"), _seg(2.0, 3.0, "Beta")]
    turns = [_turn(0.0, 1.5, "SPEAKER_00"), _turn(1.5, 3.5, "SPEAKER_01")]
    blocks = merge_diarization(segs, turns)
    assert blocks[0].text == "Alpha"
    assert blocks[1].text == "Beta"


# ---------------------------------------------------------------------------
# Overlapping turns — max overlap wins
# ---------------------------------------------------------------------------


def test_max_overlap_wins_for_segment_straddling_two_turns() -> None:
    # Segment [1.0, 3.0] straddles SPEAKER_00 [0.0, 1.8] and SPEAKER_01 [1.8, 4.0]
    # Overlap with 00: 1.0-1.8 = 0.8 s
    # Overlap with 01: 1.8-3.0 = 1.2 s  -> 01 wins
    segs = [_seg(1.0, 3.0, "Straddled text")]
    turns = [_turn(0.0, 1.8, "SPEAKER_00"), _turn(1.8, 4.0, "SPEAKER_01")]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].speaker == "SPEAKER_01"


def test_max_overlap_wins_when_one_turn_dominates_clearly() -> None:
    # Segment [0.5, 4.0], SPEAKER_00 covers [0.0, 1.0] (0.5s overlap),
    # SPEAKER_01 covers [1.0, 5.0] (3.0s overlap)
    segs = [_seg(0.5, 4.0, "Long statement")]
    turns = [_turn(0.0, 1.0, "SPEAKER_00"), _turn(1.0, 5.0, "SPEAKER_01")]
    blocks = merge_diarization(segs, turns)
    assert blocks[0].speaker == "SPEAKER_01"


# ---------------------------------------------------------------------------
# Segment with no overlapping turn — unknown fallback
# ---------------------------------------------------------------------------


def test_no_overlapping_turn_returns_default_fallback() -> None:
    segs = [_seg(10.0, 12.0, "Nobody talking at this point")]
    turns = [_turn(0.0, 5.0, "SPEAKER_00")]  # ends before segment starts
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].speaker == "SPEAKER_?"


def test_no_overlapping_turn_custom_fallback() -> None:
    segs = [_seg(20.0, 22.0, "Gap speech")]
    turns = [_turn(0.0, 5.0, "SPEAKER_00")]
    blocks = merge_diarization(segs, turns, unknown_fallback="UNKNOWN")
    assert blocks[0].speaker == "UNKNOWN"


def test_empty_turns_list_assigns_all_to_fallback() -> None:
    segs = [_seg(0.0, 1.0, "First"), _seg(1.0, 2.0, "Second")]
    blocks = merge_diarization(segs, [])
    assert all(b.speaker == "SPEAKER_?" for b in blocks)


# ---------------------------------------------------------------------------
# Consecutive same-speaker grouping
# ---------------------------------------------------------------------------


def test_consecutive_same_speaker_segments_are_merged() -> None:
    segs = [
        _seg(0.0, 1.0, "Part one"),
        _seg(1.0, 2.0, "Part two"),
        _seg(2.0, 3.0, "Part three"),
    ]
    turns = [_turn(0.0, 5.0, "SPEAKER_00")]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].text == "Part one Part two Part three"


def test_non_consecutive_same_speaker_produces_separate_blocks() -> None:
    # A-B-A pattern: SPEAKER_00, then SPEAKER_01, then SPEAKER_00 again
    segs = [
        _seg(0.0, 1.0, "A1"),
        _seg(2.0, 3.0, "B"),
        _seg(4.0, 5.0, "A2"),
    ]
    turns = [
        _turn(0.0, 1.5, "SPEAKER_00"),
        _turn(1.5, 3.5, "SPEAKER_01"),
        _turn(3.5, 5.5, "SPEAKER_00"),
    ]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 3
    assert [b.speaker for b in blocks] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert [b.text for b in blocks] == ["A1", "B", "A2"]


# ---------------------------------------------------------------------------
# Empty and edge cases
# ---------------------------------------------------------------------------


def test_empty_segments_returns_empty_list() -> None:
    assert merge_diarization([], [_turn(0.0, 5.0, "SPEAKER_00")]) == []


def test_empty_text_segments_are_skipped() -> None:
    segs = [_seg(0.0, 1.0, ""), _seg(1.0, 2.0, "  "), _seg(2.0, 3.0, "Real text")]
    turns = [_turn(0.0, 5.0, "SPEAKER_00")]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].text == "Real text"


# ---------------------------------------------------------------------------
# Dataclass input (TypedInput path)
# ---------------------------------------------------------------------------


def test_typed_input_objects_accepted() -> None:
    segs = [TranscriptSegment(start=0.0, end=2.0, text="Typed input")]
    turns = [DiarizationTurn(start=0.0, end=3.0, speaker="SPEAKER_01")]
    blocks = merge_diarization(segs, turns)
    assert len(blocks) == 1
    assert blocks[0].speaker == "SPEAKER_01"
    assert blocks[0].text == "Typed input"


# ---------------------------------------------------------------------------
# Speaker label normalisation
# ---------------------------------------------------------------------------


def test_speaker_label_normalised_to_two_digit_format() -> None:
    segs = [_seg(0.0, 1.0, "Hello")]
    turns = [_turn(0.0, 1.5, "SPEAKER_0")]  # single digit
    blocks = merge_diarization(segs, turns)
    assert blocks[0].speaker == "SPEAKER_00"


def test_dutch_spreker_label_normalised() -> None:
    # SRC script used SPREKER_00; we normalise raw pyannote output only —
    # if the raw label doesn't end in _<digits> the label passes through as-is.
    segs = [_seg(0.0, 1.0, "Dutch speaker")]
    turns = [_turn(0.0, 1.5, "SPREKER_00")]
    blocks = merge_diarization(segs, turns)
    # Ends in _00 (digits) so normalised to SPEAKER_00
    assert blocks[0].speaker == "SPEAKER_00"


# ---------------------------------------------------------------------------
# render_labeled
# ---------------------------------------------------------------------------


def test_render_labeled_formats_correctly() -> None:
    blocks = [
        LabeledBlock(speaker="SPEAKER_00", text="Hello there", start=0.0, end=2.0),
        LabeledBlock(speaker="SPEAKER_01", text="Good to meet you", start=2.5, end=5.0),
    ]
    result = render_labeled(blocks)
    assert result == "SPEAKER_00: Hello there\nSPEAKER_01: Good to meet you"


def test_render_labeled_empty_list_returns_empty_string() -> None:
    assert render_labeled([]) == ""


# ---------------------------------------------------------------------------
# Tests that would require pyannote/torch — skipped in CI
# ---------------------------------------------------------------------------


def test_diarize_wav_glue_runs_offline_with_mocked_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Exercise the diarize_wav pyannote-API glue without a model download.

    Patches ``Pipeline.from_pretrained`` so no HuggingFace token or network is
    needed; this gives CI coverage of the device selection, the 3.x/4.x
    from_pretrained fallback, the annotation unwrap, and the
    itertracks -> DiarizationTurn mapping that the two skipped integration tests
    below would otherwise be the only coverage for.

    pyannote is the optional [diarization] extra (not in the CI [full,dev] profile),
    so skip cleanly when it is not installed.
    """
    pytest.importorskip("pyannote.audio")
    import pyannote.audio

    from sales_copilot.modules.reports.diarize_merge import DiarizationTurn, diarize_wav

    class _FakeTurn:
        def __init__(self, start: float, end: float) -> None:
            self.start = start
            self.end = end

    class _FakeAnnotation:
        def itertracks(self, yield_label: bool = False):
            yield _FakeTurn(0.0, 1.5), None, "SPEAKER_00"
            yield _FakeTurn(1.5, 3.0), None, "SPEAKER_01"

    class _FakePipeline:
        def to(self, _device: object) -> _FakePipeline:
            return self

        def __call__(self, _wav: str, **_kwargs: object) -> _FakeAnnotation:
            return _FakeAnnotation()

    def _fake_from_pretrained(model_id: str, **_kwargs: object) -> _FakePipeline:
        return _FakePipeline()

    monkeypatch.setattr(pyannote.audio.Pipeline, "from_pretrained", _fake_from_pretrained)

    turns = diarize_wav(tmp_path / "anything.wav", hf_token="fake", num_speakers=2)

    assert turns == [
        DiarizationTurn(start=0.0, end=1.5, speaker="SPEAKER_00"),
        DiarizationTurn(start=1.5, end=3.0, speaker="SPEAKER_01"),
    ]


@pytest.mark.skip(reason="Requires pyannote + HuggingFace token — not run in CI")
def test_diarize_wav_returns_turns() -> None:  # pragma: no cover
    from pathlib import Path

    from sales_copilot.modules.reports.diarize_merge import diarize_wav

    turns = diarize_wav(Path("tests/fixtures/sample.wav"))
    assert len(turns) > 0


@pytest.mark.skip(reason="Requires pyannote community model + HuggingFace token — not run in CI")
def test_diarize_wav_community_model() -> None:  # pragma: no cover
    from pathlib import Path

    from sales_copilot.modules.reports.diarize_merge import diarize_wav

    turns = diarize_wav(
        Path("tests/fixtures/sample.wav"),
        model_id="pyannote/speaker-diarization-community-1",
    )
    assert len(turns) > 0
