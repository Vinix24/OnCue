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

from sales_copilot.core.i18n import t
from sales_copilot.core.paths import resolve_app_resource

from .capture import AudioConfig, _drop_oldest_and_put
from .tap_health import DEFAULT_HEARTBEAT_SECONDS, TapHealth, TapHealthTracker
from .wasapi_endpoint import _APPS_RELATIVE, load_call_apps, pick_endpoint_id, read_snapshot

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
# A COM snapshot slower than this on the reader thread is logged (D3 reads that line).
_SLOW_SNAPSHOT_S = 0.1

# How long start() waits for the reader thread to report the outcome of the
# open. The whole open is COM enumeration plus IAudioClient::Initialize --
# tens to a few hundred milliseconds on a healthy endpoint, and it is the only
# work between thread start and `ready`. Ten seconds is an order of magnitude
# of headroom over the slowest endpoint enumeration observed, and it sits well
# under tap_health's 30s heartbeat, so a wedged driver surfaces as a degraded
# tap on the next heartbeat instead of as a start() that never returns while
# holding _lock (which would wedge stop() along with it).
_OPEN_READY_TIMEOUT_SECONDS = 10.0

# How long the reader thread waits between attempts to re-open a loopback
# recorder after the render endpoint vanished. The same interval is waited once
# before the first attempt: Windows needs a moment to promote a new default
# output device after the old one disappears.
_RECOVERY_RETRY_SECONDS = 1.0

