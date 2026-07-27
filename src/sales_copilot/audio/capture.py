from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

from sales_copilot.core.paths import resolve_app_resource

if TYPE_CHECKING:
    from sales_copilot.auth.feature_policy import FeaturePolicy

logger = logging.getLogger(__name__)

# PortAudio re-enumeration guard.
#
# sounddevice/PortAudio enumerates devices at first init; later device changes
# (Bluetooth connect/disconnect, A2DP<->HFP profile flaps) stay invisible and
# stale device indices point at dead CoreAudio objects. Re-initializing forces a
# fresh enumeration at session start. sd._terminate() while streams are open
# crashes PortAudio, so re-init is only allowed when zero streams are open. The
# lock + counter make that decision atomic across the mic and system streams.
_pa_lock = threading.Lock()
_active_stream_count = 0


def _reinit_portaudio_and_register(sd: Any) -> None:
    """Re-enumerate PortAudio devices when idle, then count this stream as open.

    Called immediately before a new InputStream is opened, with the already
    imported ``sounddevice`` module. Re-init only fires when no other stream is
    currently open (terminating PortAudio with live streams crashes it). The
    open-stream count is incremented under the same lock so two concurrent stream
    opens cannot both decide they are idle.
    """

    global _active_stream_count
    with _pa_lock:
        if _active_stream_count == 0:
            try:
                sd._terminate()
                sd._initialize()
                logger.info("PortAudio re-initialized (device re-enumeration) at session start.")
            except Exception:
                logger.warning(
                    "PortAudio re-init failed; continuing with existing device enumeration.",
                    exc_info=True,
                )
        _active_stream_count += 1


def _unregister_stream() -> None:
    """Drop this stream from the open-stream count after it has been closed."""

    global _active_stream_count
    with _pa_lock:
        if _active_stream_count > 0:
            _active_stream_count -= 1


def _active_portaudio_stream_count() -> int:
    with _pa_lock:
        return _active_stream_count


@dataclass(frozen=True, slots=True)
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "float32"
    chunk_size: int = 1024
    mic_device: int | str | None = None
    capture_method: Literal["mic", "audiotee", "blackhole", "wasapi", "replay"] = "audiotee"
    # Process the AudioTee tap follows for the prospect stream when
    # capture_method="audiotee". None (default) means "auto-detect the active
    # meeting app at stream start" -- see resolve_meeting_app_target(). Set
    # explicitly to skip auto-detection.
    target_process: str | None = None
    audiotee_path: str = str(resolve_app_resource("bin/audiotee"))
    # Prospect-stream routing gate, independent of `capture_method`. "blackhole"
    # (default, historical name) does NOT force the BlackHole virtual device --
    # it means "use whatever `capture_method` says" (audiotee video-tap,
    # blackhole, or mic-only) for the prospect stream. "audiotee_call" overrides
    # `capture_method` entirely and routes the prospect stream through the Pro
    # telephony process-tap (CallTapStream) for phone calls that bypass virtual
    # audio devices (iPhone-relay).
    prospect_source: Literal["blackhole", "audiotee_call"] = "blackhole"
    # Process whose audio the call-tap follows. avconferenced is the macOS
    # telephony daemon behind FaceTime/Phone-relay.
    call_process_name: str = "avconferenced"
    # Replay path (capture_method="replay"): directory holding a recorded
    # session as self.wav + prospect.wav (16 kHz mono), replayed through the
    # live pipeline instead of capturing real hardware. REPLAY_SESSION_DIR.
    replay_session_dir: str | None = None
    # Playback speed multiplier for the replay path (REPLAY_SPEED): 1.0 plays
    # the recording back at real-time pace, 2.0 at double speed.
    replay_speed: float = 1.0


@runtime_checkable
class AudioStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read(self) -> np.ndarray | None: ...


class _ManagedAudioStream(AbstractContextManager[AudioStream]):
    def __init__(self, stream: AudioStream) -> None:
        self._stream = stream

    def __enter__(self) -> AudioStream:
        self._stream.start()
        return self._stream

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stream.stop()
        return False


def managed_audio_stream(stream: AudioStream) -> AbstractContextManager[AudioStream]:
    """Context manager that guarantees stream stop on exit."""

    return _ManagedAudioStream(stream)


