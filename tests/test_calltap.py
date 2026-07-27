"""Tests for the telephony call-tap prospect stream (PROSPECT_SOURCE=audiotee_call)."""

from __future__ import annotations

import asyncio
import io
import threading
import time

import numpy as np

from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.calltap import CallTapStream
from sales_copilot.audio.capture import (
    AudioConfig,
    DualAudioCapture,
    MicStream,
    check_streams_liveness,
    resolve_prospect_source,
)


class _FakeProc:
    """A subprocess stand-in: delivers ``data`` then blocks until terminated.

    Mirrors a live AudioTee tap whose stdout pipe stays open (delivering
    silence) until the process is killed, at which point ``read`` returns EOF.
    """

    def __init__(self, data: bytes, stderr: bytes = b"") -> None:
        self._buf = io.BytesIO(data)
        self.stderr = io.BytesIO(stderr)
        self._closed = threading.Event()
        self.stdout = self

    def read(self, size: int) -> bytes:
        chunk = self._buf.read(size)
        if chunk:
            return chunk
        while not self._closed.is_set():
            time.sleep(0.005)
        return b""

    def terminate(self) -> None:
        self._closed.set()

    def kill(self) -> None:
        self._closed.set()

    def wait(self, timeout=None) -> int:  # noqa: ANN001
        return 0


def _config(tmp_path, **overrides) -> AudioConfig:
    audiotee = tmp_path / "audiotee"
    audiotee.write_text("fake")
    base = dict(
        sample_rate=16000,
        channels=1,
        dtype="float32",
        chunk_size=4,
        capture_method="blackhole",
        prospect_source="audiotee_call",
        call_process_name="avconferenced",
        audiotee_path=str(audiotee),
    )
    base.update(overrides)
    return AudioConfig(**base)


# --- resolve_prospect_source ------------------------------------------------


def test_resolve_prospect_source_defaults_to_blackhole() -> None:
    assert resolve_prospect_source(None, None) == "blackhole"
    assert resolve_prospect_source("blackhole", {}) == "blackhole"


def test_resolve_prospect_source_env_audiotee_call() -> None:
    assert resolve_prospect_source("audiotee_call", None) == "audiotee_call"


def test_resolve_prospect_source_start_call_overrides_env() -> None:
    # start_call config wins over the env value.
    assert (
        resolve_prospect_source("blackhole", {"prospect_source": "audiotee_call"})
        == "audiotee_call"
    )
    assert (
        resolve_prospect_source("audiotee_call", {"prospect_source": "blackhole"})
        == "blackhole"
    )


def test_resolve_prospect_source_invalid_falls_back() -> None:
    assert resolve_prospect_source("garbage", None) == "blackhole"
    assert resolve_prospect_source("blackhole", {"prospect_source": "nope"}) == "blackhole"


# --- factory ----------------------------------------------------------------


def test_factory_selects_calltap_for_audiotee_call(tmp_path) -> None:
    config = _config(tmp_path)
    policy = type("_Policy", (), {"allows": lambda self, feature_id: True})()
    mic, system = DualAudioCapture(config, feature_policy=policy).create()
    assert isinstance(mic, MicStream)
    assert isinstance(system, CallTapStream)


def test_factory_blocks_calltap_without_pro(tmp_path) -> None:
    config = _config(tmp_path)
    policy = type("_Policy", (), {"allows": lambda self, feature_id: False})()
    mic, system = DualAudioCapture(config, feature_policy=policy).create()
    assert isinstance(mic, MicStream)
    assert isinstance(system, BlackHoleStream)


def test_factory_default_blackhole_unchanged(tmp_path, monkeypatch) -> None:
    import sales_copilot.audio.blackhole as bh

    monkeypatch.setattr(bh, "BlackHoleStream", BlackHoleStream)
    config = _config(tmp_path, prospect_source="blackhole", capture_method="blackhole")
    mic, system = DualAudioCapture(config).create()
    assert isinstance(mic, MicStream)
    assert isinstance(system, BlackHoleStream)


# --- PID resolution failure -------------------------------------------------


