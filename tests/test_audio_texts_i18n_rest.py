"""Attach diagnostics, call-tap and WASAPI warnings, and the bar's first paint follow LANGUAGE."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sales_copilot.core import i18n

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGS = REPO_ROOT / "config" / "i18n"
AUDIO_SRC = REPO_ROOT / "src" / "sales_copilot" / "audio"

NEW_KEYS = {
    "detail_inputstream_open_failed": {},
    "detail_binary_missing": {},
    "detail_no_target_process": {},
    "detail_target_not_found": {"process": "Zoom"},
    "detail_subprocess_start_failed": {},
    "detail_no_pipes": {},
    "detail_subprocess_stopped": {},
    "detail_subprocess_stopped_early": {},
    "calltap_binary_missing": {"path": "/x/audiotee"},
    "calltap_process_not_found": {"process": "avconferenced"},
    "detail_process_not_running": {"process": "avconferenced"},
    "calltap_start_failed": {"process": "avconferenced"},
    "calltap_stopped_again": {"process": "avconferenced"},
    "detail_subprocess_stopped_again": {},
    "calltap_process_vanished": {"process": "avconferenced"},
    "detail_process_vanished": {"process": "avconferenced"},
    "wasapi_windows_only": {},
    "detail_not_windows": {},
    "wasapi_soundcard_missing": {},
    "detail_soundcard_missing": {},
    "wasapi_open_timeout": {},
    "detail_open_timeout": {},
    "wasapi_unexpected_open_error": {},
    "detail_unexpected_open_error": {},
    "wasapi_endpoint_not_found": {"endpoint": "Speakers"},
    "wasapi_no_default_output": {},
    "detail_no_default_output": {},
    "wasapi_no_loopback_device": {},
    "detail_loopback_open_failed": {},
    "wasapi_recorder_open_failed": {},
    "detail_recorder_open_failed": {},
    "wasapi_outage": {},
    "detail_device_vanished": {},
    "wasapi_bad_shape": {"rate": 16000},
    "wasapi_read_error": {},
}


@pytest.fixture(autouse=True)
def _real_catalogs():
    i18n.set_catalog_dir(CATALOGS)
    yield
    i18n.set_catalog_dir(None)


def _audio(lang: str) -> dict:
    return yaml.safe_load((CATALOGS / f"{lang}.yaml").read_text(encoding="utf-8"))["audio"]


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"{(\w+)}", text))


def test_new_keys_exist_in_both_catalogs_with_the_same_placeholders() -> None:
    en, nl = _audio("en"), _audio("nl")
    for key, params in NEW_KEYS.items():
        assert key in en and key in nl, key
        assert _placeholders(en[key]) == _placeholders(nl[key]) == set(params), key
    assert set(en) == set(nl)


@pytest.mark.parametrize("lang", ["en", "nl"])
def test_messages_resolve_with_values_filled_in(monkeypatch, lang) -> None:
    monkeypatch.setenv("LANGUAGE", lang)
    for key, params in NEW_KEYS.items():
        text = i18n.t(f"audio.{key}", **params)
        assert text and text != f"audio.{key}", key
        assert "{" not in text, key
        for value in params.values():
            assert str(value) in text, key


def test_languages_differ_for_the_diagnostics(monkeypatch) -> None:
    monkeypatch.setenv("LANGUAGE", "en")
    assert i18n.t("audio.detail_binary_missing") == "binary missing"
    monkeypatch.setenv("LANGUAGE", "nl")
    assert i18n.t("audio.detail_binary_missing") == "binary ontbreekt"
    assert i18n.t("audio.detail_target_not_found", process="Zoom") == "doelproces 'Zoom' niet gevonden"


def test_capture_calltap_and_wasapi_carry_no_dutch_literal() -> None:
    en, nl = _audio("en"), _audio("nl")
    dutch = [v for k, v in nl.items() if k in NEW_KEYS and v != en[k]]
    assert dutch
    # Dutch catalog text, and the words the old literals used, must not appear in the sources.
    pattern = re.compile(
        r"(ontbreekt|niet gevonden|kon niet|kon geen|verdween|reageerde niet|draait niet|"
        r"opnieuw gestopt|Onverwacht|geen standaard|geen doelproces|gaf een fout|alleen beschikbaar|"
        r"Telefonie-proces|doelproces)"
    )
    for name in ("capture.py", "calltap.py", "wasapi.py"):
        text = (AUDIO_SRC / name).read_text(encoding="utf-8")
        assert [v for v in dutch if v in text] == [], name
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not pattern.search(code), (name, pattern.search(code))


def test_bar_does_not_render_before_the_catalog_is_ready() -> None:
    html = (REPO_ROOT / "dashboard/bar.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1]
    # No bare render() statement at script top level (4-space indent) outside functions/handlers.
    assert not re.search(r"^ {4}render\(\);\s*$", script, re.MULTILINE)
    # The catalog promise resolves (or fails) into the first render, and render() guards on it.
    assert re.search(r"SalesCopilotI18n\.ready\.catch\(\(\) => \{\}\)\.then\(", script)
    render_body = script.split("function render() {")[1]
    assert render_body.lstrip().startswith("if (!i18nReady) return;")