def _drop_oldest_and_put(q: queue.Queue[np.ndarray], item: np.ndarray) -> None:
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def _find_pid_posix(process_name: str, *, exact: bool) -> int | None:
    """Find a process PID using ``pgrep`` (POSIX only)."""
    args = ["pgrep", "-x", process_name] if exact else ["pgrep", "-f", process_name]
    try:
        out = subprocess.check_output(args, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    text = out.decode().strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[0].strip())
    except ValueError:
        return None


def _find_pid_win32(process_name: str, *, exact: bool) -> int | None:
    """Find a process PID using ``tasklist`` (Windows only).

    On Windows, process names typically end with ``.exe``. For **exact** match
    the filter checks the image name literally (with and without the ``.exe``
    suffix so callers can pass either form). For **fuzzy** match every running
    process is listed and filtered by substring match against the image name.
    """
    try:
        if exact:
            # Try the name as-is first, then with .exe appended if not already present.
            candidates = [process_name]
            if not process_name.lower().endswith(".exe"):
                candidates.append(process_name + ".exe")
            for candidate in candidates:
                out = subprocess.check_output(
                    ["tasklist", "/FI", f"IMAGENAME eq {candidate}", "/NH", "/FO", "CSV"],
                    stderr=subprocess.DEVNULL,
                )
                text = out.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                # CSV: "image.exe","1234","Services","0","12,345 K"
                first_line = text.splitlines()[0].strip()
                if not first_line or first_line.startswith("INFO:"):
                    continue
                parts = first_line.split(",")
                if len(parts) >= 2:
                    pid_str = parts[1].strip('"').strip()
                    if pid_str.isdigit():
                        return int(pid_str)
            return None

        # Fuzzy: list all processes and filter by substring.
        out = subprocess.check_output(
            ["tasklist", "/NH", "/FO", "CSV"],
            stderr=subprocess.DEVNULL,
        )
        needle = process_name.lower()
        for line in out.decode("utf-8", errors="replace").strip().splitlines():
            parts = line.split(",")
            if len(parts) < 2:
                continue
            image = parts[0].strip('"').strip()
            if needle in image.lower():
                pid_str = parts[1].strip('"').strip()
                if pid_str.isdigit():
                    return int(pid_str)
        return None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _find_pid_exact(process_name: str) -> int | None:
    """Find a process PID by exact name match (cross-platform).

    On POSIX this delegates to ``pgrep -x``; on Windows to ``tasklist``
    with an exact image-name filter.
    """
    if sys.platform == "win32":
        return _find_pid_win32(process_name, exact=True)
    return _find_pid_posix(process_name, exact=True)


def _find_pid_fuzzy(process_name: str) -> int | None:
    """Find a process PID by substring match (cross-platform).

    On POSIX this delegates to ``pgrep -f``; on Windows it lists every
    running process and filters by substring against the image name.
    """
    if sys.platform == "win32":
        return _find_pid_win32(process_name, exact=False)
    return _find_pid_posix(process_name, exact=False)


def _find_pid(process_name: str) -> int | None:
    return _find_pid_exact(process_name) or _find_pid_fuzzy(process_name)


# Common macOS meeting apps for the FREE-tier AudioTee video-prospect tap,
# checked in this order by resolve_meeting_app_target(). Google Meet has no
# dedicated desktop app, so it is detected via its host browser process
# instead; Teams and Zoom ship their own macOS app processes.
MEETING_APP_CANDIDATES: tuple[str, ...] = ("Google Chrome", "Microsoft Teams", "zoom.us")


