from __future__ import annotations

import io

import numpy as np
import pytest

from sales_copilot.audio.recorder_engine import RecorderEngine
from sales_copilot.audio.telephony_guard import ProcessWatcher
from sales_copilot.auth.license_format import FEATURE_CALLTAP


class _FakePolicy:
    def __init__(self, allowed: set[str] | None = None) -> None:
        self._allowed = allowed or set()

    def allows(self, feature_id: str) -> bool:
        return feature_id in self._allowed


class FakeStream:
    def __init__(self, frames: list[np.ndarray], *, fail_start: bool = False) -> None:
        self._frames = list(frames)
        self._fail_start = fail_start
        self.started = False
        self.stopped = False

    def start(self) -> None:
        if self._fail_start:
            raise RuntimeError("no device")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> np.ndarray | None:
        return self._frames.pop(0) if self._frames else None


def test_engine_records_and_reports_levels(tmp_path) -> None:
    loud = [np.full(1024, 0.5, dtype=np.float32) for _ in range(4)]
    quiet = [np.zeros(1024, dtype=np.float32) for _ in range(4)]
    prospect = FakeStream(loud)
    self_s = FakeStream(quiet)
    engine = RecorderEngine(
        out_dir=tmp_path,
        silence_seconds=2.0,
        streams={"prospect": prospect, "self": self_s},
    )

    directory = engine.start(run_loop=False)
    engine.pump(0.0)
    snap = engine.snapshot(0.0)
    by = {s.label: s for s in snap.streams}

    assert by["prospect"].started and by["self"].started
    assert abs(by["prospect"].display_db - (-6.02)) < 0.3  # 0.5 amplitude ≈ -6 dBFS
    assert not by["prospect"].silent

    # No further loud frames: after the silence window both fall silent.
    engine.pump(5.0)
    snap2 = engine.snapshot(5.0)
    assert {s.label for s in snap2.streams if s.silent} == {"prospect", "self"}

    path = engine.stop()
    assert path == directory
    assert (path / "prospect.wav").stat().st_size > 44  # past the WAV header
    assert (path / "self.wav").exists()
    assert prospect.stopped and self_s.stopped


def test_engine_marks_failed_stream_as_not_started(tmp_path) -> None:
    prospect = FakeStream([], fail_start=True)
    self_s = FakeStream([np.zeros(512, dtype=np.float32)])
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": prospect, "self": self_s},
    )

    engine.start(run_loop=False)
    snap = engine.snapshot(0.0)
    by = {s.label: s for s in snap.streams}

    assert by["prospect"].started is False
    assert by["self"].started is True
    engine.stop()


def test_engine_stop_is_idempotent(tmp_path) -> None:
    engine = RecorderEngine(out_dir=tmp_path, streams={"self": FakeStream([])})
    engine.start(run_loop=False)
    assert engine.stop() is not None
    assert engine.stop() is None


def _fake_audiotee(tmp_path) -> str:
    path = tmp_path / "audiotee"
    path.write_text("fake")
    return str(path)


def test_engine_falls_back_to_tap_all_for_telephony_in_free_tier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sales_copilot.audio.telephony_guard as tg

    monkeypatch.setattr(tg, "_find_pid", lambda name: 123 if name == "avconferenced" else None)
    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconferenced",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
        pid_watcher=ProcessWatcher("avconferenced"),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "audiotee:all"


