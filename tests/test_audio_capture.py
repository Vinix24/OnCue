import io
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.capture import (
    MEETING_APP_CANDIDATES,
    AudioConfig,
    AudioTeeStream,
    DualAudioCapture,
    MicStream,
    _find_pid_exact,
    _find_pid_fuzzy,
    check_device_health,
    find_input_device,
    get_default_input_device,
    get_default_output_device,
    managed_audio_stream,
    resolve_meeting_app_target,
)


class _FakeInputStream:
    def __init__(self, *, callback, **kwargs):  # noqa: ANN001
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


def test_micstream_drops_oldest_on_overflow(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(InputStream=_FakeInputStream)
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    config = AudioConfig(sample_rate=16000, channels=1, dtype="float32", chunk_size=4, capture_method="mic")
    stream = MicStream(config, queue_maxsize=2)
    stream.start()

    assert stream._stream is not None  # noqa: SLF001
    callback = stream._stream.callback  # type: ignore[attr-defined]  # noqa: SLF001

    callback(np.array([[1.0], [2.0], [3.0], [4.0]], dtype=np.float32), 4, None, None)
    callback(np.array([[5.0], [6.0], [7.0], [8.0]], dtype=np.float32), 4, None, None)
    callback(np.array([[9.0], [10.0], [11.0], [12.0]], dtype=np.float32), 4, None, None)

    # Queue maxsize=2 means we should keep only the last two frames.
    first = stream.read()
    second = stream.read()
    third = stream.read()

    assert first is not None and np.allclose(first[:, 0], [5.0, 6.0, 7.0, 8.0])
    assert second is not None and np.allclose(second[:, 0], [9.0, 10.0, 11.0, 12.0])
    assert third is None

    stream.stop()


def test_audioteestream_reads_pcm_and_drops_oldest(monkeypatch, tmp_path) -> None:
    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")

    monkeypatch.setattr(cap, "_find_pid", lambda name: 123)

    # Create 3 chunks of int16 PCM, but queue holds only 2.
    c1 = (np.array([1, 2, 3, 4], dtype=np.int16)).tobytes()
    c2 = (np.array([5, 6, 7, 8], dtype=np.int16)).tobytes()
    c3 = (np.array([9, 10, 11, 12], dtype=np.int16)).tobytes()
    stdout = io.BytesIO(c1 + c2 + c3)

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = io.BytesIO(b"")

        def terminate(self) -> None:  # noqa: D401
            return

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr(
        cap.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProc(),  # noqa: ARG005
    )

    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        dtype="float32",
        chunk_size=4,
        capture_method="audiotee",
        target_process="Microsoft Teams",
        audiotee_path=str(audiotee_path),
    )
    stream = AudioTeeStream(config, queue_maxsize=2)
    stream.start()

    # Wait briefly for the daemon thread to finish reading.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if stream._reader_thread is None or not stream._reader_thread.is_alive():  # noqa: SLF001
            break
        time.sleep(0.01)

    f1 = stream.read()
    f2 = stream.read()
    f3 = stream.read()

    assert f1 is not None and np.allclose(f1[:, 0], np.array([5, 6, 7, 8], dtype=np.float32) / 32768.0)
    assert f2 is not None and np.allclose(f2[:, 0], np.array([9, 10, 11, 12], dtype=np.float32) / 32768.0)
    assert f3 is None

    stream.stop()


def test_dual_audio_capture_factory(monkeypatch, tmp_path) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(InputStream=_FakeInputStream)
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)
    monkeypatch.setattr(cap, "_find_pid", lambda name: 123)

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")

    monkeypatch.setattr(
        cap.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(stdout=io.BytesIO(b""), stderr=io.BytesIO(b""), terminate=lambda: None, wait=lambda timeout=None: 0),  # noqa: E501, ANN001
    )

    config = AudioConfig(capture_method="audiotee", target_process="zoom.us", audiotee_path=str(audiotee_path))
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, AudioTeeStream)