def resolve_meeting_app_target(
    candidates: Sequence[str] = MEETING_APP_CANDIDATES,
) -> tuple[str | None, int | None, list[str]]:
    """Detect which known meeting app is running, so AudioTee needs no manual config.

    Checks each candidate process name with an exact-match ``pgrep -x`` --
    deliberately not the fuzzy fallback in ``_find_pid``, since a broad
    substring match here would make unrelated helper processes look like
    "another app is open" and defeat the ambiguity check below.

    Returns ``(process_name, pid, running_candidates)``:

    - ``process_name``/``pid`` are set only when *exactly one* candidate
      process is running -- that is the only case auto-detection can trust.
    - ``running_candidates`` lists every candidate found running (0, 1, or
      several), so a caller can build an actionable message: which app to
      open (0 found) or which extra app to close (more than 1 found).

    Known caveat: matching by process name only confirms the app is open, not
    that it is actively in a meeting. For Chrome specifically this cannot
    distinguish "running Google Meet" from "running anything unrelated", and
    tapping the main "Google Chrome" process may not carry tab audio if
    Chrome renders it through a helper/utility subprocess instead -- this has
    not been verified against a real Google Meet call.
    """

    running: list[tuple[str, int]] = []
    for name in candidates:
        pid = _find_pid_exact(name)
        if pid is not None:
            running.append((name, pid))
    if len(running) == 1:
        name, pid = running[0]
        return name, pid, [name]
    return None, None, [name for name, _ in running]


def _meeting_app_detection_warning(running_candidates: list[str]) -> str:
    """Build the actionable warning shown when meeting-app auto-detection fails."""

    if running_candidates:
        apps = ", ".join(running_candidates)
        return (
            f"Meerdere meeting-apps tegelijk actief ({apps}); de copilot kan niet automatisch "
            "kiezen welke het gesprek is. Sluit de app(s) die je niet gebruikt, of zet "
            "TARGET_PROCESS_NAME handmatig in .env. Prospect-audio valt terug op BlackHole."
        )
    return (
        "Geen meeting-app gevonden (Google Chrome / Microsoft Teams / zoom.us). Open Google Meet, "
        "Microsoft Teams of Zoom voordat je het gesprek start, of zet TARGET_PROCESS_NAME handmatig "
        "in .env. Prospect-audio valt terug op BlackHole."
    )


def _audiotee_setup_failure_message(process_label: str, diagnostic: str) -> str:
    """Build the actionable warning shown when the AudioTee tap fails to start.

    The most common real-world cause is a missing macOS Core Audio
    process-tap permission (added in macOS 14.4), so granting that permission
    is the primary suggested fix. The exact OSStatus/error text AudioTee
    reports for a permission denial has not been confirmed against a real
    denied-permission Mac, so this stays deliberately general instead of
    pattern-matching one specific error string.
    """

    detail = f" (audiotee: {diagnostic})" if diagnostic else ""
    return (
        f"AudioTee kon geen audio-tap starten voor '{process_label}'{detail}. Meest voorkomende "
        "oorzaak: de macOS-toestemming voor Core Audio process-taps ontbreekt. Geef toestemming via "
        "Systeeminstellingen > Privacy en beveiliging > Audio-opname (macOS 14.4+) en start het "
        "gesprek opnieuw. Tot die tijd blijft de prospect-stream stil."
    )


def normalize_capture_method(method: str | None, *, platform: str = sys.platform) -> str:
    """Resolve ``capture_method`` to a platform-valid value.

    ``platform`` is injectable so tests can exercise the win32 branch on
    macOS CI without a real Windows host. On win32, the macOS-only methods
    (``audiotee``/``blackhole``) and an unset value normalize to ``wasapi``;
    ``mic``/``wasapi`` are honored as-is. On any other platform, the
    Windows-only ``wasapi`` and an unset value normalize to ``audiotee``;
    other values are left unchanged.
    """

    normalized = (method or "").strip().lower()
    if platform == "win32":
        if normalized in {"audiotee", "blackhole"}:
            logger.warning(
                "capture_method=%r is macOS-only; using 'wasapi' on Windows instead.",
                normalized,
            )
            return "wasapi"
        if not normalized:
            return "wasapi"
        return normalized
    if normalized == "wasapi":
        logger.warning("capture_method='wasapi' is Windows-only; using 'audiotee' instead.")
        return "audiotee"
    if not normalized:
        return "audiotee"
    return normalized


