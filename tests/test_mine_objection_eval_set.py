from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.modules.detector.eval_mining import (
    classify_records,
    load_jsonl_records,
    load_records,
    seed_records,
    utterances_from_jsonl,
    utterances_from_markdown,
)

SAMPLE_MARKDOWN = """\
**Vincent van Deth** *[0:12]*: goedemorgen, hoe gaat het?
**Prospect** *[0:15]*: prima, dank je
**Vincent van Deth** *[0:18]*: mijn product kost vijfhonderd euro per maand
**Prospect** *[0:22]*: dat is te duur voor ons
**Prospect** *[0:25]*: we hebben dit jaar al een ander systeem gekozen
"""

NAMED_SPEAKER_MARKDOWN = """\
**Vincent van Deth** *[0:05]*: goedemorgen, hoe gaat het met jullie?
**Tom Bakker** *[0:12]*: goed, maar het budget is dit kwartaal krap
**Vincent van Deth** *[0:20]*: dat begrijp ik, laten we kijken naar de opties
**Sander Mulder | Voorbeeld Techniek** *[0:30]*: wij twijfelen nog over de implementatietijd
"""


def test_utterances_from_markdown_extracts_prospect_lines(tmp_path: Path) -> None:
    md_path = tmp_path / "call.md"
    md_path.write_text(SAMPLE_MARKDOWN, encoding="utf-8")
    utterances = utterances_from_markdown(md_path)

    texts = [u["text"] for u in utterances]
    assert "goedemorgen, hoe gaat het?" not in texts
    assert "dat is te duur voor ons" in texts
    assert "we hebben dit jaar al een ander systeem gekozen" in texts
    assert all(u["source"] == "call" for u in utterances)


def test_utterances_from_markdown_skips_short_fillers(tmp_path: Path) -> None:
    md_path = tmp_path / "call.md"
    md_path.write_text("**Prospect** *[0:15]*: oke\n**Prospect** *[0:16]*: ja", encoding="utf-8")
    utterances = utterances_from_markdown(md_path)
    assert utterances == []


def test_utterances_from_markdown_real_names_default_excludes_rep(tmp_path: Path) -> None:
    md_path = tmp_path / "real-call.md"
    md_path.write_text(NAMED_SPEAKER_MARKDOWN, encoding="utf-8")
    utterances = utterances_from_markdown(md_path)

    texts = [u["text"] for u in utterances]
    assert "goedemorgen, hoe gaat het met jullie?" not in texts
    assert "dat begrijp ik, laten we kijken naar de opties" not in texts
    assert "goed, maar het budget is dit kwartaal krap" in texts
    assert "wij twijfelen nog over de implementatietijd" in texts
    assert all(u["source"] == "real-call" for u in utterances)


def test_utterances_from_markdown_custom_exclude_speakers(tmp_path: Path) -> None:
    md_path = tmp_path / "real-call.md"
    md_path.write_text(NAMED_SPEAKER_MARKDOWN, encoding="utf-8")
    utterances = utterances_from_markdown(md_path, exclude_speakers=["Tom Bakker"])

    texts = [u["text"] for u in utterances]
    assert "goed, maar het budget is dit kwartaal krap" not in texts
    assert "goedemorgen, hoe gaat het met jullie?" in texts


def test_utterances_from_markdown_prospect_name_filters_to_single_speaker(tmp_path: Path) -> None:
    md_path = tmp_path / "real-call.md"
    md_path.write_text(NAMED_SPEAKER_MARKDOWN, encoding="utf-8")
    utterances = utterances_from_markdown(md_path, prospect_name="Sander Mulder")

    texts = [u["text"] for u in utterances]
    assert texts == ["wij twijfelen nog over de implementatietijd"]


def test_utterances_from_jsonl(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "call.jsonl"
    jsonl_path.write_text(
        json.dumps({"text": "dat is te duur", "speaker": "prospect"}) + "\n"
        + json.dumps({"text": "oke", "speaker": "prospect"}) + "\n",
        encoding="utf-8",
    )
    utterances = utterances_from_jsonl(jsonl_path)
    assert len(utterances) == 1
    assert utterances[0]["text"] == "dat is te duur"


def test_load_records_raises_on_unknown_extension(tmp_path: Path) -> None:
    bad_path = tmp_path / "call.txt"
    bad_path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported transcript format"):
        load_records(bad_path)


def test_seed_records_contains_objections_and_negatives() -> None:
    records = seed_records()
    labels = {r["label"] for r in records}
    assert "prijs" in labels
    assert "timing" in labels
    assert "negative" in labels
    assert "kans" in labels
    assert any(r["source"] == "seed" for r in records)


def test_classify_records_uses_mock_router() -> None:
    class _FakeMatch:
        def __init__(self, category: str, confidence: float) -> None:
            self.category = category
            self.confidence = confidence

    class FakeRouter:
        def classify(self, text: str) -> _FakeMatch | None:
            if "duur" in text:
                return _FakeMatch("prijs", 0.88)
            return None

    records = [
        {"text": "het is te duur", "label": "review", "source": "test"},
        {"text": "goedemorgen", "label": "review", "source": "test"},
    ]
    enriched = classify_records(records, FakeRouter())

    assert enriched[0]["predicted_category"] == "prijs"
    assert enriched[0]["predicted_confidence"] == pytest.approx(0.88)
    assert enriched[1]["predicted_category"] is None
    assert enriched[1]["predicted_confidence"] == 0.0


def test_load_jsonl_records_helper(tmp_path: Path) -> None:
    path = tmp_path / "eval.jsonl"
    path.write_text(
        json.dumps({"text": "a", "label": "prijs"}) + "\n"
        + json.dumps({"text": "b", "label": "negative"}) + "\n",
        encoding="utf-8",
    )
    records = load_jsonl_records(path)
    assert len(records) == 2
    assert records[1]["label"] == "negative"
