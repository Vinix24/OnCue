"""Windows process-loopback prospect stream (opt-in fallback route, ADR-008 point 7).

``WINDOWS_CAPTURE_ROUTE=process`` records everything Windows plays except
OnCue's own process tree (``PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE``),
not bound to one render endpoint. It is plain ctypes (no new dependency):
``ActivateAudioInterfaceAsync`` on ``VAD\\Process_Loopback`` with a ctypes
completion handler, ``IAudioClient``/``IAudioCaptureClient`` through vtables.

``ProcessLoopbackStream`` subclasses ``WasapiLoopbackStream`` and keeps its
lifecycle: one COM-joined reader thread owns the client from open to close
(#224, #227), and every setup failure degrades (one warning, ``no_tap``)
instead of raising. Not yet tested on Windows hardware.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import AudioConfig
from .wasapi import WasapiLoopbackStream

logger = logging.getLogger(__name__)

ROUTE_ENDPOINT = "endpoint"
ROUTE_PROCESS = "process"

# Process loopback arrived with Windows build 20348 (the build Microsoft's
# ApplicationLoopback sample names as the minimum).
MIN_WINDOWS_BUILD = 20348
# Windows can park an app's capture volume at 1%; below this the tap hears nothing.
APP_VOLUME_FLOOR = 0.1

_ACTIVATE_TIMEOUT_SECONDS = 5.0
_VT_BLOB = 65
_PROCESS_LOOPBACK_TYPE = 1
_EXCLUDE_TARGET_TREE = 1
_STREAMFLAGS = 0x00020000 | 0x80000000 | 0x08000000  # LOOPBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
_BUFFERFLAGS_SILENT = 0x2
_E_NOINTERFACE = ctypes.c_long(0x80004002).value
_CHANNELS = 2


class ProcessLoopbackError(RuntimeError):
    """A COM call of the process-loopback client failed."""


class _GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_U16, _U32 = ctypes.c_ushort, ctypes.c_uint


class _WaveFormat(ctypes.Structure):  # WAVEFORMATEX, packed to 18 bytes
    _pack_ = 1
    _fields_ = [("tag", _U16), ("channels", _U16), ("rate", _U32), ("byte_rate", _U32)]
    _fields_ += [("align", _U16), ("bits", _U16), ("extra", _U16)]


class _ActivationParams(ctypes.Structure):  # AUDIOCLIENT_ACTIVATION_PARAMS (process loopback)
    _fields_ = [("activation_type", _U32), ("pid", _U32), ("mode", _U32)]


class _PropVariant(ctypes.Structure):  # PROPVARIANT holding a VT_BLOB
    _fields_ = [("vt", _U16), ("pad", _U16 * 3), ("size", ctypes.c_ulong), ("data", ctypes.c_void_p)]


_IID_AUDIO_CLIENT = "1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"
_IID_CAPTURE_CLIENT = "C8ADBD64-E71E-48a0-A4DE-185C395CD317"
_IID_SIMPLE_VOLUME = "87CE5498-68D6-44E5-9215-6DA47EF883D8"
_HANDLER_IIDS = {  # IUnknown, IAgileObject, IActivateAudioInterfaceCompletionHandler
    str(uuid.UUID(i))
    for i in (
        "00000000-0000-0000-C000-000000000046",
        "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90",
        "41D949AB-9862-444A-80F6-C261334DA5EB",
    )
}


def _functype(restype: Any, *argtypes: Any) -> Any:
    return getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(restype, *argtypes)


def _vcall(ptr: Any, index: int, what: str, *args: Any, check: bool = True) -> int:
    """Call vtable slot ``index`` of COM interface ``ptr``; raise on a failed HRESULT.

    ``args`` are ctypes instances (``byref``, ``c_uint(..)``), so no argtypes are needed.
    """

    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    argtypes = [ctypes.c_void_p if type(a).__name__ == "CArgObject" else type(a) for a in args]
    hr = int(_functype(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])(ptr, *args))
    if check and hr < 0:
        raise ProcessLoopbackError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return hr


def _windows_build() -> int:
    """Windows build number, 0 when it cannot be read (fails closed against the minimum)."""

    getter = getattr(sys, "getwindowsversion", None)
    if getter is None:
        return 0
    try:
        return int(getter().build)
    except Exception:
        return 0


# Handlers Windows may still call back. ``ActivateAudioInterfaceAsync`` can invoke the
# completion handler after our wait timed out, so the handler (with the ctypes callback
# objects and the vtable its COM pointer points into) must outlive ``open``. A handler
# stays here until its COM reference count reaches zero.
_LIVE_HANDLERS: dict[int, _CompletionHandler] = {}
_LIVE_HANDLERS_LOCK = threading.Lock()
# Handlers that just left the registry: the native thunk of the call that released the
# last reference is still on the stack, so keep the last few alive a little longer.
_RETIRED_HANDLERS: collections.deque[_CompletionHandler] = collections.deque(maxlen=8)


class _CompletionHandler:
    """ctypes ``IActivateAudioInterfaceCompletionHandler`` that also answers ``IAgileObject``.

    Reference count: 1 for the pending activation (dropped by ``ActivateCompleted``)
    plus one per ``AddRef`` Windows makes. The handler is registered in
    ``_LIVE_HANDLERS`` until the count reaches zero.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self._refs = 1
        self._refs_lock = threading.Lock()
        qi = _functype(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(self._qi)
        add_ref = _functype(ctypes.c_ulong, ctypes.c_void_p)(self._add_ref)
        release = _functype(ctypes.c_ulong, ctypes.c_void_p)(self._release)
        completed = _functype(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(self._completed)
        self._callbacks = (qi, add_ref, release, completed)
        self._vtbl = (ctypes.c_void_p * 4)(*(ctypes.cast(c, ctypes.c_void_p).value for c in self._callbacks))
        self._obj = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.pointer = ctypes.addressof(self._obj)
        with _LIVE_HANDLERS_LOCK:
            _LIVE_HANDLERS[self.pointer] = self

    def _add_ref(self, _this: Any = None) -> int:
        with self._refs_lock:
            self._refs += 1
            return self._refs

    def _release(self, _this: Any = None) -> int:
        with self._refs_lock:
            self._refs = max(self._refs - 1, 0)
            refs = self._refs
        if refs == 0:
            with _LIVE_HANDLERS_LOCK:
                if _LIVE_HANDLERS.pop(self.pointer, None) is not None:
                    _RETIRED_HANDLERS.append(self)
        return refs

    def _qi(self, this: Any, riid: Any, out: Any) -> int:
        if str(uuid.UUID(bytes_le=bytes(riid[0].bytes))) in _HANDLER_IIDS:
            self._add_ref()
            out[0] = this
            return 0
        out[0] = None
        return _E_NOINTERFACE

    def _completed(self, _this: Any, _operation: Any) -> int:
        self.done.set()
        self._release()  # the pending-activation reference
        return 0


def _activate_async(variant: _PropVariant, handler_pointer: int) -> ctypes.c_void_p:
    """Start ``ActivateAudioInterfaceAsync`` on the process-loopback device; the operation interface."""

    ptr = ctypes.byref
    iid, operation = _GUID.of(_IID_AUDIO_CLIENT), ctypes.c_void_p()
    activate = ctypes.WinDLL("mmdevapi").ActivateAudioInterfaceAsync  # type: ignore[attr-defined]  # Windows only
    activate.restype = ctypes.c_long
    activate.argtypes = [ctypes.c_wchar_p] + [ctypes.c_void_p] * 3 + [ctypes.POINTER(ctypes.c_void_p)]
    hr = activate("VAD\\Process_Loopback", ptr(iid), ptr(variant), handler_pointer, ptr(operation))
    if hr < 0:
        raise ProcessLoopbackError(f"ActivateAudioInterfaceAsync failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
    return operation


class _ProcessLoopbackClient:
    """One process-loopback ``IAudioClient``; every method runs on the reader thread."""

    def __init__(self) -> None:
        self._client: Any = None
        self._capture: Any = None
        self._volume: Any = None

    def open(self, sample_rate: int, pid: int) -> None:
        ptr = ctypes.byref
        params = _ActivationParams(_PROCESS_LOOPBACK_TYPE, pid, _EXCLUDE_TARGET_TREE)
        variant = _PropVariant(_VT_BLOB, (_U16 * 3)(), ctypes.sizeof(params), ctypes.addressof(params))
        handler = _CompletionHandler()
        try:
            operation = _activate_async(variant, handler.pointer)
        except Exception:
            handler._release()  # Windows never got the handler: nothing will call it back
            raise
        try:
            if not handler.done.wait(_ACTIVATE_TIMEOUT_SECONDS):
                # Windows may still call the handler later; it stays registered until then.
                raise ProcessLoopbackError("process-loopback activation timed out")
            result, unknown = ctypes.c_long(), ctypes.c_void_p()
            _vcall(operation, 3, "GetActivateResult", ptr(result), ptr(unknown))
        finally:
            _vcall(operation, 2, "Release", check=False)
        if result.value < 0:
            raise ProcessLoopbackError(
                f"process-loopback activation failed (HRESULT 0x{result.value & 0xFFFFFFFF:08X})"
            )
        self._client = unknown
        width = _CHANNELS * 2
        # 16-bit PCM at the pipeline rate: the format AUTOCONVERTPCM documents; read() makes float32.
        fmt = _WaveFormat(1, _CHANNELS, sample_rate, sample_rate * width, width, 16, 0)
        # Initialize(shared mode, flags, 200 ms buffer, periodicity, format, session)
        args = (ctypes.c_int(0), _U32(_STREAMFLAGS), ctypes.c_longlong(2_000_000), ctypes.c_longlong(0))
        _vcall(unknown, 3, "IAudioClient::Initialize", *args, ptr(fmt), ctypes.c_void_p())
        self._capture = self._service(_IID_CAPTURE_CLIENT)
        try:
            self._volume = self._service(_IID_SIMPLE_VOLUME)
        except ProcessLoopbackError:
            logger.info("Process loopback: no ISimpleAudioVolume on the capture session.")
        _vcall(unknown, 10, "IAudioClient::Start")

    def _service(self, iid_text: str) -> Any:
        out = ctypes.c_void_p()
        _vcall(self._client, 14, "IAudioClient::GetService", ctypes.byref(_GUID.of(iid_text)), ctypes.byref(out))
        return out

    def read(self) -> np.ndarray | None:
        """All pending packets as ``(frames, 2)`` float32; ``None`` when none are waiting."""

        blocks: list[np.ndarray] = []
        ptr = ctypes.byref
        while True:
            size = _U32()
            _vcall(self._capture, 5, "GetNextPacketSize", ptr(size))
            if size.value == 0:
                break
            data, frames, flags, none = ctypes.c_void_p(), _U32(), _U32(), ctypes.c_void_p()
            _vcall(self._capture, 3, "GetBuffer", ptr(data), ptr(frames), ptr(flags), none, none)
            try:
                if flags.value & _BUFFERFLAGS_SILENT or not data.value:
                    block = np.zeros((frames.value, _CHANNELS), dtype=np.float32)
                else:
                    raw = ctypes.string_at(data.value, frames.value * _CHANNELS * 2)
                    pcm = np.frombuffer(raw, dtype="<i2").reshape(frames.value, _CHANNELS)
                    block = pcm.astype(np.float32) / 32768.0
            finally:
                _vcall(self._capture, 4, "ReleaseBuffer", _U32(frames.value))
            blocks.append(block)
        return np.concatenate(blocks) if blocks else None

    def app_volume(self) -> tuple[float, bool] | None:
        """(volume, muted) Windows holds for this capture's own session, ``None`` if unreadable."""

        if self._volume is None:
            return None
        level, muted = ctypes.c_float(), ctypes.c_int()
        _vcall(self._volume, 4, "GetMasterVolume", ctypes.byref(level))
        _vcall(self._volume, 6, "GetMute", ctypes.byref(muted))
        return float(level.value), bool(muted.value)

    def close(self) -> None:
        if self._client is not None:
            _vcall(self._client, 11, "IAudioClient::Stop", check=False)
        for pointer in (self._volume, self._capture, self._client):
            if pointer is not None:
                _vcall(pointer, 2, "Release", check=False)
        self._client = self._capture = self._volume = None


class ProcessLoopbackStream(WasapiLoopbackStream):
    """Everything except OnCue's own process tree, as one prospect stream."""

    def __init__(
        self,
        config: AudioConfig,
        *,
        client_factory: Callable[[], Any] = _ProcessLoopbackClient,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._client_factory = client_factory
        self._client: Any = None
        self._following_default = False

    @property
    def device_label(self) -> str:
        return "wasapi:process-loopback"

    def _import_backend(self) -> Any:
        return self._client_factory

    def _fail(self, message: str, detail: str) -> bool:
        self._emit_warning(message)
        self._degraded = True
        self._health.mark_attach_failed(detail)
        return False

    def _open_recorder(self, *, silent: bool = False) -> bool:
        build = _windows_build()
        if build < MIN_WINDOWS_BUILD:
            return self._fail(
                f"Process loopback needs Windows build {MIN_WINDOWS_BUILD} or newer (this PC reports {build}); "
                "set WINDOWS_CAPTURE_ROUTE=endpoint.",
                "Windows build too old for process loopback",
            )
        client = self._sc()
        try:
            client.open(self._config.sample_rate, os.getpid())
        except Exception as exc:
            logger.warning("Process loopback activation failed", exc_info=True)
            try:
                client.close()
            except Exception:
                logger.debug("Process loopback client close after a failed open raised", exc_info=True)
            return self._fail(
                f"Process loopback could not start ({exc}); the tap stays silent.", "process loopback activation failed"
            )
        self._client = client
        self._degraded = False
        self._health.mark_attached(self.device_label)
        self._check_app_volume()
        return True

    def _check_app_volume(self) -> None:
        try:
            measured = self._client.app_volume()
        except Exception:
            logger.warning("Process loopback: could not read the app volume.", exc_info=True)
            return
        if measured is None:
            return
        volume, muted = measured
        if muted or volume < APP_VOLUME_FLOOR:
            state = "muted" if muted else f"set to {volume:.0%}"
            self._emit_warning(
                f"Windows has the volume of OnCue's capture {state}, so the call will sound silent. "
                "Raise it in the Windows volume mixer (Settings > System > Sound > Volume mixer, OnCue), "
                "or set WINDOWS_CAPTURE_ROUTE=endpoint."
            )

    def _close_recorder(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            logger.warning("ProcessLoopbackStream client close raised", exc_info=True)

    def _read_loop(self) -> None:
        accum = np.zeros((0,), dtype=np.float32)
        last_emit = time.monotonic()
        pacing = (self._config.chunk_size / float(self._config.sample_rate)) * self._silence_timeout_multiplier
        while not self._stop_event.is_set():
            try:
                block = self._client.read()
                if block is not None and block.ndim == 2 and block.shape[0] > 0:
                    accum = np.concatenate([accum, np.mean(block.astype(np.float32, copy=False), axis=1)])
                    while accum.shape[0] >= self._config.chunk_size:
                        self._emit_chunk(accum[: self._config.chunk_size])
                        accum = accum[self._config.chunk_size :]
                        last_emit = time.monotonic()
                else:
                    time.sleep(self._empty_pull_sleep_s)
            except Exception:
                self._emit_warning("Process loopback failed while recording; the tap stops.")
                self._health.mark_detached("process loopback failed while recording", already_warned=True)
                self._degraded = True
                return
            if accum.shape[0] < self._config.chunk_size and (time.monotonic() - last_emit) >= pacing:
                self._emit_silence_chunk()
                last_emit = time.monotonic()


def resolve_capture_route(value: str | None) -> str:
    """``endpoint`` or ``process``; an unknown value logs a warning and means ``endpoint``."""

    route = (value or ROUTE_ENDPOINT).strip().lower() or ROUTE_ENDPOINT
    if route not in (ROUTE_ENDPOINT, ROUTE_PROCESS):
        logger.warning("WINDOWS_CAPTURE_ROUTE=%r is not 'endpoint' or 'process'; using 'endpoint'.", value)
        return ROUTE_ENDPOINT
    return route


def build_windows_prospect_stream(config: AudioConfig, **kwargs: Any) -> WasapiLoopbackStream:
    """The Windows prospect stream for ``config.windows_capture_route``."""

    if resolve_capture_route(config.windows_capture_route) == ROUTE_PROCESS:
        return ProcessLoopbackStream(config, **kwargs)
    return WasapiLoopbackStream(config, **kwargs)
