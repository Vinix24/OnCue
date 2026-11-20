"""ADR-008: the Windows loopback follows the endpoint where the call plays.

The selector is pure; COM is mocked at its seam (``_ole32``).
"""

from __future__ import annotations

import logging

import pytest

from sales_copilot.audio import wasapi_endpoint as we
from sales_copilot.audio.capture import AudioConfig
from sales_copilot.audio.wasapi_endpoint import select_endpoint

APPS = frozenset({"chrome.exe", "teams.exe"})


def test_active_session_beats_communications_default() -> None:
    sessions = {"spk": frozenset(), "bt": frozenset({"Teams.exe"})}
    assert select_endpoint(sessions, "spk", "spk", None, APPS) == ("bt", "active_session:Teams.exe")


def test_communications_default_beats_console_default() -> None:
    assert select_endpoint({"a": frozenset({"notepad.exe"})}, "bt", "spk", None, APPS) == (
        "bt",
        "communications_default",
    )


def test_console_default_when_nothing_else() -> None:
    assert select_endpoint({}, None, "spk", None, APPS) == ("spk", "console_default")


def test_nothing_known_is_none() -> None:
    assert select_endpoint({}, None, None, None, APPS) == (None, "no_endpoint")


def test_exe_match_is_case_insensitive() -> None:
    assert select_endpoint({"bt": frozenset({"CHROME.EXE"})}, None, "spk", None, APPS)[0] == "bt"
    assert select_endpoint({"bt": frozenset({"chrome.exe"})}, None, "spk", None, frozenset({"Chrome.EXE"}))[0] == "bt"


def test_tie_prefers_communications_default() -> None:
    sessions = {"a": frozenset({"chrome.exe"}), "b": frozenset({"teams.exe"})}
    assert select_endpoint(sessions, "b", "a", "a", APPS) == ("b", "active_session:teams.exe")


def test_tie_without_communications_keeps_current() -> None:
    sessions = {"a": frozenset({"chrome.exe"}), "b": frozenset({"teams.exe"})}
    assert select_endpoint(sessions, "spk", "spk", "a", APPS) == ("a", "tie_kept_current")
    assert select_endpoint(sessions, "spk", "spk", "b", APPS) == ("b", "tie_kept_current")


def test_tie_without_communications_or_current_takes_first_sorted_and_says_so(caplog) -> None:
    import logging

    caplog.set_level(logging.INFO, logger=we.logger.name)
    sessions = {"b": frozenset({"teams.exe"}), "a": frozenset({"chrome.exe"})}
    assert select_endpoint(sessions, "spk", "spk", "other", APPS) == ("a", "tie_first_sorted")
    assert select_endpoint(sessions, "spk", "spk", None, APPS) == ("a", "tie_first_sorted")
    assert any("first sorted" in r.getMessage() for r in caplog.records)


def test_call_apps_yaml_loads_adr_list() -> None:
    apps = we.load_call_apps()
    assert {
        "chrome.exe",
        "msedge.exe",
        "teams.exe",
        "ms-teams.exe",
        "zoom.exe",
        "whatsapp.exe",
        "phoneexperiencehost.exe",
    } == set(apps)


def test_call_apps_malformed_file_raises(tmp_path) -> None:
    bad = tmp_path / "apps.yaml"
    bad.write_text("call_apps: nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        we.load_call_apps(bad)


def test_read_snapshot_off_windows_is_no_information(monkeypatch) -> None:
    monkeypatch.setattr(we.sys, "platform", "darwin")
    assert we.read_snapshot() is None


def test_com_failure_logs_once_and_returns_none(monkeypatch, caplog) -> None:
    class _Ole32:
        def CoCreateInstance(self, *a):  # noqa: N802
            raise OSError("boom")

    monkeypatch.setattr(we.sys, "platform", "win32")
    monkeypatch.setattr(we, "_ole32", lambda: _Ole32())
    monkeypatch.setattr(we, "_warned", set())
    with caplog.at_level(logging.WARNING, logger=we.logger.name):
        assert we.read_snapshot() is None
        assert we.read_snapshot() is None
    assert len([r for r in caplog.records if "enumeration failed" in r.getMessage()]) == 1


def test_pick_with_no_snapshot_is_no_information() -> None:
    assert we.pick_endpoint_id("x", APPS, None) == (None, "no_information")


def test_switch_defaults_off() -> None:
    assert AudioConfig().windows_endpoint_follow is False
