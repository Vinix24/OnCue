"""Tests for the objective telephony-tap guard and PID watcher."""

from __future__ import annotations

import pytest

from sales_copilot.audio.telephony_guard import ProcessWatcher, TelephonyTapGuard


class _FakePolicy:
    def __init__(self, allowed: set[str] | None = None) -> None:
        self._allowed = allowed or set()

    def allows(self, feature_id: str) -> bool:
        return feature_id in self._allowed


@pytest.fixture
def free_policy() -> _FakePolicy:
    return _FakePolicy()


@pytest.fixture
def pro_policy() -> _FakePolicy:
    from sales_copilot.auth.license_format import FEATURE_CALLTAP

    return _FakePolicy({FEATURE_CALLTAP})


# --- ProcessWatcher ----------------------------------------------------------


def test_process_watcher_detects_running_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import sales_copilot.audio.telephony_guard as tg

    monkeypatch.setattr(tg, "_find_pid", lambda name: 12345 if name == "avconferenced" else None)
    watcher = ProcessWatcher("avconferenced")

    assert watcher.is_running() is True
    assert watcher.pid() == 12345


def test_process_watcher_detects_missing_process(monkeypatch: pytest.MonkeyPatch) -> None:
    import sales_copilot.audio.telephony_guard as tg

    monkeypatch.setattr(tg, "_find_pid", lambda name: None)
    watcher = ProcessWatcher("avconferenced")

    assert watcher.is_running() is False
    assert watcher.pid() is None


# --- TelephonyTapGuard -------------------------------------------------------


def test_guard_excludes_telephony_in_free_tier(free_policy: _FakePolicy) -> None:
    guard = TelephonyTapGuard(feature_policy=free_policy)
    watcher = ProcessWatcher("avconferenced")

    assert guard.is_telephony_process("avconferenced") is True
    assert guard.is_target_excluded("avconferenced", watcher=watcher) is True


def test_guard_allows_telephony_in_pro_tier(pro_policy: _FakePolicy) -> None:
    guard = TelephonyTapGuard(feature_policy=pro_policy)
    watcher = ProcessWatcher("avconferenced")

    assert guard.is_target_excluded("avconferenced", watcher=watcher) is False


def test_guard_allows_non_telephony_process(free_policy: _FakePolicy) -> None:
    guard = TelephonyTapGuard(feature_policy=free_policy)
    watcher = ProcessWatcher("Google Chrome")

    assert guard.is_telephony_process("Google Chrome") is False
    assert guard.is_target_excluded("Google Chrome", watcher=watcher) is False


def test_guard_excludes_telephony_even_when_process_not_running(
    free_policy: _FakePolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: the old point-in-time check freed the gate when the PID was
    # not running yet. A telephony target must stay excluded in the free tier
    # regardless of current PID state, because AudioTee re-resolves the PID at
    # tap-start.
    import sales_copilot.audio.telephony_guard as tg

    monkeypatch.setattr(tg, "_find_pid", lambda name: None)
    guard = TelephonyTapGuard(feature_policy=free_policy)

    assert guard.is_target_excluded("avconferenced") is True


def test_guard_treats_fuzzy_name_match_as_telephony(
    free_policy: _FakePolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: a user could pass the substring "avconference" and have
    # AudioTee's pgrep -f resolve it to the same PID as avconferenced, while
    # the guard only matched the exact canonical name.
    import sales_copilot.audio.telephony_guard as tg

    def _fake_find_pid(name: str) -> int | None:
        if "avconference" in name.lower():
            return 12345
        return None

    monkeypatch.setattr(tg, "_find_pid", _fake_find_pid)
    guard = TelephonyTapGuard(feature_policy=free_policy)

    assert guard.is_telephony_process("avconference") is True
    assert guard.is_target_excluded("avconference") is True


def test_guard_fuzzy_match_does_not_false_positive_for_unrelated_process(
    free_policy: _FakePolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sales_copilot.audio.telephony_guard as tg

    def _fake_find_pid(name: str) -> int | None:
        if name.lower() == "avconferenced":
            return 12345
        if name.lower() == "someunrelatedapp":
            return 99999
        return None

    monkeypatch.setattr(tg, "_find_pid", _fake_find_pid)
    guard = TelephonyTapGuard(feature_policy=free_policy)

    assert guard.is_telephony_process("someunrelatedapp") is False
    assert guard.is_target_excluded("someunrelatedapp") is False


def test_guard_is_case_insensitive(free_policy: _FakePolicy) -> None:
    guard = TelephonyTapGuard(feature_policy=free_policy)

    assert guard.is_telephony_process("AVConferenceD") is True
    assert guard.is_telephony_process("avconferenced") is True
