"""Tests for the Windows WASAPI loopback prospect stream.

Fully mocked: no real ``soundcard`` package, no real device. Runs on the
macOS-14 CI by monkeypatching ``sys.platform`` to ``"win32"`` and the module's
lazy ``_import_soundcard()`` hook to return a fake ``soundcard`` module.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

import sales_copilot.audio.wasapi as wasapi
from sales_copilot.audio.capture import AudioConfig, AudioStream
from sales_copilot.audio.wasapi import WasapiLoopbackStream


def _config(**overrides) -> AudioConfig:
    base = dict(sample_rate=16000, channels=1, dtype="float32", chunk_size=4)
    base.update(overrides)
    return AudioConfig(**base)


def _wait_until(predicate, *, timeout: float = 1.0, poll: float = 0.005) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


class _FakeRecorderCM:
    """Stand-in for ``soundcard``'s recorder context manager.

    ``record(numframes)`` pops queued blocks one at a time and then returns
    an empty ``(0, channels)`` array once exhausted, mirroring a live
    WASAPI-loopback endpoint that goes quiet once its canned audio ends.
    """

    def __init__(
        self,
        blocks: list[np.ndarray],
        *,
        samplerate: int,
        blocksize: int,
        raise_on_enter: bool = False,
        channels: int = 2,
    ) -> None:
        self._blocks = list(blocks)
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._raise_on_enter = raise_on_enter
        self._channels = channels
        self.entered = False
        self.exited = False

    def __enter__(self) -> _FakeRecorderCM:
        if self._raise_on_enter:
            raise RuntimeError("recorder open failed")
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        return False

    def record(self, numframes: int) -> np.ndarray:
        if self._blocks:
            return self._blocks.pop(0)
        return np.zeros((0, self._channels), dtype=np.float32)


class _ThreadTrackingRecorderCM(_FakeRecorderCM):
    """Like ``_FakeRecorderCM``, but records which OS thread touched it.

    ``thread_log`` collects the ``threading.get_ident()`` of whichever thread
    called ``__enter__`` (open), the first ``record()`` (read), and
    ``__exit__`` (close) -- the three points this dispatch's COM-apartment
    fix requires to happen on one and the same thread.
    """

    def __init__(self, *args, thread_log: dict[str, int], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._thread_log = thread_log

    def __enter__(self) -> _ThreadTrackingRecorderCM:
        self._thread_log["open"] = threading.get_ident()
        return super().__enter__()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._thread_log["close"] = threading.get_ident()
        return super().__exit__(exc_type, exc, tb)

    def record(self, numframes: int) -> np.ndarray:
        self._thread_log.setdefault("read", threading.get_ident())
        return super().record(numframes)


class _FakeOle32:
    """Stand-in for ``ctypes.windll.ole32``, tracking call thread identity.

    ``CoInitializeEx``/``CoUninitialize`` are assigned as instance attributes
    (bound to snake_case helpers) rather than defined as methods, purely so
    the fake's public surface can mirror the real, PascalCase COM API names
    without tripping the naming-convention linter.
    """

    def __init__(self, *, hr: int = 0) -> None:
        self.hr = hr
        self.init_calls: list[int] = []
        self.uninit_calls: list[int] = []
        self.CoInitializeEx = self._co_initialize_ex
        self.CoUninitialize = self._co_uninitialize

    def _co_initialize_ex(self, reserved: object, coinit: int) -> int:
        self.init_calls.append(threading.get_ident())
        return self.hr

    def _co_uninitialize(self) -> None:
        self.uninit_calls.append(threading.get_ident())


class _FakeMicrophone:
    def __init__(self, recorder_cm: _FakeRecorderCM) -> None:
        self._recorder_cm = recorder_cm
        self.recorder_calls: list[dict] = []

    def recorder(self, *, samplerate: int, blocksize: int | None = None, **kwargs) -> _FakeRecorderCM:
        self.recorder_calls.append({"samplerate": samplerate, "blocksize": blocksize})
        return self._recorder_cm


class _FakeSpeaker:
    def __init__(self, id: str = "default-speaker-id", name: str | None = None) -> None:  # noqa: A002
        self.id = id
        self.name = id if name is None else name


class _FakeSoundcardModule:
    def __init__(
        self,
        *,
        microphone: _FakeMicrophone | None = None,
        speaker_raises: bool = False,
        get_microphone_raises: bool = False,
        default_speaker_holder: list[_FakeSpeaker] | None = None,
        all_speakers_list: list[_FakeSpeaker] | None = None,
        microphones_by_id: dict[str, _FakeMicrophone] | None = None,
    ) -> None:
        self._microphone = microphone
        self._speaker_raises = speaker_raises
        self._get_microphone_raises = get_microphone_raises
        # A one-element list, mutated in place by tests that simulate the
        # operator switching Windows' default output device mid-call.
        self._default_speaker_holder = (
            default_speaker_holder if default_speaker_holder is not None else [_FakeSpeaker()]
        )
        self._all_speakers_list = (
            all_speakers_list if all_speakers_list is not None else list(self._default_speaker_holder)
        )
        self._microphones_by_id = microphones_by_id
        self.get_microphone_calls: list[str] = []

    def default_speaker(self) -> _FakeSpeaker | None:
        if self._speaker_raises:
            raise RuntimeError("no default speaker")
        return self._default_speaker_holder[0]

    def all_speakers(self) -> list[_FakeSpeaker]:
        return list(self._all_speakers_list)

    def get_microphone(self, id, include_loopback: bool = False) -> _FakeMicrophone:  # noqa: A002
        if self._get_microphone_raises:
            raise RuntimeError("no loopback device")
        assert include_loopback is True
        self.get_microphone_calls.append(id)
        if self._microphones_by_id is not None:
            return self._microphones_by_id[id]
        return self._microphone


class _RaisingNameSpeaker:
    """A speaker whose ``name`` raises, like an endpoint that vanishes mid-open.

    ``soundcard``'s ``_Device.name`` is not a stored attribute but a property
    that does a full COM round-trip (``_device_ptr`` -> ``OpenPropertyStore``
    -> ``GetValue``) and raises ``RuntimeError`` once the endpoint is gone --
    so it can fail *after* the recorder has already been opened.
    """

    def __init__(self, speaker_id: str = "vanishing-speaker") -> None:
        self.id = speaker_id

    @property
    def name(self) -> str:
        raise RuntimeError("endpoint disappeared")


class _BlockingRecorderCM(_FakeRecorderCM):
    """A recorder whose ``record()`` blocks until ``release`` is set.

    Models a reader thread wedged inside a driver call, so ``stop()``'s join
    times out while that thread is still alive and still owns the recorder.
    """

    def __init__(self, *args, release: threading.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._release = release
        self.record_entered = threading.Event()

    def record(self, numframes: int) -> np.ndarray:
        self.record_entered.set()
        self._release.wait(timeout=10.0)
        return super().record(numframes)


class _GatedSoundcardModule(_FakeSoundcardModule):
    """A soundcard fake whose ``default_speaker()`` blocks until released.

    Models the open sequence itself wedging on a driver call, which is what
    ``start()``'s bounded wait on the reader thread has to survive.
    """

    def __init__(self, *args, gate: threading.Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate
        self.open_entered = threading.Event()

    def default_speaker(self) -> _FakeSpeaker | None:
        self.open_entered.set()
        self._gate.wait(timeout=10.0)
        return super().default_speaker()


def _install_fake_soundcard(monkeypatch, fake_sc: _FakeSoundcardModule) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    monkeypatch.setattr(wasapi, "_import_soundcard", lambda: fake_sc)


def _start_returns_within(stream: WasapiLoopbackStream, timeout: float = 5.0) -> bool:
    """Run ``stream.start()`` off the main thread; report whether it returned.

    ``start()`` waits on the reader thread while holding ``_lock``, so a
    ``start()`` that never returns would hang the entire suite instead of
    failing one assertion. Running it on a throwaway daemon thread turns
    "hangs forever" into "this assertion fails".
    """

    done = threading.Event()

    def _run() -> None:
        try:
            stream.start()
        finally:
            done.set()

    threading.Thread(target=_run, name="wasapi-test-start", daemon=True).start()
    return done.wait(timeout)


# --- protocol conformance -----------------------------------------------------


def test_wasapi_stream_satisfies_audio_stream_protocol() -> None:
    stream = WasapiLoopbackStream(_config())
    assert isinstance(stream, AudioStream)
    assert stream.device_label == "wasapi:loopback"
    assert stream.chunks_received == 0
    assert stream.real_frames_received == 0
    stream.stop()


# --- degrade-not-raise paths ---------------------------------------------------


def test_non_windows_platform_degrades_with_warning(monkeypatch) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "darwin")
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    stream.start()

    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    assert warnings[0]["stream"] == "prospect"
    stream.stop()


def test_missing_soundcard_degrades_with_warning(monkeypatch) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "win32")

    def _raise_import() -> None:
        raise ImportError("no module named soundcard")

    monkeypatch.setattr(wasapi, "_import_soundcard", _raise_import)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    stream.start()

    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    stream.stop()


def test_no_default_speaker_degrades_with_warning(monkeypatch) -> None:
    fake_sc = _FakeSoundcardModule(speaker_raises=True)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    stream.start()

    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    stream.stop()


def test_get_microphone_failure_degrades_with_warning(monkeypatch) -> None:
    fake_sc = _FakeSoundcardModule(get_microphone_raises=True)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    stream.start()

    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    stream.stop()


def test_recorder_open_failure_degrades_with_warning(monkeypatch) -> None:
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8, raise_on_enter=True)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    stream.start()

    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    stream.stop()


def test_first_block_shape_mismatch_degrades_with_warning(monkeypatch) -> None:
    bad_block = np.zeros((4,), dtype=np.float32)  # 1-D: wrong shape contract
    recorder_cm = _FakeRecorderCM([bad_block], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append, silence_timeout_multiplier=1000.0)

    stream.start()
    assert _wait_until(lambda: len(warnings) > 0)

    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    stream.stop()


def test_device_removed_mid_stream_degrades_with_warning(monkeypatch) -> None:
    good_block = np.full((4, 2), 0.1, dtype=np.float32)
    recorder_cm = _FakeRecorderCM([good_block], samplerate=16000, blocksize=8)

    call_count = {"n": 0}
    original_record = recorder_cm.record

    def _record(numframes: int) -> np.ndarray:
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise RuntimeError("device removed")
        return original_record(numframes)

    recorder_cm.record = _record  # type: ignore[method-assign]
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append, silence_timeout_multiplier=1000.0)

    stream.start()
    assert _wait_until(lambda: len(warnings) > 0)

    assert len(warnings) == 1
    assert stream.chunks_received == 1  # the one good block delivered before the raise
    stream.stop()


# --- downmix + fixed-size chunking + RMS gating -------------------------------


def test_downmix_and_fixed_size_chunking_from_partial_pulls(monkeypatch) -> None:
    # Native 2-channel device, delivered as short/partial pulls that only
    # together add up to full chunk_size=4 frames per emitted chunk.
    blocks = [
        np.array([[0.2, 0.4]], dtype=np.float32),  # 1 frame -> mean 0.3
        np.array([[0.0, 0.0], [0.2, 0.2]], dtype=np.float32),  # 2 frames -> means 0.0, 0.2
        np.array([[1.0, 1.0]], dtype=np.float32),  # 1 frame -> mean 1.0 (completes chunk 1)
        np.full((4, 2), 0.5, dtype=np.float32),  # chunk 2: all 0.5
    ]
    recorder_cm = _FakeRecorderCM(blocks, samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)
    stream.start()

    assert _wait_until(lambda: stream.chunks_received >= 2)

    assert stream.chunks_received == 2
    assert stream.real_frames_received == 2  # both chunks carry RMS well above threshold

    frame1 = stream.read()
    frame2 = stream.read()
    assert frame1 is not None
    assert frame1.shape == (4, 1)
    assert frame1.dtype == np.float32
    assert frame2 is not None
    assert frame2.shape == (4, 1)
    np.testing.assert_allclose(frame1[:, 0], [0.3, 0.0, 0.2, 1.0], atol=1e-6)
    np.testing.assert_allclose(frame2[:, 0], [0.5, 0.5, 0.5, 0.5], atol=1e-6)

    stream.stop()


def test_real_frames_received_is_rms_gated(monkeypatch) -> None:
    quiet_block = np.full((4, 2), 0.0001, dtype=np.float32)  # RMS 0.0001 < threshold
    loud_block = np.full((4, 2), 0.01, dtype=np.float32)  # RMS 0.01 > threshold
    recorder_cm = _FakeRecorderCM([quiet_block, loud_block], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)
    stream.start()

    assert _wait_until(lambda: stream.chunks_received >= 2)

    assert stream.chunks_received == 2
    assert stream.real_frames_received == 1

    stream.stop()


# --- empty-pull backoff + synthesized silence ---------------------------------


def test_empty_pulls_backoff_and_synthesize_silence(monkeypatch) -> None:
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)  # always empty
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(
        _config(chunk_size=4),
        empty_pull_sleep_s=0.001,
        silence_timeout_multiplier=1.0,
    )
    stream.start()

    assert _wait_until(lambda: stream.chunks_received >= 3)

    assert stream.chunks_received >= 3
    assert stream.real_frames_received == 0

    frame = stream.read()
    assert frame is not None
    assert frame.shape == (4, 1)
    np.testing.assert_allclose(frame, 0.0)

    stream.stop()


# --- start/stop/read idempotency ----------------------------------------------


def test_start_stop_read_idempotency(monkeypatch) -> None:
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)

    stream.start()
    first_thread = stream._reader_thread  # noqa: SLF001
    assert first_thread is not None

    stream.start()  # second start() is a no-op while already running
    assert stream._reader_thread is first_thread  # noqa: SLF001
    assert len(mic.recorder_calls) == 1

    assert stream.read() is None  # empty queue, no crash

    stream.stop()
    stream.stop()  # idempotent stop, no crash

    assert stream.read() is None
    assert recorder_cm.exited is True


# --- AUDIO_WASAPI_ENDPOINT_NAME override ---------------------------------------


def test_endpoint_override_selects_named_speaker(monkeypatch) -> None:
    default_speaker = _FakeSpeaker(id="display-audio", name="CBA242Y - HD Audio Driver for Display Audio")
    headset_speaker = _FakeSpeaker(id="headset-id", name="Realtek USB Headset")
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(
        microphone=mic,
        default_speaker_holder=[default_speaker],
        all_speakers_list=[default_speaker, headset_speaker],
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    stream = WasapiLoopbackStream(
        _config(wasapi_endpoint_name="realtek"),
        silence_timeout_multiplier=1000.0,
    )

    stream.start()
    assert _wait_until(lambda: len(mic.recorder_calls) >= 1)

    assert fake_sc.get_microphone_calls == ["headset-id"]
    stream.stop()


def test_endpoint_override_absent_warns_once_and_falls_back(monkeypatch) -> None:
    default_speaker = _FakeSpeaker(id="display-audio", name="CBA242Y - HD Audio Driver for Display Audio")
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(
        microphone=mic,
        default_speaker_holder=[default_speaker],
        all_speakers_list=[default_speaker],
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(
        _config(wasapi_endpoint_name="nonexistent headset"),
        on_warning=warnings.append,
        silence_timeout_multiplier=1000.0,
    )

    stream.start()
    assert _wait_until(lambda: len(mic.recorder_calls) >= 1)

    assert len(warnings) == 1
    assert "nonexistent headset" in warnings[0]["message"]
    assert fake_sc.get_microphone_calls == ["display-audio"]
    stream.stop()


# --- default-endpoint drift -> re-attach ---------------------------------------


def test_default_endpoint_change_triggers_single_reattach(monkeypatch, caplog) -> None:
    speaker_a = _FakeSpeaker(id="speaker-a", name="Speaker A")
    speaker_b = _FakeSpeaker(id="speaker-b", name="Speaker B")
    holder = [speaker_a]
    recorder_a = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    recorder_b = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic_a = _FakeMicrophone(recorder_a)
    mic_b = _FakeMicrophone(recorder_b)
    fake_sc = _FakeSoundcardModule(
        default_speaker_holder=holder,
        all_speakers_list=[speaker_a, speaker_b],
        microphones_by_id={"speaker-a": mic_a, "speaker-b": mic_b},
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    caplog.set_level(logging.INFO, logger="sales_copilot.audio.wasapi")

    stream = WasapiLoopbackStream(
        _config(),
        silence_timeout_multiplier=1000.0,
        endpoint_poll_seconds=0.02,
    )
    stream.start()
    assert _wait_until(lambda: len(mic_a.recorder_calls) >= 1)

    holder[0] = speaker_b  # simulate the operator switching output device
    # Wait on the log line itself, not just the recorder swap: the swap (inside
    # _reattach's lock) and the INFO log (just after) are sequential in the
    # same reader thread, but asserting on the earlier of the two invites a
    # race against this thread if the reader gets descheduled in between.
    assert _wait_until(lambda: "re-attaching" in caplog.text, timeout=2.0)

    assert len(mic_b.recorder_calls) == 1
    assert caplog.text.count("re-attaching") == 1
    assert "Speaker A" in caplog.text
    assert "Speaker B" in caplog.text
    assert recorder_a.exited is True

    stream.stop()


def test_stable_default_endpoint_triggers_no_reattach(monkeypatch, caplog) -> None:
    speaker_a = _FakeSpeaker(id="speaker-a", name="Speaker A")
    recorder_a = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic_a = _FakeMicrophone(recorder_a)
    fake_sc = _FakeSoundcardModule(
        microphone=mic_a,
        default_speaker_holder=[speaker_a],
        all_speakers_list=[speaker_a],
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    caplog.set_level(logging.INFO, logger="sales_copilot.audio.wasapi")

    stream = WasapiLoopbackStream(
        _config(),
        silence_timeout_multiplier=1000.0,
        endpoint_poll_seconds=0.02,
    )
    stream.start()
    assert _wait_until(lambda: len(mic_a.recorder_calls) >= 1)

    time.sleep(0.15)  # several poll intervals at the 0.02s test cadence

    assert mic_a.recorder_calls == [{"samplerate": 16000, "blocksize": 8}]
    assert "re-attaching" not in caplog.text

    stream.stop()


def test_named_override_never_reattaches_on_default_change(monkeypatch, caplog) -> None:
    """An explicit, matched override is pinned -- a later default change must not displace it."""

    headset_speaker = _FakeSpeaker(id="headset-id", name="Realtek USB Headset")
    other_default = _FakeSpeaker(id="display-audio", name="CBA242Y - HD Audio Driver for Display Audio")
    holder = [other_default]
    recorder_headset = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic_headset = _FakeMicrophone(recorder_headset)
    fake_sc = _FakeSoundcardModule(
        default_speaker_holder=holder,
        all_speakers_list=[other_default, headset_speaker],
        microphones_by_id={"headset-id": mic_headset},
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    caplog.set_level(logging.INFO, logger="sales_copilot.audio.wasapi")

    stream = WasapiLoopbackStream(
        _config(wasapi_endpoint_name="realtek"),
        silence_timeout_multiplier=1000.0,
        endpoint_poll_seconds=0.02,
    )
    stream.start()
    assert _wait_until(lambda: len(mic_headset.recorder_calls) >= 1)

    holder[0] = _FakeSpeaker(id="third-device", name="Bluetooth Speaker")
    time.sleep(0.15)  # several poll intervals; a pinned override must ignore this

    assert mic_headset.recorder_calls == [{"samplerate": 16000, "blocksize": 8}]
    assert "re-attaching" not in caplog.text

    stream.stop()


# --- COM apartment-affinity fix (recorder open/read/close on one thread) ------
#
# Regression coverage for the Windows P0: soundcard's WASAPI recorder is a COM
# object, and COM interface pointers are apartment-bound to whichever thread
# creates them. The old code opened the recorder on start()'s caller thread and
# then read it from a separately spawned reader thread -- a cross-apartment COM
# violation that crashes natively (no Python traceback) on real Windows,
# roughly a second in, once the reader thread's first live read actually
# touches the COM pointer. The fix moves the recorder's entire lifecycle --
# open, read, periodic re-attach, and close -- onto the reader thread itself,
# and has that thread explicitly join the process's COM multithreaded
# apartment instead of relying on soundcard's own one-shot, import-thread-only
# CoInitializeEx call.


def test_recorder_opened_read_and_closed_on_same_thread(monkeypatch) -> None:
    """Open, first read, and close must all happen on the reader thread --
    never on the thread that calls start()/stop(). Fails on the old code
    (which opened on the caller thread) and passes on the fix."""

    thread_log: dict[str, int] = {}
    recorder_cm = _ThreadTrackingRecorderCM([], samplerate=16000, blocksize=8, thread_log=thread_log)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)
    caller_thread_id = threading.get_ident()

    stream.start()
    assert _wait_until(lambda: "open" in thread_log and "read" in thread_log)

    stream.stop()
    assert _wait_until(lambda: "close" in thread_log)

    assert thread_log["open"] == thread_log["read"] == thread_log["close"]
    assert thread_log["open"] != caller_thread_id


def test_recorder_open_failure_returns_synchronously_with_matching_tap_health(monkeypatch) -> None:
    """start() must still fail synchronously -- no hang, no dangling reader
    thread -- with the identical warning message and TapHealth attach-failed
    state the old open-on-caller-thread code produced."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8, raise_on_enter=True)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    started_at = time.monotonic()
    stream.start()
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0  # synchronous open: start() does not hang on the reader thread
    assert stream._reader_thread is None  # noqa: SLF001
    assert len(warnings) == 1
    assert warnings[0]["message"] == "Kon de WASAPI-loopback-recorder niet openen."

    health = stream.tap_health()
    assert health.attach_state.value == "attach_failed"
    assert health.detail == "recorder kon niet openen"
    assert health.warning_emitted is True

    stream.stop()  # idempotent no-op: nothing was ever attached