def test_calltap_missing_process_warns_without_crash(tmp_path, monkeypatch) -> None:
    import sales_copilot.audio.calltap as ct

    monkeypatch.setattr(ct, "_find_pid", lambda name: None)
    warnings: list[dict] = []
    stream = CallTapStream(_config(tmp_path), on_warning=warnings.append)

    # Must not raise even though avconferenced is absent.
    stream.start()

    assert stream._proc is None  # noqa: SLF001
    assert stream._supervisor is None  # noqa: SLF001
    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    assert warnings[0]["stream"] == "prospect"
    assert "avconferenced" in warnings[0]["message"]
    stream.stop()


def test_calltap_missing_binary_warns_without_crash(tmp_path, monkeypatch) -> None:
    import sales_copilot.audio.calltap as ct

    monkeypatch.setattr(ct, "_find_pid", lambda name: 4242)
    warnings: list[dict] = []
    config = AudioConfig(
        capture_method="blackhole",
        prospect_source="audiotee_call",
        audiotee_path=str(tmp_path / "does-not-exist"),
    )
    stream = CallTapStream(config, on_warning=warnings.append)
    stream.start()

    assert stream._proc is None  # noqa: SLF001
    assert len(warnings) == 1
    assert "niet gevonden" in warnings[0]["message"]


# --- subprocess delivers chunks ---------------------------------------------


def test_calltap_reads_pcm_and_drops_oldest(tmp_path, monkeypatch) -> None:
    import sales_copilot.audio.calltap as ct

    monkeypatch.setattr(ct, "_find_pid", lambda name: 123)

    c1 = np.array([1, 2, 3, 4], dtype=np.int16).tobytes()
    c2 = np.array([5, 6, 7, 8], dtype=np.int16).tobytes()
    c3 = np.array([9, 10, 11, 12], dtype=np.int16).tobytes()
    proc = _FakeProc(c1 + c2 + c3)
    monkeypatch.setattr(ct.subprocess, "Popen", lambda *a, **k: proc)  # noqa: ARG005

    stream = CallTapStream(_config(tmp_path), queue_maxsize=2)
    stream.start()

    deadline = time.time() + 1.0
    while time.time() < deadline and stream.chunks_received < 3:
        time.sleep(0.01)

    assert stream.chunks_received == 3
    f1 = stream.read()
    f2 = stream.read()
    f3 = stream.read()
    # Queue holds 2: oldest dropped, so last two chunks survive.
    assert f1 is not None and np.allclose(f1[:, 0], np.array([5, 6, 7, 8], dtype=np.float32) / 32768.0)
    assert f2 is not None and np.allclose(f2[:, 0], np.array([9, 10, 11, 12], dtype=np.float32) / 32768.0)
    assert f3 is None

    stream.stop()


# --- subprocess exit -> restart-once then warning ---------------------------


def test_calltap_restart_once_then_warns_on_repeat_exit(tmp_path, monkeypatch) -> None:
    import sales_copilot.audio.calltap as ct

    monkeypatch.setattr(ct, "_find_pid", lambda name: 123)

    spawned: list[int] = []

    class _EofProc(_FakeProc):
        """Subprocess that exits immediately (empty stdout -> instant EOF)."""

        def read(self, size: int) -> bytes:
            return b""

    def _popen(*args, **kwargs):  # noqa: ANN001, ARG001
        spawned.append(1)
        return _EofProc(b"")

    monkeypatch.setattr(ct.subprocess, "Popen", _popen)

    warnings: list[dict] = []
    stream = CallTapStream(_config(tmp_path), on_warning=warnings.append)
    stream.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and not warnings:
        time.sleep(0.01)

    # First exit triggers one restart (2 spawns), second exit gives up -> warning.
    assert len(spawned) >= 2
    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    stream.stop()


# --- liveness integration ---------------------------------------------------


def test_calltap_covered_by_liveness_check(tmp_path) -> None:
    # A call-tap that received no audio (tap open before the call connects)
    # must be flagged by the shared liveness check, same as any other stream.
    stream = CallTapStream(_config(tmp_path))
    assert stream.chunks_received == 0
    assert stream.device_label == "call-tap:avconferenced"

    broadcast: list[dict] = []

    async def _capture(payload: dict) -> None:
        broadcast.append(payload)

    flagged = asyncio.run(
        check_streams_liveness([(stream, "prospect")], _capture, timeout=0.01)
    )

    assert flagged == ["prospect"]
    assert broadcast and broadcast[0]["type"] == "audio_warning"
    assert broadcast[0]["stream"] == "prospect"