def test_blackhole_stream_drops_oldest_on_overflow(monkeypatch) -> None:
    import sales_copilot.audio.blackhole as bh

    fake_sd = SimpleNamespace(InputStream=_FakeInputStream)
    monkeypatch.setattr(bh, "_import_sounddevice", lambda: fake_sd)

    config = AudioConfig(sample_rate=16000, channels=1, dtype="float32", chunk_size=4, capture_method="blackhole")
    stream = BlackHoleStream(config, queue_maxsize=2, device_name="BlackHole 2ch")
    stream.start()

    assert stream._stream is not None  # noqa: SLF001
    callback = stream._stream.callback  # type: ignore[attr-defined]  # noqa: SLF001

    callback(np.array([[1.0], [2.0], [3.0], [4.0]], dtype=np.float32), 4, None, None)
    callback(np.array([[5.0], [6.0], [7.0], [8.0]], dtype=np.float32), 4, None, None)
    callback(np.array([[9.0], [10.0], [11.0], [12.0]], dtype=np.float32), 4, None, None)

    first = stream.read()
    second = stream.read()
    third = stream.read()

    assert first is not None and np.allclose(first[:, 0], [5.0, 6.0, 7.0, 8.0])
    assert second is not None and np.allclose(second[:, 0], [9.0, 10.0, 11.0, 12.0])
    assert third is None

    stream.stop()


def test_dual_audio_capture_blackhole_factory(monkeypatch) -> None:
    import sales_copilot.audio.blackhole as bh

    fake_sd = SimpleNamespace(InputStream=_FakeInputStream)
    monkeypatch.setattr(bh, "_import_sounddevice", lambda: fake_sd)
    monkeypatch.setattr(bh, "BlackHoleStream", BlackHoleStream)

    config = AudioConfig(capture_method="blackhole")
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, BlackHoleStream)