def test_reattach_failure_leaves_old_recorder_running_and_unclosed(monkeypatch, caplog) -> None:
    """If opening the new endpoint's recorder fails during a default-device
    drift re-attach, the still-working old recorder must not be closed or
    otherwise leaked -- the stream keeps running on the tap it already has,
    and only a subsequent stop() closes it."""

    speaker_a = _FakeSpeaker(id="speaker-a", name="Speaker A")
    speaker_b = _FakeSpeaker(id="speaker-b", name="Speaker B")
    holder = [speaker_a]
    recorder_a = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    recorder_b = _FakeRecorderCM([], samplerate=16000, blocksize=8, raise_on_enter=True)
    mic_a = _FakeMicrophone(recorder_a)
    mic_b = _FakeMicrophone(recorder_b)
    fake_sc = _FakeSoundcardModule(
        default_speaker_holder=holder,
        all_speakers_list=[speaker_a, speaker_b],
        microphones_by_id={"speaker-a": mic_a, "speaker-b": mic_b},
    )
    _install_fake_soundcard(monkeypatch, fake_sc)
    caplog.set_level(logging.WARNING, logger="sales_copilot.audio.wasapi")

    stream = WasapiLoopbackStream(
        _config(),
        silence_timeout_multiplier=1000.0,
        endpoint_poll_seconds=0.02,
    )
    stream.start()
    assert _wait_until(lambda: len(mic_a.recorder_calls) >= 1)

    holder[0] = speaker_b  # operator switches default output; the new endpoint's open will fail
    assert _wait_until(lambda: "re-attach to new default endpoint" in caplog.text, timeout=2.0)

    assert recorder_a.exited is False  # the working tap must not be torn down on a failed swap
    assert stream._recorder_cm is recorder_a  # noqa: SLF001
    assert "re-attaching" not in caplog.text  # the swap itself never completed

    stream.stop()
    assert recorder_a.exited is True  # only the normal stop() path closes it


