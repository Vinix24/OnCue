"""The notetaker page and its server messages follow LANGUAGE (English unless nl)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.test_record_server import BASE, _load_module

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDER_HTML = REPO_ROOT / "dashboard" / "recorder.html"


def _catalog(lang: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "config" / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))


class _Page(HTMLParser):
    """Visible text nodes (with the i18n key of their element) and elements with a title attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, str | None]] = []
        self.titled: list[dict] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag not in ("meta", "br", "input", "link"):
            self.stack.append((tag, d))
        if "title" in d and tag != "title":
            self.titled.append(d)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if not self.stack or self.stack[-1][0] in ("script", "style"):
            return
        text = data.strip()
        if text:
            self.texts.append((text, self.stack[-1][1].get("data-i18n")))


def _parse() -> _Page:
    page = _Page()
    page.feed(RECORDER_HTML.read_text(encoding="utf-8"))
    return page


def test_recorder_page_has_no_hardcoded_text() -> None:
    page = _parse()
    keys = _catalog("en")["recorder"]
    assert page.texts
    for text, key in page.texts:
        if not re.search(r"[A-Za-z]{2}", text):
            continue  # symbols and numbers only (the timer, the flag icon)
        assert key and key.startswith("recorder.") and key[len("recorder."):] in keys, text
    assert page.titled == []  # a title attribute must come from data-i18n-attr


def test_recorder_page_carries_no_dutch_literal() -> None:
    html = RECORDER_HTML.read_text(encoding="utf-8")
    en, nl = _catalog("en")["recorder"], _catalog("nl")["recorder"]
    dutch_only = [v for k, v in nl.items() if v != en[k] and len(v) > 6]
    assert dutch_only
    assert [v for v in dutch_only if v in html] == []
    assert 'lang="nl"' not in html


def test_recorder_keys_used_by_page_exist_in_both_catalogs() -> None:
    html = RECORDER_HTML.read_text(encoding="utf-8")
    used = set(re.findall(r"recorder\.([a-z_]+)", html)) | set(re.findall(r'tr\("([a-z_]+)"', html))
    used = {key for key in used if not key.endswith("_")} | {"meter_dead_self", "meter_dead_prospect"}
    for lang in ("en", "nl"):
        assert used <= set(_catalog(lang)["recorder"]), lang


def _client(monkeypatch, language: str | None):
    monkeypatch.delenv("LANGUAGE", raising=False)
    if language:
        monkeypatch.setenv("LANGUAGE", language)
    module = _load_module()
    monkeypatch.setattr(module, "_ensure_worker", lambda: None)
    return module, TestClient(module.app, base_url=BASE, client=("127.0.0.1", 50000))


@pytest.mark.parametrize(
    ("language", "expected_lang", "detail", "label"),
    [
        (None, "en", "Show in Finder only exists on macOS", "Show in Finder"),
        ("nl", "nl", "Toon in Finder bestaat alleen op macOS", "Toon in Finder"),
    ],
)
def test_server_message_and_page_language(monkeypatch, language, expected_lang, detail, label) -> None:
    module, client = _client(monkeypatch, language)
    monkeypatch.setattr(module, "_can_reveal", lambda: False)
    response = client.post("/reveal", json={"session": "x"})
    assert response.status_code == 404
    assert response.json()["detail"] == detail
    assert client.get("/api/config").json() == {"language": expected_lang}
    assert f'<html lang="{expected_lang}">' in client.get("/").text
    assert client.get(f"/i18n/{expected_lang}.json").json()["recorder"]["reveal"] == label


def test_server_refuses_an_unknown_catalog_language(monkeypatch) -> None:
    _module, client = _client(monkeypatch, None)
    assert client.get("/i18n/fr.json").status_code == 404
    assert client.get("/i18n/..%2Fen.json").status_code == 404
    assert "SalesCopilotI18n" in client.get("/js/i18n.js").text


@pytest.mark.parametrize(("language", "text"), [(None, "Disk full"), ("nl", "Schijf vol")])
def test_model_endpoint_error_follows_language(monkeypatch, language, text) -> None:
    module, client = _client(monkeypatch, language)

    class _Failed:
        def status(self):
            return {"state": "failed", "done": 0, "total": 0, "error": "model_error_disk_full", "detail": ""}

        def ensure(self):
            return None

    monkeypatch.setattr(module, "MODEL_DOWNLOADER", _Failed())
    assert text in client.get("/model").json()["error"]


@pytest.mark.parametrize("key", ["tap_lost", "tap_gave_up", "tap_reattached", "tap_no_system_audio"])
def test_tap_warning_keys_exist_in_both_languages(key: str) -> None:
    for lang in ("en", "nl"):
        assert _catalog(lang)["recorder"][key].strip(), (lang, key)
    assert _catalog("en")["recorder"][key] != _catalog("nl")["recorder"][key]


def test_recorder_on_state_renders_the_tap_note() -> None:
    html = RECORDER_HTML.read_text(encoding="utf-8")
    body = html.split("function onState(msg) {", 1)[1]
    # Cut at the next top-level function so the check stays inside onState.
    body = re.split(r"\n    function \w+\(", body, maxsplit=1)[0]
    assert "renderTap(msg.tap)" in body


@pytest.mark.parametrize("page", ["recorder.html", "bar.html"])
def test_pages_use_the_gave_up_text(page: str) -> None:
    html = (REPO_ROOT / "dashboard" / page).read_text(encoding="utf-8")
    assert "tap_gave_up" in html
