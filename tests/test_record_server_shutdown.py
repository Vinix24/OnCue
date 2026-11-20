"""Shutdown transition and start-up recovery of scripts/record_server.py."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_record_server import (
    BASE,
    PORT,
    REPO_ROOT,
    SESSION_ID,
    WS_GOOD,
    FakeEngine,
    _load_module,
    _recv_until,
    _write_wav,
)

MARKER = ".transcribe-pending"
OWNED = ".recording"
HELPER = REPO_ROOT / "tests" / "record_server_sigterm_helper.py"


@pytest.fixture
def rs(tmp_path, monkeypatch):
    module = _load_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(module, "OUT_DIR", str(sessions))
    FakeEngine.session_dir = sessions / SESSION_ID
    monkeypatch.setattr(module, "RecorderEngine", FakeEngine)
    monkeypatch.setattr(module, "_PORT", PORT)
    monkeypatch.setattr(module, "_ensure_worker", lambda: None)
    return module


def _client(rs) -> TestClient:
    return TestClient(rs.app, base_url=BASE, client=("127.0.0.1", 50000))


def _queued(rs) -> list[str]:
    return [rs._jobs.get_nowait()[0].name for _ in range(rs._jobs.qsize())]


def _session(
    rs, name: str, *, age_s: float = 600.0, wavs: tuple[str, ...] = ("self.wav",), owned: bool = True
) -> Path:
    folder = Path(rs.OUT_DIR) / name
    folder.mkdir()
    if owned:
        (folder / OWNED).touch()
    for wav in wavs:
        _write_wav(folder / wav)
        os.utime(folder / wav, (time.time() - age_s,) * 2)
    return folder


def test_shutdown_finishes_a_running_recording_once_and_marks_it(rs, monkeypatch) -> None:
    stops = []
    original = FakeEngine.stop
    monkeypatch.setattr(FakeEngine, "stop", lambda self: stops.append(1) or original(self))
    with _client(rs) as client, client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
    assert stops == [1]
    assert (FakeEngine.session_dir / "metadata.json").exists()
    assert (FakeEngine.session_dir / MARKER).exists()
    assert not (FakeEngine.session_dir / OWNED).exists()
    assert rs._jobs.empty()


def test_marker_exists_while_recording_and_is_gone_after_a_normal_stop(rs) -> None:
    with _client(rs) as client, client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
        assert (FakeEngine.session_dir / OWNED).exists()
        ws.send_json({"action": "stop"})
        _recv_until(ws, lambda m: m["type"] == "stopped")
        assert not (FakeEngine.session_dir / OWNED).exists()
        assert (FakeEngine.session_dir / MARKER).exists()


def test_failed_stop_keeps_the_marker_for_recovery(rs, monkeypatch) -> None:
    def boom(self):
        raise RuntimeError("device gone")

    monkeypatch.setattr(FakeEngine, "stop", boom)
    with _client(rs) as client, client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
        ws.send_json({"action": "stop"})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] != "recording")
    assert (FakeEngine.session_dir / OWNED).exists()


def test_websocket_actions_are_refused_while_closing(rs) -> None:
    rs._state["closing"] = True
    with _client(rs) as client, client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        for action in ("start", "stop", "bogus"):
            ws.send_json({"action": action})
            assert ws.receive_json() == {"type": "error", "reason": "OnCue is shutting down"}
    assert rs._state["engine"] is None
    assert not FakeEngine.session_dir.exists()


def test_pending_session_goes_back_in_the_queue(rs) -> None:
    folder = _session(rs, "20261101-100000")
    (folder / MARKER).touch()
    with _client(rs):
        assert _queued(rs) == [folder.name]


def test_pending_session_with_a_transcript_is_not_queued_again(rs) -> None:
    folder = _session(rs, "20261101-100000")
    (folder / MARKER).touch()
    (folder / "transcript.md").write_text("done", encoding="utf-8")
    rs._recover_sessions(None)
    assert _queued(rs) == []


def test_folder_without_the_marker_is_never_touched(rs) -> None:
    folder = _session(rs, "20261101-100000", wavs=("self.wav", "prospect.wav"), owned=False)

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}

    before = snapshot()
    rs._recover_sessions(None)
    assert snapshot() == before
    assert _queued(rs) == []


def test_folder_with_a_recovery_note_is_left_alone(rs) -> None:
    folder = _session(rs, "20261101-100000")
    (folder / "recovery-failed.txt").write_text("ValueError: eerder\n", encoding="utf-8")
    rs._recover_sessions(None)
    assert (folder / "recovery-failed.txt").read_text(encoding="utf-8") == "ValueError: eerder\n"
    assert not (folder / "metadata.json").exists()
    assert (folder / OWNED).exists()
    assert _queued(rs) == []


def test_old_session_without_metadata_is_recovered_and_queued(rs) -> None:
    folder = _session(rs, "20261101-100000", wavs=("self.wav", "prospect.wav"))
    rs._recover_sessions(None)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    assert (meta["recovered"], meta["session_id"], meta["duration_ms"]) == (True, folder.name, 100)
    assert meta["duration_ms_per_stream"] == {"prospect": 100, "self": 100}
    assert meta["end_padding_ms"] == {"prospect": 0, "self": 0}
    assert (meta["sample_rate"], meta["channels"]) == (16000, 1)
    assert (folder / MARKER).exists()
    assert not (folder / OWNED).exists()
    assert _queued(rs) == [folder.name]


def test_wav_header_is_fixed_without_touching_the_audio(rs) -> None:
    folder = _session(rs, "20261101-100000")
    wav = folder / "self.wav"
    raw = bytearray(wav.read_bytes())
    raw[44:] = bytes(range(256)) * 25
    raw[40:44] = (200).to_bytes(4, "little")  # header claims 100 frames, 3200 are on disk
    wav.write_bytes(bytes(raw))
    os.utime(wav, (time.time() - 600,) * 2)
    data_before = bytes(raw[44:])
    rs._recover_sessions(None)
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnframes() == 3200
    assert wav.read_bytes()[44:] == data_before
    assert json.loads((folder / "metadata.json").read_text(encoding="utf-8"))["duration_ms"] == 200


def test_young_session_is_skipped(rs) -> None:
    folder = _session(rs, "20261101-100000", age_s=5)
    rs._recover_sessions(None)
    assert not (folder / "metadata.json").exists()
    assert _queued(rs) == []


def test_unreadable_wav_gets_a_reason_and_is_not_queued(rs) -> None:
    folder = _session(rs, "20261101-100000")
    (folder / "self.wav").write_bytes(b"not a wav file at all")
    os.utime(folder / "self.wav", (time.time() - 600,) * 2)
    rs._recover_sessions(None)
    assert "geen WAV-bestand" in (folder / "recovery-failed.txt").read_text(encoding="utf-8")
    assert not (folder / "metadata.json").exists()
    assert (folder / "self.wav").read_bytes() == b"not a wav file at all"
    assert _queued(rs) == []


@pytest.mark.parametrize("done", ["metadata.json", "transcript.md", "transcription-failed.txt"])
def test_finished_sessions_are_left_alone(rs, done) -> None:
    folder = _session(rs, "20261101-100000")
    (folder / done).write_text("{}", encoding="utf-8")
    before = sorted(p.name for p in folder.iterdir())
    rs._recover_sessions(None)
    assert sorted(p.name for p in folder.iterdir()) == before
    assert _queued(rs) == []


def test_sigterm_finishes_a_real_recording(tmp_path) -> None:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    proc = subprocess.Popen(
        [sys.executable, str(HELPER), str(sessions), str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while True:
            try:
                ws = connect(f"ws://127.0.0.1:{port}/ws")
                break
            except OSError:
                assert proc.poll() is None and time.time() < deadline, "server did not come up"
                time.sleep(0.1)
        with ws:
            ws.send(json.dumps({"action": "start", "mic": None}))
            while json.loads(ws.recv(timeout=10)).get("phase") != "recording":
                pass
            proc.send_signal(signal.SIGTERM)
            with pytest.raises(ConnectionClosed):  # the server closes the socket, then exits
                while True:
                    ws.recv(timeout=30)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    (folder,) = sessions.iterdir()
    assert (folder / "metadata.json").exists()
    assert (folder / MARKER).exists()
    assert not (folder / OWNED).exists()