def test_find_input_device_matches_name(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(
        query_devices=lambda: [
            {"name": "MacBook Speakers", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
            {"name": "ULT WEAR", "max_input_channels": 1, "max_output_channels": 2},
        ]
    )
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    device = find_input_device("blackhole")

    assert device is not None
    assert device["index"] == 1


def test_default_devices_use_sounddevice_indices(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(
        query_devices=lambda: [
            {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Multi-Output Device", "max_input_channels": 0, "max_output_channels": 2},
        ],
        default=SimpleNamespace(device=(0, 1)),
    )
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    assert get_default_input_device()["name"] == "Mic"  # type: ignore[index]
    assert get_default_output_device()["name"] == "Multi-Output Device"  # type: ignore[index]


def test_check_device_health_detects_signal(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(
        query_devices=lambda: [
            {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
        ],
        default=SimpleNamespace(device=(0, 0)),
        rec=lambda frames, samplerate, channels, device, dtype: np.full((frames, channels), 0.01, dtype=np.float32),
        wait=lambda: None,
    )
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    has_audio, level = check_device_health("BlackHole 2ch")

    assert has_audio is True
    assert level == pytest.approx(0.01, rel=1e-3)


def test_check_device_health_returns_false_for_missing_device(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    fake_sd = SimpleNamespace(query_devices=lambda: [], default=SimpleNamespace(device=(-1, -1)))
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    has_audio, level = check_device_health("BlackHole 2ch")

    assert has_audio is False
    assert level == 0.0


def test_managed_audio_stream_context_starts_and_stops() -> None:
    class _FakeStream:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def read(self) -> None:
            return None

    stream = _FakeStream()
    with managed_audio_stream(stream) as active:
        assert active is stream
        assert stream.started is True
        assert stream.stopped is False

    assert stream.stopped is True


# --- Meeting-app auto-detection (resolve_meeting_app_target) ---------------


def test_resolve_meeting_app_target_single_match(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    monkeypatch.setattr(cap, "_find_pid_exact", lambda name: 555 if name == "Microsoft Teams" else None)

    name, pid, running = resolve_meeting_app_target()

    assert name == "Microsoft Teams"
    assert pid == 555
    assert running == ["Microsoft Teams"]


def test_resolve_meeting_app_target_none_running(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    monkeypatch.setattr(cap, "_find_pid_exact", lambda name: None)

    name, pid, running = resolve_meeting_app_target()

    assert name is None
    assert pid is None
    assert running == []


def test_resolve_meeting_app_target_ambiguous(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    monkeypatch.setattr(
        cap,
        "_find_pid_exact",
        lambda name: 1 if name in {"Google Chrome", "zoom.us"} else None,
    )

    name, pid, running = resolve_meeting_app_target()

    assert name is None
    assert pid is None
    assert running == ["Google Chrome", "zoom.us"]


def test_meeting_app_candidates_priority_order() -> None:
    assert MEETING_APP_CANDIDATES == ("Google Chrome", "Microsoft Teams", "zoom.us")


# --- DualAudioCapture: tap_all-minus-telephony vloer -------------------------
#
# The audiotee prospect stream taps the whole system-output mix (tap_all=True)
# instead of a single resolved process. A narrow single-process tap misses
# meeting audio rendered by helper subprocesses (e.g. Google Meet inside a
# Chrome renderer), so DualAudioCapture no longer resolves a target process or
# falls back to BlackHole on ambiguity for the audiotee path -- tap_all is the
# fixed floor regardless of what resolve_meeting_app_target() would report.
# Telephony (avconferenced) stays excluded via TelephonyTapGuard on every tier.


def test_factory_audiotee_always_taps_whole_system(monkeypatch) -> None:
    import sales_copilot.audio.capture as cap

    # _find_pid_exact must have no bearing on the audiotee path any more.
    monkeypatch.setattr(cap, "_find_pid_exact", lambda name: 777 if name == "zoom.us" else None)

    config = AudioConfig(capture_method="audiotee", target_process=None)
    warnings: list[dict] = []
    mic, system = DualAudioCapture(config).create(on_warning=warnings.append)

    assert isinstance(mic, MicStream)
    assert isinstance(system, AudioTeeStream)
    assert system._tap_all is True  # noqa: SLF001
    assert system.device_label == "audiotee:all"
    assert warnings == []


def test_factory_audiotee_ignores_target_process_and_detection_ambiguity(monkeypatch) -> None:
    """Whether target_process is set, unset, or detection is ambiguous, the
    factory always taps the whole system -- it never falls back to BlackHole
    or resolves a single process for the audiotee path any more."""

    import sales_copilot.audio.capture as cap

    monkeypatch.setattr(
        cap,
        "_find_pid_exact",
        lambda name: 1 if name in {"Google Chrome", "Microsoft Teams"} else None,
    )

    for target_process in (None, "Slack"):
        config = AudioConfig(capture_method="audiotee", target_process=target_process)
        warnings: list[dict] = []
        mic, system = DualAudioCapture(config).create(on_warning=warnings.append)

        assert isinstance(mic, MicStream)
        assert isinstance(system, AudioTeeStream)
        assert system._tap_all is True  # noqa: SLF001
        assert warnings == []


def test_factory_audiotee_excludes_telephony_in_free_tier(monkeypatch) -> None:
    import sales_copilot.audio.telephony_guard as tg

    class _FreePolicy:
        def allows(self, _feature_id: str) -> bool:
            return False

    monkeypatch.setattr(tg, "get_feature_policy", lambda: _FreePolicy())

    config = AudioConfig(capture_method="audiotee", target_process=None)
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, AudioTeeStream)
    assert system._exclude_processes == ("avconferenced",)  # noqa: SLF001


def test_factory_audiotee_does_not_exclude_telephony_on_pro_tier(monkeypatch) -> None:
    import sales_copilot.audio.telephony_guard as tg

    class _ProPolicy:
        def allows(self, _feature_id: str) -> bool:
            return True

    monkeypatch.setattr(tg, "get_feature_policy", lambda: _ProPolicy())

    config = AudioConfig(capture_method="audiotee", target_process=None)
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, AudioTeeStream)
    assert system._exclude_processes == ()  # noqa: SLF001


# --- AudioTeeStream: never-silent startup failures --------------------------


def test_audiotee_missing_binary_warns_without_crash(tmp_path) -> None:
    config = AudioConfig(
        capture_method="audiotee",
        target_process="Google Chrome",
        audiotee_path=str(tmp_path / "does-not-exist"),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)

    stream.start()  # must not raise

    assert stream.read() is None
    assert len(warnings) == 1
    assert warnings[0]["stream"] == "prospect"
    assert "niet gevonden" in warnings[0]["message"]

    stream.stop()


def test_audiotee_missing_process_warns_without_crash(monkeypatch, tmp_path) -> None:
    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: None)

    config = AudioConfig(
        capture_method="audiotee",
        target_process="zoom.us",
        audiotee_path=str(audiotee_path),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)

    stream.start()  # must not raise

    assert stream.read() is None
    assert len(warnings) == 1
    assert "zoom.us" in warnings[0]["message"]

    stream.stop()


def test_audiotee_setup_failure_emits_actionable_permission_warning(monkeypatch, tmp_path) -> None:
    """A subprocess that exits before delivering audio signals a failed Core Audio
    tap setup (most commonly a missing macOS process-tap permission). The stream
    must not raise and must surface one clear, actionable warning instead of
    failing silently."""

    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 4242)

    stderr_text = (
        b'{"type":"error","data":{"message":"Failed to setup audio tap",'
        b'"context":{"error":"tapCreationFailed(-4400)"}}}\n'
    )

    class _FakeFailingProc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"")  # EOF immediately: no audio ever produced
            self.stderr = io.BytesIO(stderr_text)

        def terminate(self) -> None:
            return

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 1

        def kill(self) -> None:
            return

    monkeypatch.setattr(cap.subprocess, "Popen", lambda *args, **kwargs: _FakeFailingProc())  # noqa: ARG005

    config = AudioConfig(
        capture_method="audiotee",
        target_process="Google Chrome",
        audiotee_path=str(audiotee_path),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)

    stream.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and not warnings:
        time.sleep(0.01)

    assert stream.read() is None
    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    assert warnings[0]["stream"] == "prospect"
    assert "Systeeminstellingen" in warnings[0]["message"]
    assert "Audio-opname" in warnings[0]["message"]
    assert "tapCreationFailed" in warnings[0]["message"]

    stream.stop()


def test_audiotee_healthy_tap_never_emits_setup_failure_warning(monkeypatch, tmp_path) -> None:
    """A tap that actually delivers audio must never trigger the setup-failure path."""

    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 123)

    pcm = (np.array([1, 2, 3, 4], dtype=np.int16)).tobytes()

    class _FakeHealthyProc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(pcm)
            self.stderr = io.BytesIO(b"")

        def terminate(self) -> None:
            return

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr(cap.subprocess, "Popen", lambda *args, **kwargs: _FakeHealthyProc())  # noqa: ARG005

    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        chunk_size=4,
        capture_method="audiotee",
        target_process="Microsoft Teams",
        audiotee_path=str(audiotee_path),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)
    stream.start()

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if stream._reader_thread is None or not stream._reader_thread.is_alive():  # noqa: SLF001
            break
        time.sleep(0.01)

    assert stream.chunks_received == 1
    assert warnings == []

    stream.stop()


def test_audiotee_tap_all_excludes_processes(monkeypatch, tmp_path) -> None:
    """tap_all with exclude_processes must pass --exclude-processes to AudioTee."""

    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 42 if name == "avconferenced" else 7)

    popen_calls: list[list[str]] = []

    class _FakeProc:
        def __init__(self, args: list[str]) -> None:
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            popen_calls.append(args)

        def terminate(self) -> None:
            return

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr(
        cap.subprocess,
        "Popen",
        lambda *args, **kwargs: _FakeProc(list(args[0])),  # noqa: ARG005
    )

    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        chunk_size=4,
        capture_method="audiotee",
        audiotee_path=str(audiotee_path),
    )
    stream = AudioTeeStream(config, tap_all=True, exclude_processes=("avconferenced",))
    stream.start()
    stream.stop()

    assert len(popen_calls) == 1
    args = popen_calls[0]
    assert "--exclude-processes" in args
    idx = args.index("--exclude-processes")
    assert args[idx + 1] == "42"


# ---------------------------------------------------------------------------
# Cross-platform process check (Fix E: pgrep → tasklist on Windows)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    """Minimal subprocess.CompletedProcess stub for check_output mocking."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout

    @property
    def stdout(self) -> bytes:
        return self._stdout


def test_find_pid_exact_pgrep_success(monkeypatch) -> None:
    """Exact match returns the first PID from pgrep on POSIX."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        assert args[:2] == ["pgrep", "-x"]
        return b"4242\n"

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="darwin"))
    assert _find_pid_exact("zoom.us") == 4242


def test_find_pid_exact_pgrep_not_found(monkeypatch) -> None:
    """Exact match returns None when pgrep exits non-zero."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="darwin"))
    assert _find_pid_exact("nonexistent") is None


def test_find_pid_exact_pgrep_not_found_via_filenotfound(monkeypatch) -> None:
    """Exact match returns None when pgrep is not installed (FileNotFoundError)."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="darwin"))
    assert _find_pid_exact("anything") is None


def test_find_pid_fuzzy_pgrep_success(monkeypatch) -> None:
    """Fuzzy match returns the first PID from pgrep -f on POSIX."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        assert args[:2] == ["pgrep", "-f"]
        return b"7777\n1234\n"

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="darwin"))
    assert _find_pid_fuzzy("chrome") == 7777