def resolve_prospect_source(
    env_value: str | None,
    start_call_config: dict[str, Any] | None,
    *,
    platform: str = sys.platform,
) -> Literal["blackhole", "audiotee_call"]:
    """Resolve the prospect-stream source.

    The start_call config (``prospect_source`` key) takes precedence over the
    ``PROSPECT_SOURCE`` env value so an operator can switch to the phone-call
    tap per call without restarting. Unknown values fall back to ``blackhole``
    so a typo never silently disables the prospect stream.

    ``platform`` is injectable for tests. On win32 the phone-call tap
    (``audiotee_call``) is never returned -- WASAPI whole-endpoint loopback
    has no equivalent to the macOS telephony process-tap, so the value
    neutralizes to ``blackhole`` instead.
    """

    raw: str | None = None
    if isinstance(start_call_config, dict):
        candidate = start_call_config.get("prospect_source")
        if isinstance(candidate, str) and candidate.strip():
            raw = candidate
    if raw is None:
        raw = env_value
    source = (raw or "blackhole").strip().lower()
    if source not in {"blackhole", "audiotee_call"}:
        logger.warning("Invalid prospect_source=%r, falling back to 'blackhole'.", raw)
        return "blackhole"
    if platform == "win32" and source == "audiotee_call":
        logger.warning(
            "prospect_source='audiotee_call' (phone-call tap) is not available on Windows; "
            "falling back to 'blackhole'."
        )
        return "blackhole"
    return source  # type: ignore[return-value]


