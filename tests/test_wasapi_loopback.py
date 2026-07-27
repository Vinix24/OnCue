"""Tests for the Windows WASAPI loopback prospect stream.

Fully mocked: no real ``soundcard`` package, no real device. Runs on the
macOS-14 CI by monkeypatching ``sys.platform`` to ``"win32"`` and the module's
lazy ``_import_soundcard()`` hook to return a fake ``soundcard`` module.
"""

from __future__ import annotations

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


class _FakeMicrophone:
    def __init__(self, recorder_cm: _FakeRecorderCM) -> None:
        self._recorder_cm = recorder_cm
        self.recorder_calls: list[dict] = []

    def recorder(self, *, samplerate: int, blocksize: int | None = None, **kwargs) -> _FakeRecorderCM:
        self.recorder_calls.append({"samplerate": samplerate, "blocksize": blocksize})
        return self._recorder_cm


class _FakeSpeaker:
    def __init__(self, id: str = "default-speaker-id") -> None:  # noqa: A002
        self.id = id


class _FakeSoundcardModule:
    def __init__(
        self,
        *,
        microphone: _FakeMicrophone | None = None,
        speaker_raises: bool = False,
        get_microphone_raises: bool = False,
    ) -> None:
        self._microphone = microphone
        self._speaker_raises = speaker_raises
        self._get_microphone_raises = get_microphone_raises

    def default_speaker(self) -> _FakeSpeaker | None:
        if self._speaker_raises:
            raise RuntimeError("no default speaker")
        return _FakeSpeaker()

    def get_microphone(self, id, include_loopback: bool = False) -> _FakeMicrophone:  # noqa: A002
        if self._get_microphone_raises:
            raise RuntimeError("no loopback device")
        assert include_loopback is True
        return self._microphone


def _install_fake_soundcard(monkeypatch, fake_sc: _FakeSoundcardModule) -> None:
    monkeypatch.setattr(wasapi.sys, "platform", "win32")
    monkeypatch.setattr(wasapi, "_import_soundcard", lambda: fake_sc)


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
