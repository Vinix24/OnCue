"""Tap warnings, transcript markers and bar labels follow LANGUAGE (English unless nl)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sales_copilot.audio import tap_health
from sales_copilot.audio.tap_health import TapHealthTracker, signal_lost_marker
from sales_copilot.core import i18n

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGS = REPO_ROOT / "config" / "i18n"


@pytest.fixture(autouse=True)
def _real_catalogs(monkeypatch):
    i18n.set_catalog_dir(CATALOGS)
    yield
    i18n.set_catalog_dir(None)


def _section(lang: str, name: str) -> dict:
    return yaml.safe_load((CATALOGS / f"{lang}.yaml").read_text(encoding="utf-8"))[name]


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", text))


def test_audio_and_bar_keys_exist_in_both_catalogs_with_the_same_placeholders() -> None:
    for name, keys in (("audio", None), ("recorder", "bar_")):
        en, nl = _section("en", name), _section("nl", name)
        wanted = [k for k in en if keys is None or k.startswith(keys)]
        assert wanted
        for key in wanted:
            assert key in nl, f"{name}.{key} missing in nl"
            assert _placeholders(en[key]) == _placeholders(nl[key]), f"{name}.{key}"
    assert set(_section("en", "audio")) == set(_section("nl", "audio"))


def _lost_health():
    clock = [0.0]
    tracker = TapHealthTracker(clock=lambda: clock[0], signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    import numpy as np

    tracker.observe(np.full(64, 0.3, dtype=np.float32))
    for _ in range(4):
        clock[0] += 20.0
        tracker.observe(np.zeros(64, dtype=np.float32))
    return tracker.snapshot()


@pytest.mark.parametrize(
    ("lang", "lost", "recovered", "gave_up", "marker"),
    [
        (
            "en",
            "trying to reconnect",
            "delivers signal again after 2 reconnect",
            "stop and start the live coach",
            "Audio missing from here",
        ),
        (
            "nl",
            "probeert de tap opnieuw te koppelen",
            "levert weer signaal na 2 poging",
            "stop en start de live coach",
            "audio ontbreekt vanaf hier",
        ),
    ],
)
def test_tap_messages_and_marker_follow_the_language(monkeypatch, lang, lost, recovered, gave_up, marker) -> None:
    monkeypatch.setenv("LANGUAGE", lang)
    health = _lost_health()
    lost_text = tap_health._tap_lost_reattaching_message("prospect", health, "self")  # noqa: SLF001
    assert lost in lost_text
    assert "prospect" in lost_text and "audiotee:all" in lost_text and "self" in lost_text and "60 " in lost_text
    assert recovered in tap_health._tap_recovered_message("prospect", health, 2)  # noqa: SLF001
    gave_up_text = tap_health._tap_gave_up_message("prospect", health, 3)  # noqa: SLF001
    assert gave_up in gave_up_text and "3 " in gave_up_text
    marker_text = signal_lost_marker("prospect", health, peer_label="self", at_ms=0)["text"]
    assert marker in marker_text and "self" in marker_text
    assert "{" not in lost_text + gave_up_text + marker_text


def test_gave_up_text_no_longer_tells_the_operator_to_end_the_call() -> None:
    assert "Stop het gesprek en start het opnieuw." not in _section("nl", "audio")["tap_gave_up"]
    assert _section("nl", "audio")["tap_gave_up"].endswith(
        "Controleer de geluidsuitvoer, of stop en start de live coach opnieuw."
    )
    assert _section("en", "audio")["tap_gave_up"].endswith(
        "Check the sound output, or stop and start the live coach again."
    )


def test_tap_health_and_bar_carry_no_dutch_literal() -> None:
    nl, en = _section("nl", "audio"), _section("en", "audio")
    nl_rec, en_rec = _section("nl", "recorder"), _section("en", "recorder")
    source = (REPO_ROOT / "src/sales_copilot/audio/tap_health.py").read_text(encoding="utf-8")
    capture = (REPO_ROOT / "src/sales_copilot/audio/capture.py").read_text(encoding="utf-8")
    html = (REPO_ROOT / "dashboard/bar.html").read_text(encoding="utf-8")
    for text in (source, capture):
        assert [v for k, v in nl.items() if v != en[k] and len(v) > 12 and v in text] == []
    bar_dutch = [v for k, v in nl_rec.items() if k.startswith("bar_") and v != en_rec[k]]
    assert bar_dutch and [v for v in bar_dutch if v in html] == []
    assert not re.search(r'"(Gereed|Klaar|Mislukt|Uitschrijven\.\.\.|pauze|stil|niet gestart|verbinding kwijt)"', html)
    assert not re.search(r'"(Stop|Start|Hervat|Pauzeer) opname"', html)


def test_audio_warning_lost_is_sticky_and_recovered_shows_its_message() -> None:
    js = (REPO_ROOT / "dashboard/js/app.js").read_text(encoding="utf-8")
    branch = js.split('payload.type === "audio_warning"')[1].split('payload.type === "tap_status"')[0]
    assert 'sticky: payload.tap_event === "gave_up" || payload.tap_event === "lost"' in branch
    assert "clearAudioWarning" not in branch  # recovered shows the message on the normal timer