def test_engine_uses_targeted_tap_for_telephony_in_pro_tier(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sales_copilot.audio.telephony_guard as tg

    monkeypatch.setattr(tg, "_find_pid", lambda name: 123 if name == "avconferenced" else None)
    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconferenced",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy({FEATURE_CALLTAP}),
        pid_watcher=ProcessWatcher("avconferenced"),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "audiotee:avconferenced"


def test_engine_ignores_tier_for_non_telephony_process(tmp_path) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="Google Chrome",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "audiotee:Google Chrome"


class _FakeAudioteeProc:
    def __init__(self) -> None:
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(b"")

    def terminate(self) -> None:
        return

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        return


def _capture_popen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    import sales_copilot.audio.capture as cap

    calls: list[list[str]] = []

    def _fake_popen(*args: str, **kwargs: object) -> _FakeAudioteeProc:  # noqa: ARG001
        calls.append(list(args[0]))
        return _FakeAudioteeProc()

    monkeypatch.setattr(cap.subprocess, "Popen", _fake_popen)
    return calls


def test_engine_free_tier_excludes_avconferenced_in_tap_all_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAIL-OPEN regression: a telephony target in the free tier must not fall
    back to an unfiltered tap_all that captures avconferenced."""

    import sales_copilot.audio.capture as cap
    import sales_copilot.audio.telephony_guard as tg

    popen_calls = _capture_popen(monkeypatch)
    monkeypatch.setattr(cap, "_find_pid", lambda name: 999 if name == "avconferenced" else None)
    monkeypatch.setattr(tg, "_find_pid", lambda name: 999 if name == "avconferenced" else None)

    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconferenced",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
        pid_watcher=ProcessWatcher("avconferenced"),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001
    prospect = streams["prospect"]

    prospect.start()
    try:
        assert prospect.device_label == "audiotee:all"
        assert len(popen_calls) == 1
        args = popen_calls[0]
        assert "--exclude-processes" in args
        exclude_idx = args.index("--exclude-processes")
        assert args[exclude_idx + 1] == "999"
        assert "--include-processes" not in args
    finally:
        prospect.stop()


def test_engine_free_tier_excludes_telephony_even_without_target(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-system tap in the free tier must still exclude telephony
    processes; otherwise a phone call would be captured by default."""

    import sales_copilot.audio.capture as cap
    import sales_copilot.audio.telephony_guard as tg

    popen_calls = _capture_popen(monkeypatch)
    monkeypatch.setattr(cap, "_find_pid", lambda name: 999 if name == "avconferenced" else None)
    monkeypatch.setattr(tg, "_find_pid", lambda name: 999 if name == "avconferenced" else None)

    engine = RecorderEngine(
        out_dir=tmp_path,
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001
    prospect = streams["prospect"]

    prospect.start()
    try:
        assert "--exclude-processes" in popen_calls[0]
        assert "999" in popen_calls[0]
    finally:
        prospect.stop()


def test_engine_toctou_exclusion_resolved_at_tap_start(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU regression: the guard must not free the target just because the
    PID was not running at the moment of the guard check. The exclusion must be
    enforced when AudioTee resolves the PID at tap-start."""

    import sales_copilot.audio.capture as cap
    import sales_copilot.audio.telephony_guard as tg

    popen_calls = _capture_popen(monkeypatch)

    calls: list[int] = []

    def _delayed_find_pid(name: str) -> int | None:
        if name != "avconferenced":
            return None
        calls.append(1)
        # First caller (the guard) sees no PID; later callers (tap-start) see it.
        return None if len(calls) == 1 else 777

    monkeypatch.setattr(cap, "_find_pid", _delayed_find_pid)
    monkeypatch.setattr(tg, "_find_pid", _delayed_find_pid)

    # Simulate an earlier point-in-time check (the old guard) that saw no PID.
    assert cap._find_pid("avconferenced") is None

    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconferenced",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
        pid_watcher=ProcessWatcher("avconferenced"),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001
    prospect = streams["prospect"]

    prospect.start()
    try:
        args = popen_calls[0]
        assert "--exclude-processes" in args
        assert "777" in args
        assert "--include-processes" not in args
    finally:
        prospect.stop()


def test_engine_wasapi_short_circuits_before_telephony_guard(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On win32/wasapi, _build_streams() must return WasapiLoopbackStream without
    ever calling the telephony guard's pgrep-based PID resolution -- pgrep does
    not exist on Windows and raises FileNotFoundError."""

    import sales_copilot.audio.capture as cap
    import sales_copilot.audio.telephony_guard as tg

    def _explode(name: str) -> int | None:  # pragma: no cover - must not be called
        raise FileNotFoundError(f"pgrep: command not found (simulated win32, looked up {name!r})")

    monkeypatch.setattr(tg, "_find_pid", _explode)
    monkeypatch.setattr(cap.sys, "platform", "win32")

    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconferenced",
        feature_policy=_FakePolicy(),
        pid_watcher=ProcessWatcher("avconferenced"),
        capture_method="wasapi",
    )
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "wasapi:loopback"
    assert streams["self"].device_label == "default"


def test_engine_wasapi_capture_method_normalizes_on_win32(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset capture_method on a simulated win32 platform normalizes to
    wasapi (via normalize_capture_method), so the short-circuit still fires
    without the caller having to know the platform default."""

    import sales_copilot.audio.recorder_engine as re_mod

    monkeypatch.setattr(re_mod.sys, "platform", "win32")

    engine = RecorderEngine(out_dir=tmp_path, streams=None)
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "wasapi:loopback"


def test_engine_macos_audiotee_selection_unchanged_with_explicit_capture_method(
    tmp_path,
) -> None:
    """capture_method='audiotee' on the (macOS) test platform keeps the existing
    AudioTee tap_all selection -- the wasapi wiring must not disturb it."""

    engine = RecorderEngine(
        out_dir=tmp_path,
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
        capture_method="audiotee",
    )
    streams = dict(engine._build_streams())  # noqa: SLF001

    assert streams["prospect"].device_label == "audiotee:all"


def test_engine_fuzzy_telephony_name_excludes_not_includes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FUZZY-NAME-BYPASS regression: "avconference" resolves via pgrep -f to
    the same PID as avconferenced. The guard must treat it as telephony and the
    fallback must exclude, not include, that PID."""

    import sales_copilot.audio.capture as cap
    import sales_copilot.audio.telephony_guard as tg

    popen_calls = _capture_popen(monkeypatch)

    def _fuzzy_find_pid(name: str) -> int | None:
        if "avconference" in name.lower():
            return 555
        return None

    monkeypatch.setattr(cap, "_find_pid", _fuzzy_find_pid)
    monkeypatch.setattr(tg, "_find_pid", _fuzzy_find_pid)

    engine = RecorderEngine(
        out_dir=tmp_path,
        prospect_process="avconference",
        audiotee_path=_fake_audiotee(tmp_path),
        feature_policy=_FakePolicy(),
    )
    streams = dict(engine._build_streams())  # noqa: SLF001
    prospect = streams["prospect"]

    prospect.start()
    try:
        args = popen_calls[0]
        assert "--exclude-processes" in args
        assert "555" in args
        assert "--include-processes" not in args
    finally:
        prospect.stop()


def _wav_frames(path) -> int:
    import wave

    with wave.open(str(path), "rb") as wf:
        return wf.getnframes()


def _stop_at(monkeypatch: pytest.MonkeyPatch, engine: RecorderEngine, elapsed_s: float) -> object:
    from sales_copilot.audio import recorder_engine

    start = engine._start_monotonic
    monkeypatch.setattr(recorder_engine.time, "monotonic", lambda: start + elapsed_s)
    return engine.stop()


def _metadata(directory) -> dict:
    import json

    return json.loads((directory / "metadata.json").read_text(encoding="utf-8"))


def _chunks(n: int) -> list[np.ndarray]:
    return [np.full(1024, 0.1, dtype=np.float32) for _ in range(n)]


def test_stop_pads_short_track_to_elapsed(tmp_path, monkeypatch) -> None:
    # 16 chunks = 1.024 s for prospect, 4 chunks for self; call lasted 1.024 s.
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream(_chunks(16)), "self": FakeStream(_chunks(4))},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    directory = _stop_at(monkeypatch, engine, 1.024)

    assert _wav_frames(directory / "prospect.wav") == 16 * 1024
    assert _wav_frames(directory / "self.wav") == 16 * 1024
    padding = _metadata(directory)["end_padding_ms"]
    assert padding["prospect"] == 0
    assert padding["self"] == 768


def test_stop_drains_remaining_frames_before_padding(tmp_path, monkeypatch) -> None:
    engine = RecorderEngine(out_dir=tmp_path, streams={"prospect": FakeStream(_chunks(16))})
    engine.start(run_loop=False)
    directory = _stop_at(monkeypatch, engine, 1.024)

    assert _wav_frames(directory / "prospect.wav") == 16 * 1024
    assert _metadata(directory)["end_padding_ms"]["prospect"] == 0


def test_steady_streams_get_no_padding(tmp_path, monkeypatch) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream(_chunks(8)), "self": FakeStream(_chunks(8))},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    directory = _stop_at(monkeypatch, engine, 8 * 1024 / 16000)

    assert _wav_frames(directory / "prospect.wav") == _wav_frames(directory / "self.wav") == 8 * 1024
    assert _metadata(directory)["end_padding_ms"] == {"prospect": 0, "self": 0}


def test_longer_track_is_not_trimmed(tmp_path, monkeypatch) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream(_chunks(16)), "self": FakeStream(_chunks(2))},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    directory = _stop_at(monkeypatch, engine, 0.5)

    assert _wav_frames(directory / "prospect.wav") == 16 * 1024
    assert _metadata(directory)["end_padding_ms"]["prospect"] == 0


