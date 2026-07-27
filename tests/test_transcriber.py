from sales_copilot.modules.transcriber.engine import TranscriptEvent, map_speaker_id, swap_speaker


def test_transcript_event_to_dict() -> None:
    event = TranscriptEvent(
        type="transcript",
        text="hallo",
        speaker="self",
        start_ms=0,
        end_ms=1200,
        is_final=True,
    )

    assert event.to_dict() == {
        "type": "transcript",
        "text": "hallo",
        "speaker": "self",
        "start_ms": 0,
        "end_ms": 1200,
        "is_final": True,
    }


def test_map_speaker_id_only_accepts_known_ids() -> None:
    assert map_speaker_id(0) == "self"
    assert map_speaker_id(1) == "prospect"
    assert map_speaker_id(2) is None


def test_swap_speaker_flips_labels() -> None:
    assert swap_speaker("self") == "prospect"
    assert swap_speaker("prospect") == "self"
    assert swap_speaker(None) is None
