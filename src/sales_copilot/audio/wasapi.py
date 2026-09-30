"""Windows WASAPI loopback prospect stream (system/render-endpoint tap).

The macOS prospect stream taps the whole system-output mix via AudioTee
(``tap_all``). WASAPI loopback on the default render endpoint is the Windows
analog: ``soundcard`` opens a loopback "microphone" on the default speaker and
delivers whatever plays through it. ``WasapiLoopbackStream`` exposes the exact
same surface as ``AudioTeeStream``/``BlackHoleStream`` (``start``/``stop``/
``read`` plus ``chunks_received`` and ``device_label``), so the recorder,
talk-time VAD and ``check_streams_liveness`` all consume it unchanged.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import AudioConfig, _drop_oldest_and_put
from .tap_health import DEFAULT_HEARTBEAT_SECONDS, TapHealth, TapHealthTracker

logger = logging.getLogger(__name__)

# COM apartment-membership is per-thread, not per-process. ``soundcard``'s own
# COM bootstrap (``soundcard.mediafoundation._com = _COMLibrary()``) runs
# exactly once, at import time, on whichever thread happens to first import
# the module -- there is no guarantee that thread is the one that later opens
# or reads the WASAPI loopback recorder. Touching a COM interface pointer
# (opening or reading it) from a thread that never joined an apartment is
# undefined behaviour, and on this stream's specific shape -- recorder opened
# on the calling thread, read from a separately spawned reader thread -- the
# observed real-world result is a native access violation with no Python
# traceback, roughly a second after ``start()`` returns (once the reader
# thread's first live read actually touches the COM pointer). ``_read_loop``
# (via ``_reader_main``) now owns the recorder's entire lifecycle -- open,
# read, periodic re-attach, and close -- on one OS thread, and that thread
# explicitly joins the process's multithreaded COM apartment for its
# lifetime instead of relying on ``soundcard``'s one-shot, import-thread-only
# initialization.


def _ole32_handle() -> Any | None:
    """Return the ``ole32`` COM library handle on Windows, or None elsewhere.

    A thin, mockable seam around ``ctypes.windll.ole32``: ``ctypes.windll``
    does not exist at all outside a real Windows interpreter (not merely
    "raises on call" -- the attribute itself is absent), so this can't be a
    bare attribute access guarded only by ``sys.platform``. Tests exercise
    this module on macOS/Linux CI with ``sys.platform`` monkeypatched to
    ``"win32"``, so the platform string alone is not a reliable signal that
    ``ctypes.windll`` is actually present.
    """

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    return getattr(windll, "ole32", None)


def _com_initialize_mta() -> bool:
    """Join the calling thread to the process's COM multithreaded apartment.

    Returns True only when this call actually joined the apartment and
    therefore must be balanced with :func:`_com_uninitialize`: ``S_OK`` (the
    thread joined) and ``S_FALSE`` (COM was already up on this thread with a
    compatible concurrency model, and the reference count still went up).

    Every other outcome returns False, which means "do NOT undo this":
    not on Windows, ``ctypes.windll`` unavailable, ``RPC_E_CHANGED_MODE``
    (the thread already belongs to an apartment with an incompatible
    concurrency model -- mirroring how ``soundcard`` itself treats that
    HRESULT as "already joined, nothing to clean up"), or any other failed
    HRESULT such as ``E_OUTOFMEMORY``/``E_INVALIDARG``. Testing for one
    known failure value instead of the two success values reported a failed
    ``CoInitializeEx`` as joined, after which ``CoUninitialize`` ran against
    an apartment reference this thread never took -- which Microsoft
    documents as forbidden.
    """

    if sys.platform != "win32":
        return False
    ole32 = _ole32_handle()
    if ole32 is None:
        return False
    com_init_multithreaded = 0x0
    s_ok = 0x0
    s_false = 0x1
    try:
        # int() inside the try on purpose: a handle that hands back anything
        # but an integer HRESULT must degrade to "not joined" like any other
        # COM failure, not raise out of the reader thread's entry point.
        hr = int(ole32.CoInitializeEx(None, com_init_multithreaded))
    except Exception:
        logger.warning("WasapiLoopbackStream: CoInitializeEx failed on reader thread.", exc_info=True)
        return False
    if hr < 0:
        hr += 2**32
    return hr in (s_ok, s_false)


def _com_uninitialize() -> None:
    """Undo a successful :func:`_com_initialize_mta` call on this thread."""

    if sys.platform != "win32":
        return
    ole32 = _ole32_handle()
    if ole32 is None:
        return
    try:
        ole32.CoUninitialize()
    except Exception:
        logger.warning("WasapiLoopbackStream: CoUninitialize failed on reader thread.", exc_info=True)


# The digital-silence threshold that used to live here (0.0005, mirroring
# check_device_health) now lives in tap_health.DEFAULT_SIGNAL_FLOOR_RMS, where
# every capture backend reads it from one place and an operator can override it
# with AUDIO_TAP_SIGNAL_FLOOR_RMS.

# How long stop() waits for the reader thread to leave the read loop and close
# the recorder itself. soundcard's record() fills short reads with zeros rather
# than blocking (mediafoundation.py:800-806), so on a healthy endpoint the
# thread returns in well under one pull interval; two seconds is only reached
# when a driver call is genuinely wedged.
_STOP_JOIN_TIMEOUT_SECONDS = 2.0

# How long start() waits for the reader thread to report the outcome of the
# open. The whole open is COM enumeration plus IAudioClient::Initialize --
# tens to a few hundred milliseconds on a healthy endpoint, and it is the only
# work between thread start and `ready`. Ten seconds is an order of magnitude
# of headroom over the slowest endpoint enumeration observed, and it sits well
# under tap_health's 30s heartbeat, so a wedged driver surfaces as a degraded
# tap on the next heartbeat instead of as a start() that never returns while
# holding _lock (which would wedge stop() along with it).
_OPEN_READY_TIMEOUT_SECONDS = 10.0


def _describe_speaker(speaker: Any) -> str:
    """Human-readable label for a soundcard speaker, for re-attach log lines."""

    name = getattr(speaker, "name", None)
    if name:
        return str(name)
    return str(getattr(speaker, "id", speaker))


def _find_named_speaker(sc: Any, name: str) -> Any | None:
    """Match ``name`` case-insensitively (substring) against every known speaker.

    Mirrors the substring-match style ``find_input_device`` already uses for
    mic devices, so an operator can point at "Realtek" instead of the full,
    driver-qualified endpoint name Windows reports.
    """

    needle = name.strip().lower()
    if not needle:
        return None
    try:
        speakers = sc.all_speakers()
    except Exception:
        logger.warning("WasapiLoopbackStream: soundcard.all_speakers() raised.", exc_info=True)
        return None
    for speaker in speakers or ():
        label = str(getattr(speaker, "name", "") or "")
        if needle in label.lower():
            return speaker
    return None


class WasapiLoopbackStream:
    """WASAPI loopback capture on a Windows render endpoint.

    Follows whatever Windows currently calls the default playback device
    unless ``AudioConfig.wasapi_endpoint_name`` names a specific endpoint
    (matched by substring against ``soundcard.all_speakers()``). While
    following the default, the reader thread periodically re-checks
    ``soundcard.default_speaker()`` and re-attaches if it has changed --
    ``sc.default_speaker()`` is otherwise resolved once, at ``start()``, so a
    device plugged in or switched mid-call would otherwise keep being read
    from the old endpoint. A matched explicit override is pinned and never
    displaced by a later default-device change.

    ``start()`` is degrade-not-raise on any setup failure (missing
    ``soundcard``, non-Windows platform, no default endpoint, recorder error,
    rate/shape mismatch), deliberately following ``AudioTeeStream`` and
    unlike ``MicStream``/``BlackHoleStream`` which raise: a background
    prospect stream must never crash the call.
    """

    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
        empty_pull_sleep_s: float = 0.01,
        silence_timeout_multiplier: float = 3.0,
        endpoint_poll_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        self._config = config
        self._on_warning = on_warning
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._recorder_cm: Any = None
        self._recorder: Any = None
        self._lock = threading.Lock()
        self._chunks_received = 0
        self._degraded = False
        self._health = TapHealthTracker()
        # A pull that returns zero frames sleeps this long before retrying
        # (SoundCard WASAPI silence-at-start, bastibe/SoundCard#166), and the
        # accumulator synthesizes a zero-filled chunk once this multiple of
        # the expected chunk duration has passed without filling, so the
        # downstream continuous buffer never wedges.
        self._empty_pull_sleep_s = empty_pull_sleep_s
        self._silence_timeout_multiplier = silence_timeout_multiplier
        # Re-attach bookkeeping (limitation 2: sc.default_speaker() is only
        # resolved once, at start()). `_following_default` is True whenever
        # this stream is tracking whatever Windows currently calls the
        # default endpoint -- i.e. no AUDIO_WASAPI_ENDPOINT_NAME override, or
        # an override that did not match anything. An explicit, matched
        # override pins the tap to that named endpoint and is never displaced
        # by a later default-device change. The poll cadence deliberately
        # reuses tap_health's existing heartbeat constant instead of adding a
        # second timer: the reader thread already loops continuously.
        self._sc: Any = None
        self._following_default = True
        self._endpoint_poll_seconds = endpoint_poll_seconds
        self._speaker_id: Any = None
        self._speaker_label = ""

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def real_frames_received(self) -> int:
        """Chunks whose RMS cleared the digital-silence floor.

        Delegates to the shared tap-health tracker so this stream and the macOS
        taps answer "did anything but zeros come through" with one measurement
        and one threshold instead of two that can drift apart.
        """

        return self._health.signal_chunks

    def tap_health(self) -> TapHealth:
        """Report whether the loopback is attached, and whether audio is flowing."""

        return self._health.snapshot()

    @property
    def device_label(self) -> str:
        return "wasapi:loopback"

    def start(self) -> None:
        with self._lock:
            previous = self._reader_thread
            if previous is not None:
                if previous.is_alive():
                    # Either this stream is already running, or a previous
                    # reader outlived stop()'s join and still owns the
                    # recorder. Clearing the stop event now would wake that
                    # thread back up next to a second reader: both would read
                    # `self._recorder`, and the old one would eventually close
                    # the new one's recorder while leaking its own.
                    return
                # A reader thread that stop() could not join has since
                # finished, so it can no longer touch the recorder and the
                # stop event is safe to clear.
                self._reader_thread = None

            self._stop_event.clear()

            if sys.platform != "win32":
                self._emit_warning("WASAPI-loopback-capture is alleen beschikbaar op Windows; deze stream blijft stil.")
                self._degraded = True
                self._health.mark_attach_failed("niet-Windows platform")
                return

            try:
                sc = _import_soundcard()
            except Exception:
                self._emit_warning(
                    "Python-package 'soundcard' ontbreekt. Installeer met "
                    "`pip install .[windows]` om WASAPI-loopback-capture te gebruiken."
                )
                self._degraded = True
                self._health.mark_attach_failed("soundcard ontbreekt")
                return

            self._sc = sc

            # The recorder must be opened on the same thread that will read
            # it (see the module docstring block above _ole32_handle): spawn
            # the reader thread first and let it perform the entire open
            # sequence itself, blocking here on `ready` so start() keeps its
            # existing synchronous, degrade-not-raise contract -- same
            # warnings, same TapHealth transitions, just executed on the
            # reader thread instead of the caller's.
            ready = threading.Event()
            opened: list[bool] = [False]
            abandoned = threading.Event()
            reader_thread = threading.Thread(
                target=self._reader_main,
                args=(ready, opened, abandoned),
                name="wasapi-loopback-reader",
                daemon=True,
            )
            reader_thread.start()

            if not ready.wait(_OPEN_READY_TIMEOUT_SECONDS):
                # The open is wedged inside a driver call. Keep the thread
                # handle so stop() still joins it and a later start() cannot
                # race a second reader against it, and set the stop event so
                # it tears its own recorder down the moment the driver call
                # returns -- it is the only thread allowed to close it.
                abandoned.set()
                self._stop_event.set()
                self._reader_thread = reader_thread
                self._emit_warning(
                    "Het WASAPI-loopback-apparaat reageerde niet bij het openen; WASAPI-loopback-capture blijft stil."
                )
                self._degraded = True
                self._health.mark_attach_failed("openen van loopback-apparaat reageerde niet")
                return

            if not opened[0]:
                self._degraded = True
                return

            self._degraded = False
            self._reader_thread = reader_thread

    def _reader_main(self, ready: threading.Event, opened: list[bool], abandoned: threading.Event) -> None:
        """Reader-thread entry point: owns the recorder's entire lifecycle.

        Opening, reading, periodic re-attach, and closing all happen here, on
        one OS thread, for the reasons in the module docstring block above
        ``_ole32_handle``. ``ready`` unblocks ``start()`` once the open has
        succeeded or failed; ``opened`` carries the outcome across that
        hand-off (the ``Event`` itself provides the memory-visibility
        guarantee, so no additional lock is needed for that hand-off).
        ``abandoned`` is set when ``start()`` stopped waiting for that
        hand-off, so a late-arriving open does not report a healthy tap for a
        thread that is on its way out.

        The COM join sits inside the ``try`` and ``ready`` is set from a
        ``finally``: anything raised on the way to the open must still release
        ``start()``, which waits on that event while holding ``_lock`` and
        would otherwise wedge ``stop()`` along with itself. Closing the
        recorder likewise sits in the outermost ``finally``, so *every* exit
        path closes what was opened -- not only the ones that reached the read
        loop. ``stop()`` deliberately closes nothing, so a leak here is
        permanent.
        """

        com_joined = False
        success = False
        try:
            try:
                com_joined = _com_initialize_mta()
                success = self._open_recorder()
            except Exception:
                logger.warning(
                    "WasapiLoopbackStream: unexpected error while opening the loopback recorder.",
                    exc_info=True,
                )
                self._emit_warning("Onverwachte fout bij het openen van WASAPI-loopback-capture; de tap blijft stil.")
                self._degraded = True
                self._health.mark_attach_failed("onverwachte fout bij openen")
                success = False
            finally:
                opened[0] = success
                ready.set()

            if not success:
                return

            if abandoned.is_set():
                # start() already gave up on this open and reported the tap as
                # failed. Do not overwrite that with the mark_attached() the
                # open just did: this thread is about to exit and the finally
                # below closes the recorder it opened.
                self._health.mark_attach_failed("openen van loopback-apparaat reageerde niet")
                return

            self._read_loop()
        finally:
            try:
                self._close_recorder()
            finally:
                if com_joined:
                    _com_uninitialize()

    def _close_recorder(self) -> None:
        """Close this thread's recorder, if it still holds one.

        Only ever called from the reader thread. A no-op when the open never
        got as far as ``recorder_cm.__enter__()``.
        """

        recorder_cm = self._recorder_cm
        self._recorder_cm = None
        self._recorder = None
        if recorder_cm is None:
            return
        try:
            recorder_cm.__exit__(None, None, None)
        except Exception:
            logger.warning("WasapiLoopbackStream recorder __exit__ raised", exc_info=True)

    def _open_recorder(self) -> bool:
        """Resolve the target endpoint and open its WASAPI loopback recorder.

        Runs on the reader thread (called from ``_reader_main``), never on
        the thread that calls ``start()``. Returns True and populates
        ``self._recorder``/``self._recorder_cm`` on success; on any failure
        it emits the same operator-facing warning and ``TapHealth`` state
        this used to set from ``start()`` and returns False.
        """

        sc = self._sc
        endpoint_name = (self._config.wasapi_endpoint_name or "").strip()
        following_default = True
        speaker = None
        if endpoint_name:
            speaker = _find_named_speaker(sc, endpoint_name)
            if speaker is None:
                self._emit_warning(
                    f"WASAPI-eindpunt '{endpoint_name}' (AUDIO_WASAPI_ENDPOINT_NAME) niet gevonden; "
                    "WASAPI-loopback-capture valt terug op het standaard uitvoerapparaat."
                )
            else:
                following_default = False

        if speaker is None:
            try:
                speaker = sc.default_speaker()
            except Exception:
                speaker = None
        if speaker is None:
            self._emit_warning(
                "Geen standaard audio-uitvoerapparaat gevonden; WASAPI-loopback-capture kan niet starten."
            )
            self._degraded = True
            self._health.mark_attach_failed("geen standaard uitvoerapparaat")
            return False

        try:
            microphone = sc.get_microphone(speaker.id, include_loopback=True)
        except Exception:
            self._emit_warning("Kon geen WASAPI-loopback-apparaat openen voor het standaard uitvoerapparaat.")
            self._degraded = True
            self._health.mark_attach_failed("loopback-apparaat kon niet openen")
            return False

        blocksize = self._config.chunk_size * 2
        try:
            recorder_cm = microphone.recorder(samplerate=self._config.sample_rate, blocksize=blocksize)
            recorder = recorder_cm.__enter__()
        except Exception:
            self._emit_warning("Kon de WASAPI-loopback-recorder niet openen.")
            self._degraded = True
            self._health.mark_attach_failed("recorder kon niet openen")
            return False

        self._recorder_cm = recorder_cm
        self._recorder = recorder
        self._following_default = following_default
        self._speaker_id = getattr(speaker, "id", None)
        self._speaker_label = _describe_speaker(speaker)
        self._degraded = False
        self._health.mark_attached(self.device_label)
        return True

    def _check_default_endpoint_drift(self) -> None:
        """Re-attach when the Windows default output endpoint has changed.

        Only fires while this stream is following the default endpoint
        (``_following_default``): an explicit, matched ``AUDIO_WASAPI_ENDPOINT_NAME``
        override pins the tap to that named device and must never be displaced
        by a later default-device change, or the override would be pointless.
        """

        if not self._following_default or self._sc is None:
            return
        try:
            current = self._sc.default_speaker()
        except Exception:
            return
        if current is None:
            return
        if getattr(current, "id", None) == self._speaker_id:
            return
        self._reattach(current)

    def _reattach(self, new_speaker: Any) -> None:
        """Swap the recorder to ``new_speaker`` without dropping buffered audio.

        Opens the new recorder before touching any shared state, so a failure
        here leaves the current (still-working) tap running untouched. The
        actual swap is guarded by ``_lock`` and aborts if ``stop()`` has
        already begun, so a concurrent shutdown can never race a freshly
        opened recorder into being silently leaked.
        """

        old_label = self._speaker_label
        new_label = _describe_speaker(new_speaker)
        try:
            microphone = self._sc.get_microphone(new_speaker.id, include_loopback=True)
            blocksize = self._config.chunk_size * 2
            new_recorder_cm = microphone.recorder(samplerate=self._config.sample_rate, blocksize=blocksize)
            new_recorder = new_recorder_cm.__enter__()
        except Exception:
            logger.warning(
                "WASAPI re-attach to new default endpoint (%s) failed; keeping current tap (%s).",
                new_label,
                old_label,
                exc_info=True,
            )
            return

        with self._lock:
            if self._stop_event.is_set():
                try:
                    new_recorder_cm.__exit__(None, None, None)
                except Exception:
                    pass
                return
            old_recorder_cm = self._recorder_cm
            self._recorder_cm = new_recorder_cm
            self._recorder = new_recorder
            self._speaker_id = getattr(new_speaker, "id", None)
            self._speaker_label = new_label

        self._health.mark_attached(self.device_label)
        if old_recorder_cm is not None:
            try:
                old_recorder_cm.__exit__(None, None, None)
            except Exception:
                logger.warning("WasapiLoopbackStream old recorder __exit__ raised during re-attach", exc_info=True)

        logger.info(
            "WASAPI loopback default output endpoint changed, re-attaching: %s -> %s",
            old_label,
            new_label,
        )

    def _read_loop(self) -> None:
        accum = np.zeros((0,), dtype=np.float32)
        shape_checked = False
        last_emit = time.monotonic()
        last_endpoint_check = time.monotonic()
        pacing_timeout = (self._config.chunk_size / float(self._config.sample_rate)) * self._silence_timeout_multiplier

        while not self._stop_event.is_set():
            now = time.monotonic()
            if self._following_default and (now - last_endpoint_check) >= self._endpoint_poll_seconds:
                self._check_default_endpoint_drift()
                last_endpoint_check = now

            try:
                block = self._recorder.record(numframes=self._config.chunk_size)
            except Exception:
                self._emit_warning("WASAPI-loopback-apparaat gaf een fout of verdween tijdens opname.")
                self._degraded = True
                return

            if not shape_checked:
                shape_checked = True
                if not self._validate_first_block_shape(block):
                    self._emit_warning(
                        "Onverwachte vorm of samplerate van WASAPI-loopback-audio "
                        f"(verwacht (frames, kanalen) op {self._config.sample_rate} Hz)."
                    )
                    self._degraded = True
                    return

            try:
                frames = int(block.shape[0]) if isinstance(block, np.ndarray) and block.ndim == 2 else 0

                if frames > 0:
                    mono = np.mean(block.astype(np.float32, copy=False), axis=1)
                    accum = np.concatenate([accum, mono])
                    while accum.shape[0] >= self._config.chunk_size:
                        frame = accum[: self._config.chunk_size]
                        accum = accum[self._config.chunk_size :]
                        self._emit_chunk(frame)
                        last_emit = time.monotonic()
                else:
                    time.sleep(self._empty_pull_sleep_s)

                if accum.shape[0] < self._config.chunk_size and (time.monotonic() - last_emit) >= pacing_timeout:
                    self._emit_silence_chunk()
                    last_emit = time.monotonic()
            except Exception:
                self._emit_warning("WASAPI-loopback-apparaat gaf een fout of verdween tijdens opname.")
                self._degraded = True
                return

    def _validate_first_block_shape(self, block: Any) -> bool:
        if block is None:
            return True
        if not isinstance(block, np.ndarray):
            return False
        if block.ndim != 2:
            return False
        if block.shape[1] < 1:
            return False
        return True

    def _emit_chunk(self, mono_frame: np.ndarray) -> None:
        frame = mono_frame.reshape(self._config.chunk_size, 1).astype(np.float32, copy=False)
        self._chunks_received += 1
        if self._health.observe(frame):
            logger.info(
                "WASAPI loopback carrying signal: first audible frame after %s chunks.",
                self._chunks_received,
            )
        _drop_oldest_and_put(self._queue, frame)

    def _emit_silence_chunk(self) -> None:
        frame = np.zeros((self._config.chunk_size, 1), dtype=np.float32)
        self._chunks_received += 1
        self._health.observe(frame)
        _drop_oldest_and_put(self._queue, frame)

    def _emit_warning(self, message: str) -> None:
        logger.error("WasapiLoopbackStream: %s", message)
        if self._on_warning is None:
            return
        try:
            self._on_warning({"type": "audio_warning", "stream": "prospect", "message": message})
        except Exception:
            logger.warning("on_warning callback raised for WASAPI loopback stream", exc_info=True)

    def stop(self) -> None:
        # The recorder is closed on the reader thread itself, in
        # `_reader_main`'s outermost finally, once `_read_loop` observes
        # `_stop_event` and returns -- not here. Closing a WASAPI COM
        # recorder from whichever thread happens to call `stop()` is the
        # same cross-thread COM violation `start()` used to make on open.
        # So this call returning is not a promise that the recorder is
        # already closed: on a join that times out, that happens later, on
        # the reader thread, and until then this stream stays un-restartable.
        with self._lock:
            self._stop_event.set()
            thread = self._reader_thread

        if thread is None:
            return

        if thread is not threading.current_thread():
            thread.join(timeout=_STOP_JOIN_TIMEOUT_SECONDS)

        with self._lock:
            # Release the slot only once the reader has actually exited. A
            # join that timed out leaves a thread that still owns the recorder
            # and still reads `self._recorder` every iteration; handing the
            # slot back there would let the next start() clear the stop event
            # and wake it up alongside a second reader. start() picks such a
            # thread up again once it has finished.
            if self._reader_thread is thread and not thread.is_alive():
                self._reader_thread = None

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


def _import_soundcard() -> Any:
    import soundcard as sc  # type: ignore[import-not-found]

    return sc
