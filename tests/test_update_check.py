"""Tests for the on-demand update check and its library endpoint (ADR-006 point 7)."""

# ruff: noqa: F811  (the rs fixture is imported from test_record_server and used as a test argument)
from __future__ import annotations

import importlib
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.core import update_check as uc
from tests.test_record_server import BASE, EVIL_ORIGIN, GOOD_ORIGIN, _load_module, rs  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_URL = "https://github.com/Vinix24/OnCue/releases/tag/v1.0.1"


def _release_body(tag: str = "v1.0.1", url: str = RELEASE_URL) -> bytes:
    return json.dumps({"tag_name": tag, "html_url": url}).encode()


def _fetch(body: bytes, calls: list | None = None):
    def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        if calls is not None:
            calls.append((url, headers, timeout))
        return body

    return fetch


def _raising(exc: BaseException):
    def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        raise exc

    return fetch


# --- version ordering --------------------------------------------------------


def test_newer_version_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    result = uc.check_for_updates(fetch=_fetch(_release_body("v1.0.1")), lang="en")
    assert result["current_version"] == "1.0.0"
    assert result["latest_version"] == "1.0.1"
    assert result["update_available"] is True
    assert result["release_url"] == RELEASE_URL
    assert result["error"] is None


def test_same_version_is_not_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    result = uc.check_for_updates(fetch=_fetch(_release_body("v1.0.0")), lang="en")
    assert result["latest_version"] == "1.0.0"
    assert result["update_available"] is False


def test_older_latest_version_is_not_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    result = uc.check_for_updates(fetch=_fetch(_release_body("v0.9.9")), lang="en")
    assert result["latest_version"] == "0.9.9"
    assert result["update_available"] is False


def test_prerelease_sorts_before_its_release() -> None:
    rc6 = uc.parse_version("v1.0.0-rc.6")
    release = uc.parse_version("1.0.0")
    newer = uc.parse_version("v1.0.1")
    assert rc6 < release < newer
    assert str(rc6) == "1.0.0-rc.6"


def test_prerelease_number_compares_numerically() -> None:
    assert uc.parse_version("1.0.0-rc.6") < uc.parse_version("1.0.0-rc.10")


@pytest.mark.parametrize(
    ("installed", "tag", "expected"),
    [
        ("1.0.0-rc.6", "v1.0.0", True),
        ("1.0.0", "v1.0.1", True),
        ("1.0.0-rc.6", "v1.0.0-rc.10", True),
        ("1.0.0", "v1.0.0-rc.6", False),
    ],
)
def test_prerelease_ordering_through_the_check(
    monkeypatch: pytest.MonkeyPatch, installed: str, tag: str, expected: bool
) -> None:
    monkeypatch.setattr(uc, "__version__", installed)
    result = uc.check_for_updates(fetch=_fetch(_release_body(tag)), lang="en")
    assert result["update_available"] is expected


@pytest.mark.parametrize("text", ["", "1.0", "v1.0.0.1", "release-1.0.0", "1.0.0-"])
def test_invalid_version_strings_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        uc.parse_version(text)


# --- install routes ----------------------------------------------------------


def test_app_route_opens_the_release_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    monkeypatch.setattr(uc, "is_frozen_app", lambda: True)
    result = uc.check_for_updates(fetch=_fetch(_release_body()), lang="en")
    assert result["install_route"] == "app"
    assert result["action"] == {
        "type": "release_page",
        "url": RELEASE_URL,
        "download": "OnCue-1.0.1-arm64.dmg",
    }


def test_one_line_route_runs_the_readme_install_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    monkeypatch.setattr(uc, "is_frozen_app", lambda: False)
    monkeypatch.setattr(uc, "resolve_app_support", lambda: tmp_path)
    result = uc.check_for_updates(fetch=_fetch(_release_body()), lang="en")
    assert result["install_route"] == "one_line_install"
    assert result["action"]["type"] == "command"
    assert result["action"]["commands"] == [uc.ONE_LINE_INSTALL_COMMAND]
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert uc.ONE_LINE_INSTALL_COMMAND in readme


def test_git_checkout_route_pulls_and_reinstalls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    monkeypatch.setattr(uc, "is_frozen_app", lambda: False)
    monkeypatch.setattr(uc, "resolve_app_support", lambda: tmp_path)
    result = uc.check_for_updates(fetch=_fetch(_release_body()), lang="en")
    assert result["install_route"] == "git_checkout"
    assert result["action"] == {"type": "steps", "commands": ["git pull", "bash scripts/install.sh"]}
    install = (REPO_ROOT / "INSTALL.md").read_text(encoding="utf-8")
    update_section = install.split("## Update", 1)[1]
    for command in result["action"]["commands"]:
        assert command in update_section


# --- the request itself ------------------------------------------------------