# --- COM apartment join/leave (CoInitializeEx / CoUninitialize) ---------------


def test_com_initialize_returns_false_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "darwin")
    assert wasapi._com_initialize_mta() is False


def test_com_initialize_returns_false_when_ole32_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: None)
    assert wasapi._com_initialize_mta() is False


def test_com_initialize_joins_mta_and_uninitialize_balances_it(monkeypatch) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    fake_ole32 = _FakeOle32(hr=0)
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: fake_ole32)

    assert wasapi._com_initialize_mta() is True
    wasapi._com_uninitialize()

    assert fake_ole32.init_calls == [threading.get_ident()]
    assert fake_ole32.uninit_calls == [threading.get_ident()]


def test_com_initialize_skips_uninitialize_on_changed_mode(monkeypatch) -> None:
    """RPC_E_CHANGED_MODE means this thread already belongs to an apartment
    with a different concurrency model -- our call joined nothing, so nothing
    should be uninitialized for it."""

    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    rpc_e_changed_mode_signed = 0x80010106 - 2**32
    fake_ole32 = _FakeOle32(hr=rpc_e_changed_mode_signed)
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: fake_ole32)

    assert wasapi._com_initialize_mta() is False


def test_reader_thread_joins_and_leaves_com_mta_via_stream_lifecycle(monkeypatch) -> None:
    """End-to-end: starting and stopping the stream joins and leaves the COM
    MTA exactly once, on the reader thread -- not on the thread that calls
    start()/stop()."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    fake_ole32 = _FakeOle32(hr=0)
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: fake_ole32)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)
    caller_thread_id = threading.get_ident()

    stream.start()
    assert len(fake_ole32.init_calls) == 1  # start() blocks until open finishes, so this is synchronous

    stream.stop()

    assert fake_ole32.init_calls == [fake_ole32.uninit_calls[0]]
    assert fake_ole32.init_calls[0] != caller_thread_id


# --- reader-thread lifecycle: every open must be balanced by a close ----------
#
# Follow-up regressions found by the adversarial review of #224. The reader
# thread now owns the recorder's whole lifecycle, which means it is also the
# only place that can ever close it: stop() deliberately closes nothing, since
# that would be the cross-thread COM access this module exists to avoid. So
# every exit path out of the reader thread -- not just the one that reached the
# read loop -- has to close what it opened, and no second reader may ever be
# started while a previous one is still alive.


def test_open_failure_after_enter_still_closes_the_recorder(monkeypatch) -> None:
    """An exception between ``recorder_cm.__enter__()`` and the read loop must
    not leak the recorder. Before this fix the close only ran in a ``finally``
    wrapped around ``_read_loop()``, so a failure before the loop was entered
    kept the IAudioClient claimed until the process exited -- and ``stop()``
    no longer closes anything to make up for it."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic, default_speaker_holder=[_RaisingNameSpeaker()])
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config())
    stream.start()

    assert recorder_cm.entered is True  # the failure really is after the open
    assert stream._reader_thread is None  # noqa: SLF001
    assert _wait_until(lambda: recorder_cm.exited is True), "recorder opened but never closed"
    assert stream._recorder_cm is None  # noqa: SLF001

    stream.stop()
    assert recorder_cm.exited is True