def make_audio_warning_broadcaster(
    loop: asyncio.AbstractEventLoop,
    broadcast_warning: Callable[[dict[str, Any]], Awaitable[None]],
) -> Callable[[dict[str, Any]], None]:
    """Build a thread-safe callback that broadcasts an audio_warning payload.

    Streams run their subprocess readers in background threads, so a warning
    raised there (subprocess crash, PID loss) cannot await the coaching
    broadcast directly. The returned callable schedules the async broadcast on
    the orchestrator loop via ``run_coroutine_threadsafe`` and never raises into
    the caller thread.
    """

    def _emit(payload: dict[str, Any]) -> None:
        async def _run() -> None:
            try:
                await broadcast_warning(payload)
            except Exception:
                logger.warning("Failed to broadcast audio_warning payload", exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(_run(), loop)
        except Exception:
            logger.warning("Failed to schedule audio_warning broadcast", exc_info=True)

    return _emit


def _import_sounddevice():
    import sounddevice as sd  # type: ignore[import-not-found]

    return sd


def list_audio_devices() -> list[dict[str, Any]]:
    sd = _import_sounddevice()
    devices = sd.query_devices()
    listed: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        listed.append({"index": index, **dict(device)})
    return listed


def get_default_output_device() -> dict[str, Any] | None:
    sd = _import_sounddevice()
    default_device = getattr(sd, "default", None)
    output_index = None
    if default_device is not None:
        try:
            output_index = default_device.device[1]
        except (AttributeError, IndexError, TypeError):
            output_index = None
    if output_index in (None, -1):
        return None
    devices = list_audio_devices()
    for device in devices:
        if device["index"] == output_index:
            return device
    return None


def find_input_device(device_name: str) -> dict[str, Any] | None:
    needle = device_name.strip().lower()
    if not needle:
        return None
    for device in list_audio_devices():
        if int(device.get("max_input_channels", 0) or 0) <= 0:
            continue
        if needle in str(device.get("name", "")).lower():
            return device
    return None


def get_default_input_device() -> dict[str, Any] | None:
    sd = _import_sounddevice()
    default_device = getattr(sd, "default", None)
    input_index = None
    if default_device is not None:
        try:
            input_index = default_device.device[0]
        except (AttributeError, IndexError, TypeError):
            input_index = None
    if input_index in (None, -1):
        return None
    devices = list_audio_devices()
    for device in devices:
        if device["index"] == input_index:
            return device
    return None


def check_device_health(device_name: str) -> tuple[bool, float]:
    """Record 2s from device, return (has_audio, mean_level)."""

    sd = _import_sounddevice()
    if device_name.strip().lower() == "default":
        device = get_default_input_device()
    else:
        device = find_input_device(device_name)
    if device is None:
        return False, 0.0

    samplerate = 48000
    frames = int(2 * samplerate)
    channels = min(max(int(device.get("max_input_channels", 1) or 1), 1), 2)
    record_seconds = frames / samplerate

    def _await_recording() -> None:
        try:
            sd.wait()
        except Exception:
            pass

    try:
        recording = sd.rec(
            frames,
            samplerate=samplerate,
            channels=channels,
            device=int(device["index"]),
            dtype="float32",
        )
        # sd.wait() has no native timeout; a wedged Core Audio device would block
        # this startup probe forever. Bound the wait to the capture duration plus
        # a small margin and abandon the read if the device never delivers.
        waiter = threading.Thread(target=_await_recording, name="device-health-wait", daemon=True)
        waiter.start()
        waiter.join(timeout=record_seconds + 3.0)
        if waiter.is_alive():
            try:
                sd.stop()
            except Exception:
                pass
            return False, 0.0
    except Exception:
        return False, 0.0

    level = float(np.mean(np.abs(recording)))
    return level > 0.0005, level


class MicStream:
    def __init__(self, config: AudioConfig, *, queue_maxsize: int = 100, device: int | str | None = None) -> None:
        self._config = config
        self._device = device
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._stream = None
        self._lock = threading.Lock()
        self._chunks_received = 0

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def device_label(self) -> str:
        return "default" if self._device is None else str(self._device)

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return

            sd = _import_sounddevice()

            def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
                self._chunks_received += 1
                _drop_oldest_and_put(self._queue, indata.copy())

            _reinit_portaudio_and_register(sd)
            try:
                self._stream = sd.InputStream(
                    samplerate=self._config.sample_rate,
                    channels=self._config.channels,
                    dtype=self._config.dtype,
                    blocksize=self._config.chunk_size,
                    callback=callback,
                    device=self._device,
                )
                self._stream.start()
            except Exception:
                self._stream = None
                _unregister_stream()
                raise

    def stop(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            stream = self._stream
            self._stream = None
            try:
                stream.stop()
            except Exception:
                logger.warning("MicStream stop() raised on device=%s", self.device_label, exc_info=True)
            try:
                stream.close()
            except Exception:
                logger.warning("MicStream close() raised on device=%s", self.device_label, exc_info=True)
            finally:
                _unregister_stream()

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


class AudioTeeStream:
    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        tap_all: bool = False,
        exclude_processes: tuple[str, ...] | None = None,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        # tap_all taps every process' system output instead of a single target
        # process. That is what a video call needs: the far side plays through
        # the system mix regardless of which app (Chrome/Zoom/FaceTime) hosts it,
        # so the tap follows the whole system mix and skips target-process
        # resolution entirely.
        self._tap_all = tap_all
        # Process names that must be excluded from the tap. This is resolved to
        # PIDs at tap-start (and can be re-resolved on restart) so the exclusion
        # is effective at the moment AudioTee opens the tap, not at an earlier
        # point-in-time guard check.
        self._exclude_processes = exclude_processes or ()
        self._on_warning = on_warning
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks_received = 0
        self._degraded = False
        self._setup_failure_emitted = False
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def device_label(self) -> str:
        if self._tap_all:
            return "audiotee:all"
        return f"audiotee:{self._config.target_process or 'unknown'}"

    def start(self) -> None:
        """Start the AudioTee tap, either whole-system (tap_all) or targeted.

        In targeted mode the configured target process is resolved to a PID and
        passed via ``--include-processes``. In tap_all mode that resolution is
        skipped and AudioTee taps every process' system output, which is what a
        video call needs regardless of which app hosts it.

        Startup failures (missing binary, no target process, target process not
        running) do not raise: they log an error, emit an actionable
        audio_warning, and leave the stream in a degraded (silent) state,
        matching CallTapStream. A raised exception here would propagate uncaught
        out of the background AudioBufferer/DirectWhisperEngine task with no
        dashboard warning -- exactly the "cryptic error" / silent-failure mode
        this fixes.
        """

        with self._lock:
            if self._proc is not None:
                return

            if not os.path.exists(self._config.audiotee_path):
                self._emit_warning(
                    f"AudioTee-binary niet gevonden ({self._config.audiotee_path}); "
                    "prospect-tap kan niet starten."
                )
                self._degraded = True
                return

            chunk_duration_s = self._config.chunk_size / float(self._config.sample_rate)
            args = [
                self._config.audiotee_path,
                "--sample-rate",
                str(self._config.sample_rate),
                "--chunk-duration",
                str(chunk_duration_s),
            ]

            if not self._tap_all:
                if not self._config.target_process:
                    self._emit_warning(
                        "Geen doelproces ingesteld voor de AudioTee-prospect-tap en geen "
                        "meeting-app automatisch gedetecteerd."
                    )
                    self._degraded = True
                    return

                pid = _find_pid(self._config.target_process)
                if pid is None:
                    self._emit_warning(
                        f"Meeting-app '{self._config.target_process}' niet (meer) gevonden. "
                        "Open de app en start het gesprek opnieuw."
                    )
                    self._degraded = True
                    return

                args += ["--include-processes", str(pid)]

            excluded_pids = self._resolve_excluded_pids()
            if excluded_pids:
                # AudioTee expects space-separated PIDs as distinct argv tokens
                # (`--exclude-processes 1234 5678`); a comma-joined single value
                # ("1234,5678") is rejected with "Invalid value" and aborts the
                # whole tap. Spread each PID as its own argument.
                args += ["--exclude-processes", *(str(pid) for pid in sorted(excluded_pids))]

            self._stop_event.clear()
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
            except OSError:
                self._emit_warning(f"Kon AudioTee niet starten ({self._config.audiotee_path}).")
                self._degraded = True
                return

            if proc.stdout is None or proc.stderr is None:
                self._emit_warning("AudioTee-subprocess heeft geen stdout/stderr pipes.")
                self._degraded = True
                return

            self._proc = proc

            # Start the stderr drainer before the stdout reader: an immediate
            # setup failure makes stdout hit EOF right away, and
            # _maybe_emit_setup_failure() (called from the stdout thread) joins
            # this thread to pick up its diagnostic output. Starting it first
            # guarantees self._stderr_thread is already set by then.
            self._stderr_thread = threading.Thread(target=self._drain_stderr_loop, name="audiotee-stderr", daemon=True)
            self._stderr_thread.start()

            self._reader_thread = threading.Thread(target=self._read_stdout_loop, name="audiotee-stdout", daemon=True)
            self._reader_thread.start()

    def _read_stdout_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None

        # AudioTee emits raw PCM to stdout. With `--sample-rate`, it commonly uses int16 output.
        input_dtype = np.dtype("int16")
        sample_bytes = input_dtype.itemsize
        bytes_per_chunk = self._config.chunk_size * self._config.channels * sample_bytes

        buffer = bytearray()

        while not self._stop_event.is_set():
            chunk = self._proc.stdout.read(bytes_per_chunk)
            if not chunk:
                self._maybe_emit_setup_failure()
                break
            buffer.extend(chunk)
            if len(buffer) < bytes_per_chunk:
                continue

            raw = bytes(buffer[:bytes_per_chunk])
            del buffer[:bytes_per_chunk]

            frame = np.frombuffer(raw, dtype=input_dtype).reshape((-1, self._config.channels))

            if self._config.dtype == "float32":
                frame = frame.astype(np.float32) / 32768.0
            else:
                frame = frame.astype(np.dtype(self._config.dtype))

            self._chunks_received += 1
            _drop_oldest_and_put(self._queue, frame)

    def _drain_stderr_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stderr is not None

        while not self._stop_event.is_set():
            data = self._proc.stderr.readline()
            if not data:
                break
            text = data.decode("utf-8", "replace").strip()
            if not text:
                continue
            with self._stderr_lock:
                self._stderr_lines.append(text)
                if len(self._stderr_lines) > 20:
                    self._stderr_lines.pop(0)

    def _maybe_emit_setup_failure(self) -> None:
        """Warn once when the subprocess exits before delivering any audio.

        A requested stop() sets _stop_event before the stdout pipe closes, so
        this only fires for an *unrequested* early exit -- the signature of a
        failed Core Audio process-tap setup (most commonly a missing
        process-tap permission, occasionally a target process that exited
        between PID-resolution and tap creation).

        The stdout and stderr pipes both close once the subprocess exits, so
        the stderr-draining thread should already be finished or finishing by
        the time this runs; a short join guards against the remaining
        thread-scheduling race so the diagnostic text is reliably captured
        before the warning is built.
        """

        if self._stop_event.is_set() or self._chunks_received > 0 or self._setup_failure_emitted:
            return
        self._setup_failure_emitted = True
        stderr_thread = self._stderr_thread
        if stderr_thread is not None and stderr_thread is not threading.current_thread():
            stderr_thread.join(timeout=0.5)
        with self._stderr_lock:
            diagnostic = " | ".join(self._stderr_lines[-3:])
        if self._tap_all:
            process_label = "alle processen"
        else:
            process_label = self._config.target_process or "onbekend proces"
        self._emit_warning(_audiotee_setup_failure_message(process_label, diagnostic))

    def _resolve_excluded_pids(self) -> set[int]:
        """Resolve the current PIDs of any processes that must be excluded.

        Resolving at tap-start (and on every restart) closes the TOCTOU window
        between the telephony guard and the actual Core Audio tap: a process
        that was not running when the guard ran will still be excluded when the
        tap opens.
        """

        pids: set[int] = set()
        for name in self._exclude_processes:
            pid = _find_pid(name)
            if pid is not None:
                pids.add(pid)
        return pids

    def _emit_warning(self, message: str) -> None:
        logger.error("AudioTee: %s", message)
        if self._on_warning is None:
            return
        try:
            self._on_warning({"type": "audio_warning", "stream": "prospect", "message": message})
        except Exception:
            logger.warning("on_warning callback raised for AudioTee tap", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            if self._proc is None:
                return

            proc = self._proc
            self._stop_event.set()
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            finally:
                self._proc = None

            if self._reader_thread is not None:
                self._reader_thread.join(timeout=1)
                self._reader_thread = None
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1)
                self._stderr_thread = None

            # Close the subprocess pipes after the reader threads have stopped
            # so the stdout/stderr fds are released promptly instead of lingering
            # until garbage collection.
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


async def check_streams_liveness(
    streams_and_labels: list[tuple[AudioStream, str]],
    broadcast_warning: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    timeout: float = 2.0,
) -> list[str]:
    """Warn when an opened stream delivers no audio within ``timeout`` seconds.

    After stream-open, each stream is expected to push chunks continuously
    regardless of model warmup. A stream that reports zero chunks after the
    timeout points at a dead/stale device (e.g. a Bluetooth profile flap). For
    each silent stream a structured ``audio_warning`` event is broadcast on the
    coaching channel so the dashboard can surface it. This never raises and never
    hard-fails the call: live streams keep running.

    Returns the list of stream labels that were flagged as silent.
    """

    await asyncio.sleep(timeout)

    silent: list[str] = []
    for stream, label in streams_and_labels:
        received = getattr(stream, "chunks_received", None)
        if not isinstance(received, int) or received > 0:
            continue
        device_label = getattr(stream, "device_label", "unknown")
        logger.warning("Mic stream silent/dead: device=%s stream=%s", device_label, label)
        silent.append(label)
        try:
            await broadcast_warning(
                {
                    "type": "audio_warning",
                    "stream": label,
                    "message": (
                        f"Geen audio van {label}-stream (device={device_label}). "
                        "Controleer je microfoon/audio-routing."
                    ),
                }
            )
        except Exception:
            logger.warning("Failed to broadcast audio_warning for stream=%s", label, exc_info=True)
    return silent


class DualAudioCapture:
    def __init__(self, config: AudioConfig, *, feature_policy: FeaturePolicy | None = None) -> None:
        self._config = config
        if feature_policy is None:
            from sales_copilot.auth.feature_policy import get_feature_policy

            feature_policy = get_feature_policy()
        self._feature_policy = feature_policy

    def create(
        self,
        *,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[AudioStream, AudioStream | None]:
        # The factory is the authoritative platform-normalization point:
        # direct AudioConfig() callers (RecorderEngine, wizard/meter.py, tests)
        # may pass a macOS/Windows-specific capture_method or prospect_source
        # straight through, so both are re-normalized here -- before the
        # entitlement branch and before the capture_method branches -- rather
        # than trusting whatever the env-level loaders already computed.
        capture_method = normalize_capture_method(self._config.capture_method, platform=sys.platform)
        prospect_source = resolve_prospect_source(self._config.prospect_source, None, platform=sys.platform)

        if capture_method == "replay":
            # Replay a recorded session through the live pipeline: both
            # streams come from WAV files, no mic or meeting app is touched.
            # Branches before MicStream construction so replay mode never
            # initialises PortAudio.
            return self._create_replay_streams()

        mic: AudioStream = MicStream(self._config, device=self._config.mic_device)

        def _warn(message: str) -> None:
            logger.warning("%s", message)
            if on_warning is not None:
                on_warning({"type": "audio_warning", "stream": "prospect", "message": message})

        # prospect_source takes precedence for the prospect/system stream. When
        # set to audiotee_call the prospect audio comes from the telephony
        # process-tap regardless of capture_method, so a phone call can be
        # captured even with the default audiotee/mic self-stream setup.
        if prospect_source == "audiotee_call":
            from sales_copilot.auth.feature_policy import FEATURE_CALLTAP

            if not self._feature_policy.allows(FEATURE_CALLTAP):
                _warn("Telefonie-capture via AudioTee vereist een geldige Sales Pro-licentie.")
                from .blackhole import BlackHoleStream

                return mic, BlackHoleStream(self._config)
            from .calltap import CallTapStream

            return mic, CallTapStream(self._config, on_warning=on_warning)

        if capture_method == "mic":
            return mic, None
        if capture_method == "audiotee":
            # tap_all: capture the whole system output mix natively via AudioTee
            # (no BlackHole, no Multi-Output, no output switching). Meeting audio
            # (Google Meet-in-Chrome, Teams, Zoom) is rendered by helper subprocesses,
            # so a single-process name tap misses it. The free tier still excludes the
            # telephony daemon (avconferenced) so phone calls stay a Pro feature.
            from .telephony_guard import get_telephony_tap_guard

            guard = get_telephony_tap_guard()
            system: AudioStream = AudioTeeStream(
                self._config,
                tap_all=True,
                exclude_processes=guard.excluded_telephony_processes(),
                on_warning=on_warning,
            )
            return mic, system
        if capture_method == "blackhole":
            from .blackhole import BlackHoleStream

            system = BlackHoleStream(self._config)
            return mic, system
        if capture_method == "wasapi":
            from .wasapi import WasapiLoopbackStream

            return mic, WasapiLoopbackStream(self._config, on_warning=on_warning)

        raise ValueError(f"Unsupported capture_method: {capture_method}")

    def _create_replay_streams(self) -> tuple[AudioStream, AudioStream]:
        """Build the (self, prospect) stream pair for capture_method="replay".

        Both streams replay a recorded session directory (self.wav = the rep,
        prospect.wav = the prospect) through ReplayAudioStream in paced mode,
        so the recording plays through the same live pipeline (transcription,
        detection, dashboard) at REPLAY_SPEED x real-time without touching
        audio hardware. Raises a clear error when the directory or WAVs are
        missing instead of failing later with an obscure wave/open traceback.
        """
        from .replay import ReplayAudioStream

        session_dir = (self._config.replay_session_dir or "").strip()
        if not session_dir:
            raise ValueError(
                "AUDIO_CAPTURE_METHOD=replay requires REPLAY_SESSION_DIR to point at a "
                "recorded session directory (e.g. data/sessions/<id>/) containing "
                "self.wav and prospect.wav."
            )
        base = Path(session_dir).expanduser()
        self_wav = base / "self.wav"
        prospect_wav = base / "prospect.wav"
        missing = [str(p) for p in (self_wav, prospect_wav) if not p.is_file()]
        if not base.is_dir() or missing:
            raise FileNotFoundError(
                "Replay session not found or incomplete: expected 16 kHz mono WAVs at "
                f"{self_wav} and {prospect_wav} (missing: {', '.join(missing) if missing else base}). "
                "Record a call first (RECORD_AUDIO=true) or point REPLAY_SESSION_DIR at an "
                "existing data/sessions/<id>/ directory."
            )
        speed = max(0.1, self._config.replay_speed)
        logger.info(
            "Replay capture: session=%s speed=%.1fx (self=%s, prospect=%s)",
            base,
            speed,
            self_wav.name,
            prospect_wav.name,
        )
        self_stream: AudioStream = ReplayAudioStream(
            self_wav,
            chunk_size_frames=self._config.chunk_size,
            speed_multiplier=speed,
            paced=True,
        )
        prospect_stream: AudioStream = ReplayAudioStream(
            prospect_wav,
            chunk_size_frames=self._config.chunk_size,
            speed_multiplier=speed,
            paced=True,
        )
        return self_stream, prospect_stream