def test_request_carries_only_the_two_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    calls: list = []
    uc.check_for_updates(fetch=_fetch(_release_body(), calls), lang="en")
    ((url, headers, timeout),) = calls
    assert url == "https://api.github.com/repos/Vinix24/OnCue/releases/latest"
    assert "?" not in url
    assert headers == {
        "Accept": "application/vnd.github+json",
        "User-Agent": "OnCue/1.0.0",
    }
    assert timeout == 5.0


# --- failure paths -----------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (urllib.error.URLError("offline"), "error_network"),
        (TimeoutError("timed out"), "error_network"),
        (urllib.error.HTTPError(uc.RELEASES_LATEST_URL, 403, "rate limited", None, None), "error_rate_limited"),
        (urllib.error.HTTPError(uc.RELEASES_LATEST_URL, 429, "rate limited", None, None), "error_rate_limited"),
        (urllib.error.HTTPError(uc.RELEASES_LATEST_URL, 404, "not found", None, None), "error_no_release"),
        (urllib.error.HTTPError(uc.RELEASES_LATEST_URL, 500, "server error", None, None), "error_http"),
    ],
)
def test_network_and_http_failures_return_a_message_and_do_not_raise(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, code: str
) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    result = uc.check_for_updates(fetch=_raising(exc), lang="en")
    assert result["error"]["code"] == code
    assert result["error"]["message"]
    assert result["error"]["message"] != f"update.{code}"
    assert result["latest_version"] is None
    assert result["release_url"] is None
    assert result["update_available"] is None
    assert result["action"] is None


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"[]",
        b'{"tag_name": 1, "html_url": "https://example.test"}',
        b'{"html_url": "https://example.test"}',
        b'{"tag_name": "v1.0.1", "html_url": "  "}',
        json.dumps({"tag_name": "not-a-version", "html_url": RELEASE_URL}).encode(),
        b"null",
    ],
)
def test_malformed_payloads_return_a_message_and_do_not_raise(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    result = uc.check_for_updates(fetch=_fetch(body), lang="en")
    assert result["error"]["code"] == "error_malformed"
    assert result["error"]["message"]
    assert result["update_available"] is None


def test_failure_message_is_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uc, "__version__", "1.0.0")
    english = uc.check_for_updates(fetch=_raising(urllib.error.URLError("offline")), lang="en")
    dutch = uc.check_for_updates(fetch=_raising(urllib.error.URLError("offline")), lang="nl")
    assert english["error"]["message"] != dutch["error"]["message"]
    assert "GitHub" in dutch["error"]["message"]
    assert "niet bereikbaar" in dutch["error"]["message"]


# --- nothing happens until the button is clicked -----------------------------


def test_import_app_and_library_page_make_no_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    importlib.reload(uc)
    module = _load_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(module, "OUT_DIR", str(sessions))
    monkeypatch.setattr(module.route_runtime, "resolve_app_support", lambda: tmp_path)
    monkeypatch.setattr(module, "_PORT", 8780)
    module._state.update({"engine": None, "starting": False, "broadcaster": None, "latest": None})
    module._clients.clear()
    with TestClient(module.app, base_url=BASE, client=("127.0.0.1", 50000)) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "update-btn" in response.text
    assert calls == []


@pytest.fixture
def client(rs):
    with TestClient(rs.app, base_url=BASE, client=("127.0.0.1", 50000)) as c:
        yield c


def test_endpoint_returns_the_check_result_only_when_called(rs, client, monkeypatch) -> None:
    seen: dict = {}
    payload = {
        "current_version": "1.0.0",
        "latest_version": "1.0.1",
        "update_available": True,
        "release_url": RELEASE_URL,
        "install_route": "app",
        "action": {"type": "release_page", "url": RELEASE_URL, "download": "OnCue-1.0.1-arm64.dmg"},
        "error": None,
    }

    def fake_check(**kwargs):
        seen.update(kwargs)
        return payload

    monkeypatch.setattr(rs.update_check, "check_for_updates", fake_check)
    assert client.get("/").status_code == 200
    assert seen == {}
    response = client.get("/api/update-check", headers=GOOD_ORIGIN)
    assert response.status_code == 200
    assert response.json() == payload
    assert seen == {"lang": "nl"}


def test_endpoint_is_local_only(rs, client) -> None:
    response = client.get("/api/update-check", headers=EVIL_ORIGIN)
    assert response.status_code == 403


def test_endpoint_turns_a_failed_request_into_a_visible_error(rs, client, monkeypatch) -> None:
    calls: list = []

    def offline(*args, **kwargs):
        calls.append(args)
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    assert client.get("/").status_code == 200
    assert calls == []
    response = client.get("/api/update-check", headers=GOOD_ORIGIN)
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == "error_network"
    assert body["update_available"] is None
    assert len(calls) == 1
