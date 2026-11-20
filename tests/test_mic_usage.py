from __future__ import annotations

import sys

import pytest

from sales_copilot.audio import mic_usage
from sales_copilot.audio.mic_usage import (
    MicUsageOk,
    MicUsageReadError,
    MicUsageUnavailable,
    MicUser,
    NativeReadError,
    group_by_bundle,
    read_mic_users,
)

OWN = 100
TABLE = {1: 0, OWN: 1, 101: OWN, 102: 101, 200: 1, 201: 200, 202: 200, 300: 1}


def _read(raw, **kw):
    return read_mic_users(reader=lambda: raw, process_table=lambda: TABLE, own_pid=OWN, **kw)


def test_empty_list_is_ok_and_empty():
    assert _read([]) == MicUsageOk(())


def test_one_app():
    assert _read([(200, "com.example.meet", True)]) == MicUsageOk((MicUser(200, "com.example.meet"),))


def test_several_apps():
    result = _read([(200, "com.a", True), (300, "com.b", True)])
    assert isinstance(result, MicUsageOk)
    assert {u.bundle_id for u in result.users} == {"com.a", "com.b"}


def test_not_running_input_is_skipped():
    assert _read([(200, "com.a", False)]) == MicUsageOk(())


def test_helpers_group_into_one_entry():
    users = [MicUser(202, "com.a"), MicUser(200, "com.a"), MicUser(201, "com.a"), MicUser(300, ""), MicUser(400, "")]
    assert group_by_bundle(users) == {
        "com.a": (200, 201, 202),
        "pid:300": (300,),
        "pid:400": (400,),
    }


def test_own_process_and_descendants_excluded():
    result = _read([(OWN, "com.own", True), (101, "", True), (102, "", True), (200, "com.a", True)])
    assert result == MicUsageOk((MicUser(200, "com.a"),))


def test_unavailable_on_other_platform(monkeypatch):
    monkeypatch.setattr(mic_usage.sys, "platform", "win32")
    assert isinstance(read_mic_users(), MicUsageUnavailable)
    assert mic_usage.mic_usage_available() is False


def test_unavailable_on_macos_14_1(monkeypatch):
    monkeypatch.setattr(mic_usage.sys, "platform", "darwin")
    monkeypatch.setattr(mic_usage.platform, "mac_ver", lambda: ("14.1", ("", "", ""), ""))
    assert isinstance(read_mic_users(), MicUsageUnavailable)
    assert mic_usage.mic_usage_available() is False


def test_unavailable_when_process_list_missing(monkeypatch):
    monkeypatch.setattr(mic_usage.sys, "platform", "darwin")
    monkeypatch.setattr(mic_usage.platform, "mac_ver", lambda: ("15.0", ("", "", ""), ""))
    monkeypatch.setattr(mic_usage, "_make_reader", lambda: None)
    assert isinstance(read_mic_users(), MicUsageUnavailable)


def test_nonzero_status_is_read_error_not_empty():
    def failing():
        raise NativeReadError("pid: OSStatus 560947818")

    result = read_mic_users(reader=failing, process_table=lambda: TABLE, own_pid=OWN)
    assert result == MicUsageReadError("pid: OSStatus 560947818")


def test_failing_process_table_is_read_error():
    def broken():
        raise NativeReadError("ps exited with status 1")

    result = read_mic_users(reader=lambda: [], process_table=broken, own_pid=OWN)
    assert isinstance(result, MicUsageReadError)


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Audio exists on macOS only")
def test_real_reader_is_ok_or_read_error():
    result = read_mic_users()
    if isinstance(result, MicUsageUnavailable):
        assert mic_usage._platform_ok() is not None or not mic_usage.mic_usage_available()
    else:
        assert isinstance(result, (MicUsageOk, MicUsageReadError))