# ADR-008: how often the selector runs while following the call's endpoint, and
# how long an outage may last before tap_health reports no_tap and the audio
# warning is emitted (once per outage).
_FOLLOW_POLL_SECONDS = 2.0
_FOLLOW_OUTAGE_GRACE_SECONDS = 10.0


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
        recovery_retry_s: float = _RECOVERY_RETRY_SECONDS,
        follow_poll_seconds: float = _FOLLOW_POLL_SECONDS,
        outage_grace_s: float = _FOLLOW_OUTAGE_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._recovery_retry_s = recovery_retry_s
        self._clock = clock
        self._sleep = sleep
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
        # ADR-008 follow mode: True only while WINDOWS_ENDPOINT_FOLLOW is on and
        # no AUDIO_WASAPI_ENDPOINT_NAME override matched.
        self._follow_active = False
        self._follow_poll_seconds = follow_poll_seconds
        self._outage_grace_s = outage_grace_s
        self._call_apps: frozenset[str] | None = None
        self._select_reason = ""
        self._default_lookup_failure_logged = False
        self._outage_warned = False
        # Start of the current outage; kept until a read returns audio again (not
        # until a mere re-open), so the grace period cannot restart on every recovery.
        self._outage_start: float | None = None
        # A failed call-app list load is remembered: logged once, not on every poll.
        self._call_apps_failed = False
        # Seconds of lost time not yet worth a whole chunk of silence; carried to
        # the next switch so many short gaps cannot drift the timeline.
        self._silence_carry_s = 0.0
        # ADR-008 fallback order after the selected endpoint: communications, console.
        self._fallback_ids: list[str] = []

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
                self._emit_warning(t("audio.wasapi_windows_only"))
                self._degraded = True
                self._health.mark_attach_failed(t("audio.detail_not_windows"))
                return

            try:
                sc = self._import_backend()
            except Exception:
                self._emit_warning(t("audio.wasapi_soundcard_missing"))
                self._degraded = True
                self._health.mark_attach_failed(t("audio.detail_soundcard_missing"))
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
                self._emit_warning(t("audio.wasapi_open_timeout"))
                self._degraded = True
                self._health.mark_attach_failed(t("audio.detail_open_timeout"))
                return

            if not opened[0]:
                self._degraded = True
                return

            self._degraded = False
            self._reader_thread = reader_thread

    def _import_backend(self) -> Any:
        """Return the capture backend ``_open_recorder`` works with (a seam for subclasses)."""

        return _import_soundcard()

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
                self._emit_warning(t("audio.wasapi_unexpected_open_error"))
                self._degraded = True
                self._health.mark_attach_failed(t("audio.detail_unexpected_open_error"))
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
                self._health.mark_attach_failed(t("audio.detail_open_timeout"))
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

    def _open_recorder(self, *, silent: bool = False) -> bool:
        """Resolve the target endpoint and open its WASAPI loopback recorder.

        Runs on the reader thread (called from ``_reader_main``), never on
        the thread that calls ``start()``. Returns True and populates
        ``self._recorder``/``self._recorder_cm`` on success; on any failure
        it emits the same operator-facing warning and ``TapHealth`` state
        this used to set from ``start()`` and returns False.

        ``silent=True`` is the outage-recovery variant: a failed attempt emits
        no warning and leaves ``TapHealth`` and ``_degraded`` alone (the outage
        was reported once and the loop retries), and a successful one leaves
        the ``mark_attached`` call to the recovery loop.
        """

        sc = self._sc
        endpoint_name = (self._config.wasapi_endpoint_name or "").strip()
        following_default = True
        speaker = None
        if endpoint_name:
            speaker = _find_named_speaker(sc, endpoint_name)
            if speaker is None:
                if not silent:
                    self._emit_warning(t("audio.wasapi_endpoint_not_found", endpoint=endpoint_name))
            else:
                following_default = False

        if speaker is None and following_default and self._config.windows_endpoint_follow:
            speaker = self._select_speaker(sc)
        if speaker is None:
            try:
                speaker = sc.default_speaker()
            except Exception:
                speaker = None
        if speaker is None:
            if not silent:
                self._emit_warning(t("audio.wasapi_no_default_output"))
                self._degraded = True
                self._health.mark_attach_failed(t("audio.detail_no_default_output"))
            return False

        # ADR-008 point 3: a selected endpoint that cannot be opened must not block
        # the fallback; try the communications default, then the console default,
        # in the same pass. Only the follow mode (selector in charge) falls back.
        candidates = [speaker]
        if self._follow_active_for_open(following_default):
            candidates.extend(self._fallback_speakers(sc, speaker))
        blocksize = self._config.chunk_size * 2
        failure = ""
        recorder_cm = recorder = None
        for index, candidate in enumerate(candidates):
            try:
                microphone = sc.get_microphone(candidate.id, include_loopback=True)
            except Exception:
                if index == 0:
                    failure = "loopback"
                logger.warning("WASAPI loopback: cannot open endpoint %s", _describe_speaker(candidate), exc_info=True)
                continue
            try:
                recorder_cm = microphone.recorder(samplerate=self._config.sample_rate, blocksize=blocksize)
                recorder = recorder_cm.__enter__()
            except Exception:
                if index == 0:
                    failure = "recorder"
                recorder_cm = recorder = None
                logger.warning(
                    "WASAPI loopback: cannot open recorder for %s", _describe_speaker(candidate), exc_info=True
                )
                continue
            if candidate is not speaker:
                logger.info(
                    "WASAPI loopback fell back from %s to %s after an open failure.",
                    _describe_speaker(speaker),
                    _describe_speaker(candidate),
                )
            speaker = candidate
            break
        else:
            if not silent:
                if failure == "loopback":
                    self._emit_warning(t("audio.wasapi_no_loopback_device"))
                    self._health.mark_attach_failed(t("audio.detail_loopback_open_failed"))
                else:
                    self._emit_warning(t("audio.wasapi_recorder_open_failed"))
                    self._health.mark_attach_failed(t("audio.detail_recorder_open_failed"))
                self._degraded = True
            return False

        self._recorder_cm = recorder_cm
        self._recorder = recorder
        self._following_default = following_default
        self._follow_active = following_default and bool(self._config.windows_endpoint_follow)
        self._speaker_id = getattr(speaker, "id", None)
        self._speaker_label = _describe_speaker(speaker)
        self._degraded = False
        if not silent:
            self._health.mark_attached(self.device_label)
        return True

    def _follow_active_for_open(self, following_default: bool) -> bool:
        return following_default and bool(self._config.windows_endpoint_follow)

    def _fallback_speakers(self, sc: Any, tried: Any) -> list[Any]:
        """Speakers after ``tried`` in ADR-008 order: communications, console, then default."""

        found: list[Any] = []
        seen = {getattr(tried, "id", None)}
        try:
            available = list(sc.all_speakers() or ())
        except Exception:
            available = []
        for wanted in self._fallback_ids:
            for cand in available:
                cid = getattr(cand, "id", None)
                if cid == wanted and cid not in seen:
                    seen.add(cid)
                    found.append(cand)
        try:
            default = sc.default_speaker()
        except Exception:
            default = None
        if default is not None and getattr(default, "id", None) not in seen:
            found.append(default)
        return found

    def _select_speaker(self, sc: Any) -> Any | None:
        """Ask the ADR-008 selector for the speaker to tap; None means "no information".

        The caller then uses ``default_speaker()``, which is today's behaviour. A
        broken call-app list or a failing speaker lookup is logged and treated the
        same way, never as a reason to stop the capture.
        """

        self._select_reason = "no_information"
        self._fallback_ids = []
        if self._call_apps is None:
            if self._call_apps_failed:
                self._select_reason = "call_apps_unavailable"
                return None
            try:
                self._call_apps = load_call_apps()
            except Exception:
                self._call_apps_failed = True
                self._select_reason = "call_apps_unavailable"
                logger.warning(
                    "WasapiLoopbackStream: call-app list unavailable (%s); endpoint selection uses the default output.",
                    resolve_app_resource(_APPS_RELATIVE),
                    exc_info=True,
                )
                return None
        try:
            snapshot_started = self._clock()
            snapshot = read_snapshot()
            snapshot_s = self._clock() - snapshot_started
            if snapshot_s > _SLOW_SNAPSHOT_S:
                logger.info("WASAPI endpoint snapshot took %d ms (reader thread)", round(snapshot_s * 1000))
            endpoint_id, reason = pick_endpoint_id(self._speaker_id, self._call_apps, snapshot)
            if snapshot is not None:
                self._fallback_ids = [i for i in (snapshot.communications_id, snapshot.console_id) if i]
            if endpoint_id is None:
                self._select_reason = reason
                return None
            for candidate in sc.all_speakers() or ():
                if getattr(candidate, "id", None) == endpoint_id:
                    self._select_reason = reason
                    return candidate
            self._select_reason = f"{reason}, no matching speaker"
        except Exception:
            logger.warning("WasapiLoopbackStream: endpoint selection failed; using the default output.", exc_info=True)
        return None

    def _log_default_lookup_failure(self, exc: Exception) -> None:
        """Warn once that the default-speaker lookup fails, so it is not read as "endpoint unchanged"."""

        if self._default_lookup_failure_logged:
            return
        self._default_lookup_failure_logged = True
        logger.warning(
            "WASAPI loopback: default-speaker lookup failed (%s: %s); the endpoint check is skipped.",
            type(exc).__name__,
            exc,
        )

    def _follow_endpoint(self) -> bool:
        """Switch to the selector's choice when it differs. True when switched."""

        speaker = self._select_speaker(self._sc)
        if speaker is None:
            # ADR-008 point 1: no information means the default output, while
            # polling as well as at start.
            try:
                speaker = self._sc.default_speaker()
            except Exception as exc:
                self._log_default_lookup_failure(exc)
                return False
            if speaker is None:
                return False
        if getattr(speaker, "id", None) == self._speaker_id:
            return False
        return self._reattach(speaker, reason=self._select_reason)

    def _fill_silence(self, seconds: float) -> None:
        """Emit the silence chunks that ``seconds`` of lost time is worth (fixed time base)."""

        chunk_s = self._config.chunk_size / float(self._config.sample_rate)
        total = max(seconds, 0.0) + self._silence_carry_s
        count = int(total / chunk_s)
        self._silence_carry_s = total - count * chunk_s
        for _ in range(count):
            self._emit_silence_chunk()

    def _report_outage(self) -> None:
        """The tap is gone: one audio warning per outage, and tap_health says no_tap."""

        if not self._outage_warned:
            self._outage_warned = True
            self._emit_warning(t("audio.wasapi_outage"))
        self._health.mark_detached(t("audio.detail_device_vanished"), already_warned=True)

    def _recover_recorder(self, lost_label: str) -> bool:
        """Keep the clock running while the endpoint is gone, and re-open it.

        Emits zero chunks at real-time pace (the number due is derived from
        elapsed time, not from a sleep per chunk, so the silence cannot drift)
        and retries the normal open every ``_recovery_retry_s``, the first
        attempt only after one interval. Returns True once a recorder is open
        again, False when ``stop()`` ended the wait. Whatever a failed attempt
        partly opened is closed before the next one.
        """

        chunk_s = self._config.chunk_size / float(self._config.sample_rate)
        loss_start = self._clock()
        emitted = 0
        repeat_outage = self._outage_start is not None
        outage_start = self._outage_start if self._outage_start is not None else loss_start
        self._outage_start = outage_start
        # Following the call's endpoint: the next endpoint in the selector's order
        # is tried at once, and the outage is only reported after the grace period.
        next_attempt = loss_start + (0.0 if self._follow_active and not repeat_outage else self._recovery_retry_s)
        outage_pending = self._follow_active

        def fill_silence() -> None:
            nonlocal emitted
            due = int((self._clock() - loss_start) / chunk_s) - emitted
            for _ in range(max(due, 0)):
                self._emit_silence_chunk()
                emitted += 1

        while not self._stop_event.is_set():
            fill_silence()
            now = self._clock()
            if outage_pending and now - outage_start >= self._outage_grace_s:
                outage_pending = False
                self._report_outage()
            if now >= next_attempt:
                try:
                    opened = self._open_recorder(silent=True)
                except Exception:
                    opened = False
                if opened and self._stop_event.is_set():
                    return False
                if opened:
                    fill_silence()
                    self._health.mark_attached(self.device_label)
                    logger.info(
                        "WASAPI loopback re-attached: %s -> %s after %.1fs of silence filled.",
                        lost_label,
                        self._speaker_label,
                        self._clock() - loss_start,
                    )
                    return True
                self._close_recorder()
                next_attempt = self._clock() + self._recovery_retry_s
                continue
            self._sleep(min(chunk_s, max(next_attempt - now, 0.0)))
        return False

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
        except Exception as exc:
            self._log_default_lookup_failure(exc)
            return
        if current is None:
            return
        if getattr(current, "id", None) == self._speaker_id:
            return
        self._reattach(current)

    def _reattach(self, new_speaker: Any, *, reason: str | None = None) -> bool:
        """Swap the recorder to ``new_speaker`` without dropping buffered audio.

        Opens the new recorder before touching any shared state, so a failure
        here leaves the current (still-working) tap running untouched. The
        actual swap is guarded by ``_lock`` and aborts if ``stop()`` has
        already begun, so a concurrent shutdown can never race a freshly
        opened recorder into being silently leaked.
        """

        old_label = self._speaker_label
        new_label = _describe_speaker(new_speaker)
        open_started = self._clock()
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
            return False
        gap_s = max(self._clock() - open_started, 0.0)

        with self._lock:
            if self._stop_event.is_set():
                try:
                    new_recorder_cm.__exit__(None, None, None)
                except Exception:
                    pass
                return False
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

        if reason is None:
            logger.info(
                "WASAPI loopback default output endpoint changed, re-attaching: %s -> %s",
                old_label,
                new_label,
            )
            return True
        # The old recorder was not read while the new one opened: keep the tracks
        # aligned by filling that time with silence on the fixed time base.
        self._fill_silence(gap_s)
        logger.info(
            "WASAPI loopback switched endpoint: %s -> %s (reason: %s, gap: %d ms)",
            old_label,
            new_label,
            reason,
            round(gap_s * 1000),
        )
        return True

    def _read_loop(self) -> None:
        accum = np.zeros((0,), dtype=np.float32)
        shape_checked = False
        last_emit = time.monotonic()
        last_endpoint_check = self._clock()
        self._outage_warned = False
        self._outage_start = None
        pacing_timeout = (self._config.chunk_size / float(self._config.sample_rate)) * self._silence_timeout_multiplier

        while not self._stop_event.is_set():
            now = self._clock()
            poll_s = self._follow_poll_seconds if self._follow_active else self._endpoint_poll_seconds
            if self._following_default and (now - last_endpoint_check) >= poll_s:
                last_endpoint_check = now
                if self._follow_active:
                    if self._follow_endpoint():
                        accum = np.zeros((0,), dtype=np.float32)
                        shape_checked = False
                        last_emit = time.monotonic()
                        last_endpoint_check = self._clock()
                        continue
                else:
                    self._check_default_endpoint_drift()

            try:
                block = self._recorder.record(numframes=self._config.chunk_size)
            except Exception:
                # A read error is an outage, not the end of the call: a
                # vanished endpoint (headset off, USB unplugged) can come back
                # or be replaced by the new default. One warning per outage;
                # it re-arms only once a read has returned audio again.
                lost_label = self._speaker_label
                if not self._follow_active:
                    self._report_outage()
                self._close_recorder()
                if not self._recover_recorder(lost_label):
                    return
                accum = np.zeros((0,), dtype=np.float32)
                shape_checked = False
                last_emit = time.monotonic()
                last_endpoint_check = self._clock()
                continue

            if not shape_checked:
                shape_checked = True
                if not self._validate_first_block_shape(block):
                    self._emit_warning(t("audio.wasapi_bad_shape", rate=self._config.sample_rate))
                    self._degraded = True
                    return

            try:
                frames = int(block.shape[0]) if isinstance(block, np.ndarray) and block.ndim == 2 else 0

                if frames > 0:
                    self._outage_warned = False
                    self._outage_start = None
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
                self._emit_warning(t("audio.wasapi_read_error"))
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
