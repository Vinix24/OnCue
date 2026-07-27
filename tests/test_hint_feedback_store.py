from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.core import feedback_store
from sales_copilot.core.feedback_store import (
    HintFeedbackRecord,
    append_hint_feedback,
    load_hint_feedback,
)


@pytest.fixture(autouse=True)
def _patch_feedback_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every test to its own isolated feedback file."""
    target = tmp_path / "hints.ndjson"
    monkeypatch.setattr(feedback_store, "_hints_path", lambda: target)


def test_record_derives_eval_fields_from_feedback_up() -> None:
    record = HintFeedbackRecord(
        hint="Hoe ziet jullie huidige proces eruit?",
        feedback="up",
        context_utterance="We verliezen veel tijd met offertes.",
    )

    assert record.feedback == "up"
    assert record.label == "hint_positive"
    assert record.text == "We verliezen veel tijd met offertes."
    assert record.session_id


def test_record_derives_eval_fields_from_feedback_down() -> None:
    record = HintFeedbackRecord(
        hint="Wat is jullie budget?",
        feedback="down",
        context_utterances=["We willen eerst een demo.", "De huidige tool is traag."],
    )

    assert record.feedback == "down"
    assert record.label == "hint_negative"
    assert record.context_utterance == "De huidige tool is traag."
    assert record.text == record.context_utterance


def test_record_rejects_invalid_feedback() -> None:
    with pytest.raises(ValueError, match="feedback must be 'up' or 'down'"):
        HintFeedbackRecord(hint="x", feedback="sideways")


def test_append_hint_feedback_creates_file_and_writes_ndjson() -> None:
    record = HintFeedbackRecord(
        hint="Hoe ziet jullie huidige proces eruit?",
        feedback="up",
        context_utterance="We verliezen veel tijd met offertes.",
        session_id="sess-001",
    )

    path = append_hint_feedback(record)

    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["hint"] == record.hint
    assert parsed["feedback"] == "up"
    assert parsed["session_id"] == "sess-001"
    assert parsed["label"] == "hint_positive"
    assert parsed["text"] == "We verliezen veel tijd met offertes."


def test_append_hint_feedback_appends_second_record() -> None:
    append_hint_feedback(HintFeedbackRecord(hint="A", feedback="up"))
    append_hint_feedback(HintFeedbackRecord(hint="B", feedback="down"))

    path = feedback_store._hints_path()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["hint"] == "A"
    assert json.loads(lines[1])["hint"] == "B"


def test_load_hint_feedback_round_trip() -> None:
    append_hint_feedback(
        HintFeedbackRecord(hint="A", feedback="up", context_utterance="x", session_id="s1")
    )
    append_hint_feedback(
        HintFeedbackRecord(hint="B", feedback="down", context_utterance="y", session_id="s2")
    )

    records = load_hint_feedback()

    assert len(records) == 2
    assert records[0].hint == "A"
    assert records[1].hint == "B"
    assert records[1].label == "hint_negative"


def test_load_hint_feedback_limit_returns_tail() -> None:
    for i in range(5):
        append_hint_feedback(HintFeedbackRecord(hint=f"hint-{i}", feedback="up"))

    records = load_hint_feedback(limit=2)

    assert len(records) == 2
    assert records[0].hint == "hint-3"
    assert records[1].hint == "hint-4"


def test_load_hint_feedback_returns_empty_when_missing() -> None:
    assert load_hint_feedback() == []


def test_feedback_record_preserves_extra_fields() -> None:
    record = HintFeedbackRecord(
        hint="Zijn er andere beslissers?",
        feedback="up",
        context_utterance="Mijn manager moet dit goedkeuren.",
        phase="discovery",
        source="tester_dashboard",
    )

    assert record.phase == "discovery"
    assert record.source == "tester_dashboard"
    dumped = json.loads(record.model_dump_json())
    assert dumped["phase"] == "discovery"
    assert dumped["source"] == "tester_dashboard"