def test_unexpected_open_failure_warns_and_marks_attach_failed(monkeypatch) -> None:
    """The generic ``except`` around the open is a sixth failure route, and it
    has to behave like the five named ones: an ``audio_warning`` for the
    dashboard and an ``attach_failed`` TapHealth state. Left on ``NOT_STARTED``
    the heartbeat stays silent too (``evaluate_tap_health`` returns None), so
    the tap fails to start with nothing anywhere saying why."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic, default_speaker_holder=[_RaisingNameSpeaker()])
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []

    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)
    stream.start()

    assert len(warnings) == 1
    assert warnings[0]["type"] == "audio_warning"
    assert warnings[0]["stream"] == "prospect"

    health = stream.tap_health()
    assert health.attach_state.value == "attach_failed"
    assert health.warning_emitted is True

    stream.stop()


def test_restart_is_refused_while_a_timed_out_reader_thread_is_still_alive(monkeypatch) -> None:
    """``stop()``'s join has a timeout, so it can return with the reader thread
    still alive and still owning the recorder. Releasing the thread slot there
    let the next ``start()`` clear the stop event and wake that old reader back
    up: two threads then read ``self._recorder``, and the old one eventually
    closes the *new* thread's recorder while leaking its own."""

    # raising=False so this test can also be run against the pre-fix module,
    # where the constant does not exist yet: it then reproduces the defect
    # itself (a second recorder opened) instead of an AttributeError.
    monkeypatch.setattr(wasapi, "_STOP_JOIN_TIMEOUT_SECONDS", 0.05, raising=False)
    release = threading.Event()
    recorder_cm = _BlockingRecorderCM([], samplerate=16000, blocksize=8, release=release)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    stream = WasapiLoopbackStream(_config(), silence_timeout_multiplier=1000.0)
    try:
        stream.start()
        old_thread = stream._reader_thread  # noqa: SLF001
        assert old_thread is not None
        assert recorder_cm.record_entered.wait(2.0)

        stream.stop()  # the join times out: the reader is wedged inside record()
        assert old_thread.is_alive()
        assert stream._stop_event.is_set()  # noqa: SLF001

        stream.start()  # must not wake the old reader, must not open a second one
        assert len(mic.recorder_calls) == 1
        assert stream._stop_event.is_set()  # noqa: SLF001
        assert stream._reader_thread is old_thread  # noqa: SLF001

        release.set()
        assert _wait_until(lambda: not old_thread.is_alive(), timeout=3.0)
        assert _wait_until(lambda: recorder_cm.exited is True, timeout=2.0)

        # Once the old reader is really gone the stream restarts cleanly.
        stream.start()
        assert stream._stop_event.is_set() is False  # noqa: SLF001
        assert len(mic.recorder_calls) == 2
    finally:
        release.set()
        stream.stop()