def test_unstarted_stream_is_not_padded(tmp_path, monkeypatch) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream([], fail_start=True), "self": FakeStream(_chunks(4))},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    directory = _stop_at(monkeypatch, engine, 2.0)

    assert not (directory / "prospect.wav").exists()
    meta = _metadata(directory)
    assert "prospect" not in meta["end_padding_ms"]
    assert meta["end_padding_ms"]["self"] == 2000 - 256


def test_padding_is_logged_per_track(tmp_path, monkeypatch, caplog) -> None:
    engine = RecorderEngine(out_dir=tmp_path, streams={"self": FakeStream(_chunks(1))})
    engine.start(run_loop=False)
    engine.pump(0.0)
    with caplog.at_level("INFO", logger="sales_copilot.audio.recorder_engine"):
        _stop_at(monkeypatch, engine, 1.0)
    assert any("Padded track 'self'" in r.getMessage() for r in caplog.records)


def test_silent_started_stream_is_not_padded(tmp_path, monkeypatch) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream([]), "self": FakeStream(_chunks(4))},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    directory = _stop_at(monkeypatch, engine, 2.0)

    assert not (directory / "prospect.wav").exists()
    meta = _metadata(directory)
    assert meta["end_padding_ms"].get("prospect", 0) == 0
    assert meta["duration_ms_per_stream"].get("prospect", 0) == 0
    assert meta["end_padding_ms"]["self"] == 2000 - 256
    assert _wav_frames(directory / "self.wav") == 2 * 16000


def test_all_silent_started_streams_still_warn_zero_frames(tmp_path, monkeypatch, caplog) -> None:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream([]), "self": FakeStream([])},
    )
    engine.start(run_loop=False)
    engine.pump(0.0)
    with caplog.at_level("INFO", logger="sales_copilot.audio.recorder"):
        _stop_at(monkeypatch, engine, 2.0)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("zero audio frames" in r.getMessage() for r in warnings)


def test_pad_track_to_skips_track_without_real_audio(tmp_path) -> None:
    from sales_copilot.audio.recorder import AudioRecorder

    recorder = AudioRecorder("s", tmp_path)
    assert recorder.pad_track_to("self", 1000.0) == 0
    recorder.write_chunk("prospect", np.full(1000, 0.1, dtype=np.float32), 0)
    assert recorder.pad_track_to("prospect", 1000.0) == 1000 - round(1000 * 1000 / 16000)
    assert recorder.pad_track_to("prospect", 1000.0) == 0
