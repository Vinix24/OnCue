"""The demo replay picks the bundled sample that matches the call language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.sample_aha.player import default_sample_path, sample_path_for_language


@pytest.mark.parametrize(
    ("language", "filename"),
    [
        ("en", "sample-aha_en.json"),
        ("nl", "sample-aha.json"),
        ("NL", "sample-aha.json"),
        (" nl ", "sample-aha.json"),
        ("de", "sample-aha_en.json"),
        ("", "sample-aha_en.json"),
    ],
)
def test_sample_path_for_language(language: str, filename: str) -> None:
    assert Path(sample_path_for_language(language)).name == filename


@pytest.mark.parametrize(
    ("call_language", "filename"),
    [
        (None, "sample-aha_en.json"),
        ("en", "sample-aha_en.json"),
        ("nl", "sample-aha.json"),
        ("fr", "sample-aha_en.json"),
    ],
)
def test_default_sample_path_follows_call_language(
    monkeypatch: pytest.MonkeyPatch, call_language: str | None, filename: str
) -> None:
    monkeypatch.delenv("SAMPLE_AHA_PATH", raising=False)
    if call_language is None:
        monkeypatch.delenv("CALL_LANGUAGE", raising=False)
    else:
        monkeypatch.setenv("CALL_LANGUAGE", call_language)
    path = Path(default_sample_path())
    assert path.name == filename
    assert path.exists()


def test_explicit_sample_path_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "en")
    monkeypatch.setenv("SAMPLE_AHA_PATH", "/somewhere/custom.json")
    assert default_sample_path() == "/somewhere/custom.json"


@pytest.mark.parametrize("language", ["en", "nl"])
def test_bundled_samples_have_matching_structure(language: str) -> None:
    sample = json.loads(Path(sample_path_for_language(language)).read_text(encoding="utf-8"))
    events = sample["events"]
    assert len(events) == 18
    assert {e["speaker"] for e in events} == {"self", "prospect"}
    assert len(sample["suggestions"]) == 3
    assert all(s["after_event_index"] < len(events) for s in sample["suggestions"])