def test_com_initialize_reports_failure_on_a_failed_hresult(monkeypatch) -> None:
    """Only ``S_OK`` and ``S_FALSE`` join the apartment and must be balanced.
    Treating every HRESULT except ``RPC_E_CHANGED_MODE`` as success made a
    failed ``CoInitializeEx`` report "joined", after which the reader thread
    ran ``CoUninitialize()`` against an apartment refcount it never raised --
    which Microsoft documents as forbidden."""

    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    e_outofmemory = 0x8007000E
    e_invalidarg = 0x80070057
    for hresult in (e_outofmemory, e_invalidarg):
        fake_ole32 = _FakeOle32(hr=hresult - 2**32)  # as ctypes hands back a signed c_int
        monkeypatch.setattr(wasapi, "_ole32_handle", lambda ole32=fake_ole32: ole32)
        joined = wasapi._com_initialize_mta()
        assert joined is False, f"failed HRESULT 0x{hresult:08X} must not count as joined"


def test_com_initialize_treats_s_false_as_joined(monkeypatch) -> None:
    """``S_FALSE`` means COM was already up on this thread with the same
    concurrency model. The refcount still went up, so it still needs undoing."""

    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    fake_ole32 = _FakeOle32(hr=1)  # S_FALSE
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: fake_ole32)

    assert wasapi._com_initialize_mta() is True