def test_find_pid_win32_exact_without_exe(monkeypatch) -> None:
    """Exact match on Windows appends .exe when the name lacks it."""

    calls: list[list[str]] = []

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        calls.append(list(args))
        cmd = " ".join(args)
        if "zoom.us.exe" in cmd:
            return b'"zoom.us.exe","5555","Console","1","12,345 K"\r\n'
        return b""

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_exact("zoom.us") == 5555
    assert len(calls) >= 2  # tried without .exe (empty), then with .exe


def test_find_pid_win32_exact_with_exe(monkeypatch) -> None:
    """Exact match on Windows does not double-append .exe."""

    calls: list[list[str]] = []

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        calls.append(list(args))
        return b'"chrome.exe","1234","Console","1","12,345 K"\r\n'

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_exact("chrome.exe") == 1234
    assert len(calls) == 1


def test_find_pid_win32_exact_not_found(monkeypatch) -> None:
    """Exact match on Windows returns None when no process matches."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        return b"INFO: No tasks are running which match the specified criteria.\r\n"

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_exact("nonexistent") is None


def test_find_pid_win32_fuzzy(monkeypatch) -> None:
    """Fuzzy match on Windows filters the full process list by substring."""

    tasklist_output = (
        b'"System Idle Process","0","Services","0","8 K"\r\n'
        b'"chrome.exe","1234","Console","1","123,456 K"\r\n'
        b'"notepad.exe","5678","Console","1","12,345 K"\r\n'
    )

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        return tasklist_output

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_fuzzy("chrome") == 1234


def test_find_pid_win32_fuzzy_not_found(monkeypatch) -> None:
    """Fuzzy match on Windows returns None when no process matches the substring."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        return b'"notepad.exe","5678","Console","1","12,345 K"\r\n'

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_fuzzy("chrome") is None


def test_find_pid_win32_tasklist_not_found(monkeypatch) -> None:
    """On Windows, FileNotFoundError from missing tasklist is caught gracefully."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        raise FileNotFoundError("tasklist")

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="win32"))
    assert _find_pid_exact("anything") is None
    assert _find_pid_fuzzy("anything") is None


def test_find_pid_posix_file_not_found_is_caught(monkeypatch) -> None:
    """On POSIX, FileNotFoundError from a missing pgrep is caught gracefully."""

    def _fake_check_output(args, **kwargs) -> bytes:  # noqa: ANN003
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr("sales_copilot.audio.capture.subprocess.check_output", _fake_check_output)
    monkeypatch.setattr("sales_copilot.audio.capture.sys", SimpleNamespace(platform="darwin"))
    assert _find_pid_exact("anything") is None
    assert _find_pid_fuzzy("anything") is None
