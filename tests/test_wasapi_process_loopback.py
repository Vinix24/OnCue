"""Tests for the opt-in Windows process-loopback route (ADR-008 point 7).

Mocked at the COM seam: ``ProcessLoopbackStream`` takes a ``client_factory``
and the Windows build comes from ``_windows_build``. No Windows host needed.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest

import sales_copilot.audio.capture as cap
import sales_copilot.audio.wasapi_process as wp
from sales_copilot.audio.capture import AudioConfig, DualAudioCapture
from sales_copilot.audio.recorder_engine import RecorderEngine
from sales_copilot.audio.tap_health import TapSignalState
from sales_copilot.audio.wasapi import WasapiLoopbackStream
from sales_copilot.audio.wasapi_process import ProcessLoopbackStream, build_windows_prospect_stream


def _config(**overrides) -> AudioConfig:
    base = dict(sample_rate=16000, channels=1, dtype="float32", chunk_size=4)
    base.update(overrides)
    return AudioConfig(**base)


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _FakeClient:
    def __init__(self, blocks=None, volume=(1.0, False), open_error=None, read_error=None) -> None:
        self.blocks = list(blocks or [])
        self.volume = volume
        self.open_error = open_error
        self.read_error = read_error
        self.opened_with = None
        self.closed = False
        self.threads: list[int] = []

    def open(self, sample_rate, pid):
        self.threads.append(threading.get_ident())
        if self.open_error:
            raise self.open_error
        self.opened_with = (sample_rate, pid)

    def read(self):
        self.threads.append(threading.get_ident())
        if self.read_error:
            raise self.read_error
        return self.blocks.pop(0) if self.blocks else None

    def app_volume(self):
        return self.volume

    def close(self):
        self.threads.append(threading.get_ident())
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_handler_registry():
    wp._LIVE_HANDLERS.clear()
    yield
    wp._LIVE_HANDLERS.clear()


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(wp.sys, "platform", "win32")
    monkeypatch.setattr("sales_copilot.audio.wasapi.sys.platform", "win32")
    monkeypatch.setattr(wp, "_windows_build", lambda: wp.MIN_WINDOWS_BUILD)


def _stream(client, warnings=None, **cfg):
    return ProcessLoopbackStream(
        _config(**cfg),
        client_factory=lambda: client,
        on_warning=(warnings.append if warnings is not None else None),
        empty_pull_sleep_s=0.001,
    )


# --- route selection ---------------------------------------------------------


def test_route_endpoint_is_default_and_unchanged() -> None:
    assert type(build_windows_prospect_stream(_config())) is WasapiLoopbackStream
    assert type(build_windows_prospect_stream(_config(windows_capture_route="endpoint"))) is WasapiLoopbackStream


def test_route_process_builds_process_stream() -> None:
    assert isinstance(build_windows_prospect_stream(_config(windows_capture_route="Process")), ProcessLoopbackStream)


def test_unknown_route_warns_and_uses_endpoint(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        stream = build_windows_prospect_stream(_config(windows_capture_route="bogus"))
    assert type(stream) is WasapiLoopbackStream
    assert "bogus" in caplog.text


def test_dual_capture_site_picks_class_from_route(monkeypatch) -> None:
    monkeypatch.setattr(cap.sys, "platform", "win32")
    _, endpoint = DualAudioCapture(AudioConfig(capture_method="wasapi")).create()
    _, process = DualAudioCapture(AudioConfig(capture_method="wasapi", windows_capture_route="process")).create()
    assert type(endpoint) is WasapiLoopbackStream
    assert isinstance(process, ProcessLoopbackStream)


def test_recorder_engine_site_picks_class_from_route(monkeypatch) -> None:
    monkeypatch.setattr(cap.sys, "platform", "win32")

    def prospect(route):
        monkeypatch.setenv("WINDOWS_CAPTURE_ROUTE", route)
        return dict(RecorderEngine(capture_method="wasapi")._build_streams())["prospect"]

    assert type(prospect("endpoint")) is WasapiLoopbackStream
    assert isinstance(prospect("process"), ProcessLoopbackStream)
    monkeypatch.delenv("WINDOWS_CAPTURE_ROUTE")
    assert type(dict(RecorderEngine(capture_method="wasapi")._build_streams())["prospect"]) is WasapiLoopbackStream


# --- degrade, never raise ----------------------------------------------------


def test_degrades_below_minimum_build(monkeypatch, windows) -> None:
    monkeypatch.setattr(wp, "_windows_build", lambda: wp.MIN_WINDOWS_BUILD - 1)
    client, warnings = _FakeClient(), []
    stream = _stream(client, warnings)
    stream.start()
    assert len(warnings) == 1 and str(wp.MIN_WINDOWS_BUILD) in warnings[0]["message"]
    assert stream.tap_health().state is TapSignalState.NO_TAP
    assert client.opened_with is None
    stream.stop()


def test_unreadable_build_is_treated_as_too_old(monkeypatch, windows) -> None:
    monkeypatch.setattr(wp, "_windows_build", lambda: 0)
    stream = _stream(_FakeClient(), (w := []))
    stream.start()
    assert stream.tap_health().state is TapSignalState.NO_TAP and len(w) == 1


def test_degrades_on_activation_failure(windows) -> None:
    client, warnings = _FakeClient(open_error=wp.ProcessLoopbackError("boom")), []
    stream = _stream(client, warnings)
    stream.start()
    assert len(warnings) == 1 and "boom" in warnings[0]["message"]
    assert stream.tap_health().state is TapSignalState.NO_TAP
    assert client.closed
    stream.stop()


def test_degrades_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr("sales_copilot.audio.wasapi.sys.platform", "linux")
    warnings: list = []
    stream = _stream(_FakeClient(), warnings)
    stream.start()
    assert len(warnings) == 1
    assert stream.tap_health().state is TapSignalState.NO_TAP


def test_read_error_stops_visibly(windows) -> None:
    warnings: list = []
    stream = _stream(_FakeClient(read_error=OSError("gone")), warnings)
    stream.start()
    assert _wait_until(lambda: bool(warnings))
    stream.stop()
    assert stream.tap_health().state is TapSignalState.NO_TAP


# --- audio path --------------------------------------------------------------


def test_downmixes_stereo_to_mono_and_chunks(windows) -> None:
    left, right = np.full(8, 0.5, dtype=np.float32), np.full(8, -0.1, dtype=np.float32)
    client = _FakeClient(blocks=[np.stack([left, right], axis=1)])
    stream = _stream(client)
    stream.start()
    assert client.opened_with[0] == 16000
    assert _wait_until(lambda: stream.chunks_received >= 2)
    stream.stop()
    first = stream.read()
    assert first.shape == (4, 1)
    assert np.allclose(first, 0.2)
    assert stream.real_frames_received >= 2
    assert stream.device_label == "wasapi:process-loopback"


def test_open_read_close_share_one_reader_thread(windows) -> None:
    client = _FakeClient(blocks=[np.ones((8, 2), dtype=np.float32)])
    stream = _stream(client)
    stream.start()
    assert _wait_until(lambda: stream.chunks_received >= 2)
    stream.stop()
    assert _wait_until(lambda: client.closed)
    assert len(set(client.threads)) == 1
    assert client.threads[0] != threading.get_ident()


def test_silent_capture_reaches_tap_health(windows) -> None:
    stream = _stream(_FakeClient(blocks=[np.zeros((64, 2), dtype=np.float32)] * 20))
    stream.start()
    assert _wait_until(lambda: stream.chunks_received >= 20)
    stream.stop()
    health = stream.tap_health()
    assert health.signal_chunks == 0 and health.zero_run_chunks >= 20
    assert stream.real_frames_received == 0


# --- app-volume check ----------------------------------------------------------


@pytest.mark.parametrize(
    ("volume", "warns"),
    [
        ((0.01, False), True),
        ((0.09, False), True),
        ((1.0, True), True),
        ((0.1, False), False),
        ((1.0, False), False),
        (None, False),
    ],
)
def test_app_volume_threshold(windows, volume, warns) -> None:
    warnings: list = []
    stream = _stream(_FakeClient(volume=volume), warnings)
    stream.start()
    stream.stop()
    assert len(warnings) == (1 if warns else 0)
    if warns and volume[0] < 1.0:
        assert f"{volume[0]:.0%}" in warnings[0]["message"] and "volume mixer" in warnings[0]["message"]


def test_unreadable_app_volume_does_not_break_the_tap(windows) -> None:
    client = _FakeClient()
    client.app_volume = lambda: (_ for _ in ()).throw(wp.ProcessLoopbackError("no volume"))
    stream = _stream(client, (w := []))
    stream.start()
    stream.stop()
    assert w == []
    assert client.opened_with is not None


# --- ctypes COM plumbing (runs on any platform with a fake vtable) -------------


def test_vcall_dispatches_through_the_vtable_and_raises_on_failed_hresult() -> None:
    import ctypes

    seen = []
    ok = wp._functype(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)(lambda _this, n: seen.append(n) or 0)
    bad = wp._functype(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint)(lambda _this, n: -2147467259)
    vtbl = (ctypes.c_void_p * 2)(*(ctypes.cast(f, ctypes.c_void_p).value for f in (ok, bad)))
    obj = ctypes.c_void_p(ctypes.addressof(vtbl))
    iface = ctypes.c_void_p(ctypes.addressof(obj))
    assert wp._vcall(iface, 0, "ok", ctypes.c_uint(7)) == 0
    assert seen == [7]
    with pytest.raises(wp.ProcessLoopbackError, match="0x80004005"):
        wp._vcall(iface, 1, "bad", ctypes.c_uint(1))
    assert wp._vcall(iface, 1, "bad", ctypes.c_uint(1), check=False) < 0


def test_completion_handler_answers_agile_object_and_rejects_unknown_iids() -> None:
    import ctypes

    handler = wp._CompletionHandler()
    out = (ctypes.c_void_p * 1)()
    agile = wp._GUID.of("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
    assert handler._qi(5, ctypes.pointer(agile), out) == 0 and out[0] == 5
    other = wp._GUID.of("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
    assert handler._qi(5, ctypes.pointer(other), out) == wp._E_NOINTERFACE
    handler._completed(None, None)
    assert handler.done.is_set()


# --- handler lifetime, activation, PCM format ----------------------------------


class _FakeCom:
    """A COM object whose vtable slots are Python callables (all take ``this`` + 6 ignored args)."""

    def __init__(self, slots: dict | None = None, size: int = 20) -> None:
        import ctypes

        self.calls: list[int] = []
        fn = wp._functype(ctypes.c_long, *([ctypes.c_void_p] * 7))

        def make(index):
            def call(_this, a, b, c, d, e, f):
                self.calls.append(index)
                handler = (slots or {}).get(index)
                if handler:
                    handler(a, b, c, d, e, f)
                return 0

            return fn(call)

        self._fns = [make(i) for i in range(size)]
        self._vtbl = (ctypes.c_void_p * size)(*(ctypes.cast(f, ctypes.c_void_p).value for f in self._fns))
        self._obj = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._obj))


def _put(address: int, ctype, value) -> None:
    ctype.from_address(address).value = value


def _fire_completion(handler_pointer: int) -> None:
    import ctypes

    wp._vcall(ctypes.c_void_p(handler_pointer), 3, "ActivateCompleted", ctypes.c_void_p())


def _audio_client_fake(extra: dict | None = None) -> _FakeCom:
    """An IAudioClient fake whose GetService hands out a (retained) generic service object."""

    import ctypes

    service = _FakeCom()
    audio = _FakeCom({14: lambda _iid, out, *_: _put(out, ctypes.c_void_p, service.pointer.value), **(extra or {})})
    audio.service = service
    return audio


def _fake_activation(monkeypatch, *, result: int = 0, complete_inline: bool, audio=None):
    """Patch ``_activate_async``; returns the operation COM fake and the captured handler pointers."""

    import ctypes

    audio = audio or _audio_client_fake()
    keep = [audio]

    def get_result(res, unknown, *_):
        _put(res, ctypes.c_long, result)
        _put(unknown, ctypes.c_void_p, audio.pointer.value)

    operation = _FakeCom({3: get_result})
    pointers: list[int] = []

    def fake_activate(_variant, handler_pointer):
        pointers.append(handler_pointer)
        if complete_inline:
            _fire_completion(handler_pointer)
        return operation.pointer

    monkeypatch.setattr(wp, "_activate_async", fake_activate)
    return operation, pointers, keep


def test_late_completion_after_timeout_finds_handler_alive(monkeypatch) -> None:
    import gc

    monkeypatch.setattr(wp, "_ACTIVATE_TIMEOUT_SECONDS", 0.01)
    operation, pointers, _keep = _fake_activation(monkeypatch, complete_inline=False)
    with pytest.raises(wp.ProcessLoopbackError, match="timed out"):
        wp._ProcessLoopbackClient().open(16000, 1)
    gc.collect()
    assert pointers[0] in wp._LIVE_HANDLERS  # still retained after the timeout
    assert operation.calls == [2]  # operation interface released on the timeout path
    _fire_completion(pointers[0])  # the late native callback must not crash or raise
    assert pointers[0] not in wp._LIVE_HANDLERS
    assert wp._LIVE_HANDLERS == {}


def test_operation_released_on_failed_activation_result(monkeypatch) -> None:
    operation, _pointers, _keep = _fake_activation(monkeypatch, result=-2147467259, complete_inline=True)
    with pytest.raises(wp.ProcessLoopbackError, match="0x80004005"):
        wp._ProcessLoopbackClient().open(16000, 1)
    assert operation.calls == [3, 2]
    assert wp._LIVE_HANDLERS == {}


def test_operation_released_on_successful_activation(monkeypatch) -> None:
    operation, _pointers, _keep = _fake_activation(monkeypatch, complete_inline=True)
    client = wp._ProcessLoopbackClient()
    client.open(16000, 1)
    assert operation.calls == [3, 2]
    assert wp._LIVE_HANDLERS == {}
    client.close()


def test_failed_activate_call_unregisters_the_handler(monkeypatch) -> None:
    def boom(_variant, _pointer):
        raise wp.ProcessLoopbackError("ActivateAudioInterfaceAsync failed")

    monkeypatch.setattr(wp, "_activate_async", boom)
    with pytest.raises(wp.ProcessLoopbackError):
        wp._ProcessLoopbackClient().open(16000, 1)
    assert wp._LIVE_HANDLERS == {}


def test_handler_refcount_controls_retention() -> None:
    handler = wp._CompletionHandler()
    assert handler.pointer in wp._LIVE_HANDLERS
    assert handler._add_ref() == 2
    handler._completed(None, None)  # drops the pending-activation reference
    assert handler.pointer in wp._LIVE_HANDLERS  # Windows still holds its own reference
    assert handler._release() == 0
    assert handler.pointer not in wp._LIVE_HANDLERS
    assert handler._release() == 0  # an extra Release never goes negative


def test_initialize_requests_16_bit_pcm_for_the_autoconvert_flag(monkeypatch) -> None:

    seen = {}

    def initialize(mode, flags, duration, period, fmt, session):
        seen["flags"] = flags
        wf = wp._WaveFormat.from_address(fmt)
        seen["fmt"] = (wf.tag, wf.channels, wf.rate, wf.bits, wf.align)

    audio = _audio_client_fake({3: initialize})
    _fake_activation(monkeypatch, complete_inline=True, audio=audio)
    wp._ProcessLoopbackClient().open(16000, 1)
    assert seen["fmt"] == (1, 2, 16000, 16, 4)  # WAVE_FORMAT_PCM, stereo, 16-bit
    assert seen["flags"] & 0x80000000  # AUTOCONVERTPCM is set, now with a PCM request


def test_read_converts_int16_pcm_to_float32() -> None:
    import ctypes

    pcm = np.array([[16384, -16384], [32767, -32768]], dtype="<i2")
    raw = ctypes.create_string_buffer(pcm.tobytes(), pcm.nbytes)
    state = {"sizes": [2, 0]}

    def next_size(out, *_):
        _put(out, ctypes.c_uint, state["sizes"].pop(0))

    def get_buffer(data, frames, flags, *_):
        _put(data, ctypes.c_void_p, ctypes.addressof(raw))
        _put(frames, ctypes.c_uint, 2)
        _put(flags, ctypes.c_uint, 0)

    capture = _FakeCom({5: next_size, 3: get_buffer})
    client = wp._ProcessLoopbackClient()
    client._capture = capture.pointer
    block = client.read()
    assert block.dtype == np.float32 and block.shape == (2, 2)
    assert np.allclose(block, [[0.5, -0.5], [32767 / 32768, -1.0]])
    assert capture.calls == [5, 3, 4, 5]


def test_read_silent_flag_gives_zeros() -> None:
    import ctypes

    sizes = [3, 0]
    capture = _FakeCom(
        {
            5: lambda out, *_: _put(out, ctypes.c_uint, sizes.pop(0)),
            3: lambda data, frames, flags, *_: (
                _put(data, ctypes.c_void_p, 0),
                _put(frames, ctypes.c_uint, 3),
                _put(flags, ctypes.c_uint, wp._BUFFERFLAGS_SILENT),
            ),
        }
    )
    client = wp._ProcessLoopbackClient()
    client._capture = capture.pointer
    assert not client.read().any()


# --- .env reaches the recorder (scripts/record_call.py never loads it) ---------


def test_env_file_route_reaches_recorder_engine(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("WINDOWS_CAPTURE_ROUTE=process\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WINDOWS_CAPTURE_ROUTE", "")  # registers cleanup of what load_env sets
    monkeypatch.delenv("WINDOWS_CAPTURE_ROUTE")
    monkeypatch.setattr(cap.sys, "platform", "win32")
    # record_call.py builds RecorderEngine with no env loading of its own
    engine = RecorderEngine(capture_method="wasapi")
    assert engine._config.windows_capture_route == "process"
    assert isinstance(dict(engine._build_streams())["prospect"], ProcessLoopbackStream)


def test_process_environment_wins_over_env_file(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("WINDOWS_CAPTURE_ROUTE=process\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WINDOWS_CAPTURE_ROUTE", "endpoint")
    assert RecorderEngine(capture_method="wasapi")._config.windows_capture_route == "endpoint"
