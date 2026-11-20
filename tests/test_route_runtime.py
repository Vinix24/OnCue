from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from sales_copilot.core import route_runtime as rr


@contextmanager
def _listening() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        yield s.getsockname()[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _put(tmp_path, route: str, content: str) -> None:
    path = rr.runtime_file(route, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_write_then_read_leaves_no_temp_file(tmp_path) -> None:
    path = rr.write_runtime_file("copilot", 8761, tmp_path)
    assert path == tmp_path / "run" / "copilot.json"
    assert json.loads(path.read_text()) == {"pid": os.getpid(), "port": 8761}
    assert rr.read_runtime_file("copilot", tmp_path) == {"pid": os.getpid(), "port": 8761}
    assert [p.name for p in path.parent.iterdir()] == ["copilot.json"]


def test_remove_only_removes_our_own_file(tmp_path) -> None:
    _put(tmp_path, "notetaker", json.dumps({"pid": _dead_pid(), "port": 8780}))
    assert rr.remove_runtime_file("notetaker", tmp_path) is False
    assert rr.runtime_file("notetaker", tmp_path).exists()
    rr.write_runtime_file("notetaker", 8780, tmp_path)
    assert rr.remove_runtime_file("notetaker", tmp_path) is True
    assert not rr.runtime_file("notetaker", tmp_path).exists()
    assert rr.remove_runtime_file("notetaker", tmp_path) is False


def test_unknown_route_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        rr.write_runtime_file("other", 1, tmp_path)
    with pytest.raises(ValueError):
        rr.other_route_running("other", data_dir=tmp_path, environ={})


def test_live_pid_with_answering_port_is_running(tmp_path) -> None:
    with _listening() as port:
        rr.write_runtime_file("notetaker", port, tmp_path)
        assert rr.other_route_running("copilot", data_dir=tmp_path, environ={}) is True


def test_live_pid_whose_port_does_not_answer_is_not_running(tmp_path) -> None:
    rr.write_runtime_file("notetaker", _free_port(), tmp_path)
    assert rr.other_route_running("copilot", data_dir=tmp_path, environ={}) is False


def test_live_pid_does_not_fall_back_to_env_when_its_port_is_silent(tmp_path) -> None:
    with _listening() as env_port:
        rr.write_runtime_file("notetaker", _free_port(), tmp_path)
        env = {"RECORDER_PORT": str(env_port)}
        assert rr.other_route_running("copilot", data_dir=tmp_path, environ=env) is False


def test_dead_pid_counts_as_absent_and_falls_back_to_env(tmp_path) -> None:
    with _listening() as port:
        _put(tmp_path, "notetaker", json.dumps({"pid": _dead_pid(), "port": _free_port()}))
        env = {"RECORDER_PORT": str(port)}
        assert rr.other_route_running("copilot", data_dir=tmp_path, environ=env) is True
    assert rr.other_route_running("copilot", data_dir=tmp_path, environ=env) is False


UNPARSABLE = ["not json", "[]", '{"pid": "x", "port": 1}', '{"pid": 1}', '{"pid": true, "port": 1}', ""]


@pytest.mark.parametrize("content", UNPARSABLE)
def test_unparsable_file_counts_as_absent(tmp_path, content) -> None:
    with _listening() as port:
        _put(tmp_path, "notetaker", content)
        env = {"RECORDER_PORT": str(port)}
        assert rr.read_runtime_file("notetaker", tmp_path) is None
        assert rr.other_route_running("copilot", data_dir=tmp_path, environ=env) is True


def test_no_file_and_non_default_env_port_that_answers_is_running(tmp_path) -> None:
    with _listening() as port:
        assert rr.other_route_running("copilot", data_dir=tmp_path, environ={"RECORDER_PORT": str(port)}) is True
        assert rr.other_route_running("notetaker", data_dir=tmp_path, environ={"WS_HUB_PORT": str(port)}) is True


def test_no_file_and_nothing_listening_is_not_running(tmp_path) -> None:
    env = {"RECORDER_PORT": str(_free_port()), "WS_HUB_PORT": str(_free_port())}
    assert rr.other_route_running("copilot", data_dir=tmp_path, environ=env) is False
    assert rr.other_route_running("notetaker", data_dir=tmp_path, environ=env) is False


def test_runtime_file_port_wins_over_env(tmp_path) -> None:
    with _listening() as file_port, _listening() as env_port:
        assert file_port != env_port
        rr.write_runtime_file("copilot", file_port, tmp_path)
        env = {"WS_HUB_PORT": str(_free_port())}
        assert rr.other_route_running("notetaker", data_dir=tmp_path, environ=env) is True
        del env_port


def test_configured_port_falls_back_on_bad_values() -> None:
    assert rr.configured_port("notetaker", {"RECORDER_PORT": "x"}) == 8780
    assert rr.configured_port("notetaker", {"RECORDER_PORT": "99999"}) == 8780
    assert rr.configured_port("copilot", {}) == 8760
    assert rr.configured_port("copilot", {"WS_HUB_PORT": " 9000 "}) == 9000
