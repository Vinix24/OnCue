"""Tests for scripts/record_server.py: shared state, auto-transcribe, transcript endpoint."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from sales_copilot.audio.recorder_engine import EngineSnapshot, StreamSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "record_server.py"

SESSION_ID = "20261108-140300"
PORT = 8780
BASE = f"http://127.0.0.1:{PORT}"
GOOD_ORIGIN = {"origin": BASE}
# TestClient always sends Host "testserver" on websockets, whatever the base_url.
WS_HOST = {"host": f"127.0.0.1:{PORT}"}
WS_GOOD = {**GOOD_ORIGIN, **WS_HOST}
EVIL_ORIGIN = {"origin": "http://evil.example"}


def _load_module():
    spec = importlib.util.spec_from_file_location("record_server_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)


class FakeEngine:
    """Stands in for RecorderEngine: no audio devices, writes a real session dir on stop."""

    session_dir: Path

    def __init__(self, **_kwargs) -> None:
        self.elapsed = 12.3

    def start(self) -> Path:
        return self.session_dir

    def snapshot(self) -> EngineSnapshot:
        return EngineSnapshot(
            recording=True,
            elapsed_s=self.elapsed,
            directory=str(self.session_dir),
            streams=[
                StreamSnapshot("prospect", -30.0, -20.0, False, True),
                StreamSnapshot("self", -40.0, -25.0, False, True),
            ],
        )

    def stop(self) -> Path:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        _write_wav(self.session_dir / "self.wav")
        _write_wav(self.session_dir / "prospect.wav")
        (self.session_dir / "metadata.json").write_text(json.dumps({"duration_ms": 754000}))
        return self.session_dir


class FakeBackend:
    def __init__(self, segments: dict[str, list[dict]] | None = None, error: Exception | None = None) -> None:
        self.segments = segments or {}
        self.error = error
        self.stopped = False

    async def transcribe_file_segments(self, path: Path) -> list[dict]:
        if self.error is not None:
            raise self.error
        return self.segments.get(path.stem, [])

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def rs(tmp_path, monkeypatch):
    module = _load_module()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(module, "OUT_DIR", str(sessions))
    FakeEngine.session_dir = sessions / SESSION_ID
    monkeypatch.setattr(module, "RecorderEngine", FakeEngine)
    monkeypatch.setattr(module, "_language", lambda: "nl")
    module._state.update({"engine": None, "starting": False, "broadcaster": None, "latest": None})
    module._clients.clear()
    monkeypatch.setattr(module, "_PORT", PORT)
    return module


@pytest.fixture
def client(rs):
    with TestClient(rs.app, base_url=BASE, client=("127.0.0.1", 50000)) as c:
        yield c


def _use_backend(rs, monkeypatch, backend: FakeBackend) -> FakeBackend:
    monkeypatch.setattr(rs, "_build_backend", lambda: backend)
    return backend


def _recv_until(ws, predicate, limit: int = 200) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message never arrived")


def test_levels_reach_every_client_including_one_that_connects_mid_recording(rs, client) -> None:
    with client.websocket_connect("/ws", headers=WS_GOOD) as first:
        assert first.receive_json()["phase"] == "idle"
        first.send_json({"action": "start", "mic": None})
        _recv_until(first, lambda m: m["type"] == "level")

        with client.websocket_connect("/ws", headers=WS_GOOD) as second:
            hello = second.receive_json()
            assert hello["type"] == "state"
            assert hello["phase"] == "recording"
            assert hello["elapsed_s"] == pytest.approx(12.3)

            level_second = _recv_until(second, lambda m: m["type"] == "level")
            level_first = _recv_until(first, lambda m: m["type"] == "level")
            for level in (level_first, level_second):
                assert level["recording"] is True
                assert {s["label"] for s in level["streams"]} == {"prospect", "self"}

            second.send_json({"action": "stop"})
            _recv_until(first, lambda m: m["type"] == "stopped")
        rs._jobs.join()


def test_stop_transcribes_and_writes_labeled_transcript(rs, client, monkeypatch) -> None:
    backend = _use_backend(
        rs,
        monkeypatch,
        FakeBackend({
            "self": [{"start": 1.0, "end": 3.0, "text": "Goedemiddag, met Vincent."}],
            "prospect": [{"start": 65.0, "end": 68.0, "text": "Dag, ik heb een vraag over de prijs."}],
        }),
    )
    with client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
        ws.send_json({"action": "stop"})
        final = _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] in ("ready", "failed"))

    assert final["phase"] == "ready"
    assert final["session"] == SESSION_ID
    transcript = (FakeEngine.session_dir / "transcript.md").read_text(encoding="utf-8")
    assert "Datum: 2026-11-08" in transcript
    assert "Starttijd: 14:03" in transcript
    assert "Duur: 12:34" in transcript
    assert "[00:01] Jij: Goedemiddag, met Vincent." in transcript
    assert "[01:05] Ander: Dag, ik heb een vraag over de prijs." in transcript
    assert backend.stopped is True
    assert not (FakeEngine.session_dir / "transcript.md.tmp").exists()


def test_english_language_uses_you_and_other(rs, monkeypatch) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    _write_wav(session / "self.wav")
    _write_wav(session / "prospect.wav")
    _use_backend(
        rs,
        monkeypatch,
        FakeBackend({
            "self": [{"start": 0.0, "end": 2.0, "text": "Hello there."}],
            "prospect": [{"start": 5.0, "end": 7.0, "text": "Hi, thanks for calling."}],
        }),
    )
    monkeypatch.setattr(rs, "_language", lambda: "en")

    rs.transcribe_session_dir(session)

    transcript = (session / "transcript.md").read_text(encoding="utf-8")
    assert "Start time: 14:03" in transcript
    assert "[00:00] You: Hello there." in transcript
    assert "[00:05] Other: Hi, thanks for calling." in transcript


def test_default_labels_of_other_callers_are_untouched() -> None:
    from sales_copilot.modules.transcriber.postcall import DEFAULT_LABELS

    assert DEFAULT_LABELS["self"] == "JIJ"
    assert DEFAULT_LABELS["prospect"] == "PROSPECT"


def test_silent_recording_still_writes_a_transcript(rs, monkeypatch) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    _write_wav(session / "self.wav")
    _use_backend(rs, monkeypatch, FakeBackend({}))

    path = rs.transcribe_session_dir(session)

    assert "(Geen spraak herkend.)" in path.read_text(encoding="utf-8")


def test_failing_backend_reports_failed_state_and_logs(rs, client, monkeypatch, caplog) -> None:
    _use_backend(rs, monkeypatch, FakeBackend(error=RuntimeError("whisper binary not found")))
    caplog.set_level(logging.ERROR, logger="sales_copilot.record_server")
    with client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
        ws.send_json({"action": "stop"})
        final = _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] in ("ready", "failed"))

    assert final["phase"] == "failed"
    assert final["reason"] == "RuntimeError: whisper binary not found"
    assert not (FakeEngine.session_dir / "transcript.md").exists()
    failures = [r for r in caplog.records if "Transcription of" in r.getMessage()]
    assert failures and failures[0].exc_info is not None


def test_session_without_wavs_fails_loudly(rs) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    with pytest.raises(RuntimeError, match="Geen opname"):
        rs.transcribe_session_dir(session)


def test_client_connecting_after_failure_sees_latest_result(rs, client, monkeypatch) -> None:
    _use_backend(rs, monkeypatch, FakeBackend(error=RuntimeError("boom")))
    with client.websocket_connect("/ws", headers=WS_GOOD) as ws:
        ws.receive_json()
        ws.send_json({"action": "start", "mic": None})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "recording")
        ws.send_json({"action": "stop"})
        _recv_until(ws, lambda m: m["type"] == "state" and m["phase"] == "failed")

    with client.websocket_connect("/ws", headers=WS_GOOD) as late:
        hello = late.receive_json()
    assert hello["phase"] == "failed"
    assert hello["reason"] == "RuntimeError: boom"


def test_transcriptions_run_one_at_a_time_and_during_a_new_recording(rs, monkeypatch) -> None:
    import threading

    running = 0
    peak = 0
    guard = threading.Lock()
    gate = threading.Event()

    def fake_transcribe(session_dir: Path) -> Path:
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        gate.wait(timeout=5)
        with guard:
            running -= 1
        return session_dir / "transcript.md"

    monkeypatch.setattr(rs, "transcribe_session_dir", fake_transcribe)

    class _Loop:
        def is_closed(self) -> bool:
            return True

    monkeypatch.setattr(rs, "_publish_from_thread", lambda _loop: None)
    for name in ("a", "b", "c"):
        rs._enqueue_transcription(Path(rs.OUT_DIR) / name, _Loop())
    assert rs._state_message()["phase"] == "transcribing"
    assert rs._state_message()["transcribing"] == 3
    gate.set()
    rs._jobs.join()

    assert peak == 1
    assert rs._state_message()["transcribing"] == 0


def test_transcript_endpoint_serves_the_session_transcript(rs, client) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    (session / "transcript.md").write_text("# Transcript\n", encoding="utf-8")

    res = client.get("/transcript", params={"session": SESSION_ID})

    assert res.status_code == 200
    assert res.text == "# Transcript\n"
    assert res.headers["content-type"].startswith("text/plain")


def test_transcript_endpoint_404_when_not_written_yet(rs, client) -> None:
    (Path(rs.OUT_DIR) / SESSION_ID).mkdir()
    assert client.get("/transcript", params={"session": SESSION_ID}).status_code == 404


@pytest.mark.parametrize(
    "session",
    ["..", "../secret", "../../etc", "/etc", "a/b", "..%2f..", "", ".hidden/..", "20261108-140300/.."],
)
def test_transcript_endpoint_rejects_ids_that_escape_the_sessions_dir(rs, client, tmp_path, session) -> None:
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "transcript.md").write_text("geheim", encoding="utf-8")
    (tmp_path / "transcript.md").write_text("geheim", encoding="utf-8")

    res = client.get("/transcript", params={"session": session})

    assert res.status_code in (400, 404)
    assert "geheim" not in res.text


def test_transcript_endpoint_rejects_symlink_out_of_sessions_dir(rs, client, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "transcript.md").write_text("geheim", encoding="utf-8")
    (Path(rs.OUT_DIR) / "linked").symlink_to(outside, target_is_directory=True)

    res = client.get("/transcript", params={"session": "linked"})

    assert res.status_code == 404
    assert "geheim" not in res.text


def test_transcript_endpoint_requires_a_session(rs, client) -> None:
    assert client.get("/transcript").status_code == 400


def test_transcript_endpoint_is_localhost_only(rs) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    (session / "transcript.md").write_text("x", encoding="utf-8")
    with TestClient(rs.app, base_url=BASE, client=("203.0.113.9", 50000)) as remote:
        assert remote.get("/transcript", params={"session": SESSION_ID}).status_code == 403
        assert remote.post("/reveal", headers=GOOD_ORIGIN, json={"session": SESSION_ID}).status_code == 403


def test_reveal_rejects_ids_that_escape_the_sessions_dir(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    calls: list = []
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: calls.append(a))

    for session in ("..", "../x", "/etc"):
        assert client.post("/reveal", headers=GOOD_ORIGIN, json={"session": session}).status_code in (400, 404)
    assert calls == []


def test_reveal_runs_open_r_on_the_transcript(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    (session / "transcript.md").write_text("x", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(rs.subprocess, "run", lambda cmd, **k: calls.append(cmd))

    res = client.post("/reveal", headers=GOOD_ORIGIN, json={"session": SESSION_ID})

    assert res.status_code == 200
    assert calls == [["open", "-R", str((session / "transcript.md").resolve())]]


def test_reveal_is_unavailable_off_macos(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: False)
    (Path(rs.OUT_DIR) / SESSION_ID).mkdir()
    assert client.post("/reveal", headers=GOOD_ORIGIN, json={"session": SESSION_ID}).status_code == 404


def _refused_websocket(client, headers) -> None:
    with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)):
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.send_json({"action": "start", "mic": None})
            ws.receive_json()


def test_websocket_with_foreign_origin_is_refused_and_cannot_record(rs, client) -> None:
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "https://127.0.0.1:8780", "null"):
        _refused_websocket(client, {"origin": origin, **WS_HOST})
    assert rs._state["engine"] is None
    assert rs._clients == set()


def test_websocket_allows_localhost_origin_and_no_origin(rs, client) -> None:
    for headers in ({"origin": f"http://localhost:{PORT}"}, {"origin": BASE}, {}):
        headers = {**headers, **WS_HOST}
        with client.websocket_connect("/ws", headers=headers) as ws:
            assert ws.receive_json()["type"] == "state"


@pytest.mark.parametrize("path", ["/", "/transcript?session=" + SESSION_ID])
def test_foreign_host_is_refused_for_get_routes(rs, path) -> None:
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    (session / "transcript.md").write_text("x", encoding="utf-8")
    with TestClient(rs.app, base_url="http://rebind.example:8780", client=("127.0.0.1", 50000)) as c:
        assert c.get(path).status_code == 400


def test_foreign_host_is_refused_for_reveal_and_websocket(rs, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    (Path(rs.OUT_DIR) / SESSION_ID).mkdir()
    with TestClient(rs.app, base_url="http://rebind.example:8780", client=("127.0.0.1", 50000)) as c:
        res = c.post("/reveal", headers=GOOD_ORIGIN, json={"session": SESSION_ID})
        assert res.status_code == 400
        _refused_websocket(c, {"host": "rebind.example:8780"})
    assert rs._state["engine"] is None


def test_localhost_host_is_accepted(rs) -> None:
    with TestClient(rs.app, base_url=f"http://localhost:{PORT}", client=("127.0.0.1", 50000)) as c:
        assert c.get("/").status_code == 200


def test_reveal_rejects_foreign_origin(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    (Path(rs.OUT_DIR) / SESSION_ID).mkdir()
    calls: list = []
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: calls.append(a))
    res = client.post("/reveal", headers=EVIL_ORIGIN, json={"session": SESSION_ID})
    assert res.status_code == 403
    assert calls == []


def test_reveal_rejects_non_json_content_type(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    (Path(rs.OUT_DIR) / SESSION_ID).mkdir()
    calls: list = []
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: calls.append(a))
    body = json.dumps({"session": SESSION_ID})
    for content_type in ("text/plain", "application/x-www-form-urlencoded", None):
        headers = {"content-type": content_type} if content_type else {}
        res = client.post("/reveal", headers=headers, content=body)
        assert res.status_code == 415
    assert calls == []


def test_reveal_without_origin_header_is_allowed(rs, client, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_can_reveal", lambda: True)
    session = Path(rs.OUT_DIR) / SESSION_ID
    session.mkdir()
    monkeypatch.setattr(rs.subprocess, "run", lambda *a, **k: None)
    assert client.post("/reveal", json={"session": SESSION_ID}).status_code == 200


@pytest.mark.audio_fixture
def test_real_whisper_cpp_transcribes_a_recorded_session(rs, tmp_path) -> None:
    """Needs a built whisper.cpp binary and a model from .env plus a real session."""
    sessions = sorted(p for p in (REPO_ROOT / "data" / "sessions").glob("*") if (p / "self.wav").exists())
    if not sessions:
        pytest.skip("no recorded session available")
    target = tmp_path / sessions[-1].name
    target.mkdir()
    for wav in sessions[-1].glob("*.wav"):
        (target / wav.name).write_bytes(wav.read_bytes())

    path = rs.transcribe_session_dir(target)

    assert path.read_text(encoding="utf-8").startswith("# ")