def test_failed_com_join_is_never_uninitialized_over_the_stream_lifecycle(monkeypatch) -> None:
    """End-to-end counterpart: a reader thread whose COM join failed must leave
    the apartment refcount alone on the way out."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)
    fake_ole32 = _FakeOle32(hr=0x8007000E - 2**32)  # E_OUTOFMEMORY
    monkeypatch.setattr(wasapi, "_ole32_handle", lambda: fake_ole32)

    stream = WasapiLoopbackStream(_config(chunk_size=4), silence_timeout_multiplier=1000.0)
    stream.start()
    stream.stop()

    assert len(fake_ole32.init_calls) == 1
    assert fake_ole32.uninit_calls == []


def test_start_returns_when_the_com_join_raises(monkeypatch) -> None:
    """The COM join used to sit outside the reader thread's ``try``, so
    anything it raised skipped ``ready.set()`` entirely: ``start()`` then
    waited forever on that event *while holding ``_lock``*, wedging ``stop()``
    along with it. The wait is now bounded and the event is set in a
    ``finally``."""

    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _FakeSoundcardModule(microphone=mic)
    _install_fake_soundcard(monkeypatch, fake_sc)

    def _boom() -> bool:
        raise TypeError("CoInitializeEx returned something that is not an int")

    monkeypatch.setattr(wasapi, "_com_initialize_mta", _boom)
    warnings: list[dict] = []
    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)

    assert _start_returns_within(stream), "start() hung on the reader thread's ready event"

    assert stream._reader_thread is None  # noqa: SLF001
    assert stream._degraded is True  # noqa: SLF001
    assert len(warnings) == 1
    assert stream.tap_health().attach_state.value == "attach_failed"

    stream.stop()


def test_start_gives_up_when_the_open_never_signals_ready(monkeypatch) -> None:
    """Second belt for the same hang: if the open itself wedges inside a driver
    call, ``start()`` returns degraded instead of blocking forever. The wedged
    reader keeps its thread slot so a later ``start()`` cannot race a second
    reader against it, and the stop event is set so it tears its own recorder
    down as soon as the driver call returns."""

    # raising=False for the same reason as the restart test above; against the
    # pre-fix module this then fails on the unbounded wait itself.
    monkeypatch.setattr(wasapi, "_OPEN_READY_TIMEOUT_SECONDS", 0.05, raising=False)
    gate = threading.Event()
    recorder_cm = _FakeRecorderCM([], samplerate=16000, blocksize=8)
    mic = _FakeMicrophone(recorder_cm)
    fake_sc = _GatedSoundcardModule(microphone=mic, gate=gate)
    _install_fake_soundcard(monkeypatch, fake_sc)
    warnings: list[dict] = []

    stream = WasapiLoopbackStream(_config(), on_warning=warnings.append)
    try:
        assert _start_returns_within(stream, timeout=3.0), "start() hung on a wedged open"
        assert fake_sc.open_entered.is_set()
        assert stream._degraded is True  # noqa: SLF001
        assert len(warnings) == 1
        assert stream._stop_event.is_set()  # noqa: SLF001

        stream.start()  # no second reader while the wedged one is still alive
        assert mic.recorder_calls == []

        gate.set()
        assert _wait_until(lambda: recorder_cm.exited is True, timeout=3.0)
        assert len(mic.recorder_calls) == 1  # opened once, then torn down by the stop event
    finally:
        gate.set()
        stream.stop()
