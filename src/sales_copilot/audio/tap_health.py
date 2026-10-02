"""Runtime tap health: tell "tap open, no signal" apart from "no tap at all".

Both states look identical from the outside -- no transcript, no error, no log
line -- yet they need opposite responses. "No tap" is a setup problem (missing
Core Audio process-tap permission, binary gone, subprocess died). "Tap open, no
signal" is a routing problem (the call audio plays on another machine, or this
machine's output is muted). On 2026-09-05 an hour went into debugging a tap that
was never broken, because nothing in the running system could say which of the
two it was.

The streams already know the answer; they just never said it out loud:

* ``AudioTeeStream`` tracked ``_degraded`` privately and threw it away after
  ``start()`` returned, so nothing could ask "is this tap actually attached?".
* A tap that died *after* delivering audio produced no warning at all, because
  the setup-failure path deliberately skips any stream that once delivered a
  chunk.
* Every stream counted chunks, and every stream discarded the sample values, so
  a tap delivering nothing but digital silence was indistinguishable from a
  healthy one.

This module turns that into one small, thread-cheap tracker each stream owns,
plus a bounded periodic INFO heartbeat that reports the verdict per stream.

Threshold discipline: the signal floor is a *digital-silence* floor, not a
speech floor. 0.0005 is not a new number invented here -- it is the value
``check_device_health`` already uses to decide "this device delivered genuine
audio", and the same value the Windows loopback path used for the same question
under its own name (``_REAL_AUDIO_RMS_THRESHOLD``, now folded into this module
so there is one threshold instead of two that can drift). It answers "are these
frames anything other than zeros", not "is somebody speaking": the speech floor
is ``VAD_MIN_RMS_PROSPECT`` (0.008), sixteen times higher, and using that here
would report a real but quiet call as a dead tap. Override with
``AUDIO_TAP_SIGNAL_FLOOR_RMS`` when a setup needs a different floor.

Mid-call signal loss (added 2026-09-10). The four states above answer "did this
tap ever work". They cannot see a tap that worked for sixteen minutes and then
stopped, because ``signal_chunks > 0`` latches for the rest of the call. A
40-minute Teams call on 2026-09-10 lost 23 minutes of prospect audio exactly
that way: the recorder kept writing, the file kept growing, and every sample in
the gap was zero. Nothing warned, and the transcript afterwards read as if the
prospect had gone quiet.

Lowering the silence floor would not have caught it and would have started
crying wolf on ordinary pauses. The discriminator is a different measurement
entirely, and the recording itself hands it over: a live capture of a silent
source always carries a noise floor (11 to 175 RMS on int16 in the pauses of
that same call), while a stopped tap writes samples that are *exactly* zero. So
``observe`` now separates "below the floor" from "bit-for-bit zero", counts the
consecutive all-zero frames, and ``ATTACHED_SIGNAL_LOST`` fires only on a
sustained run of them after the stream has already proved it can carry audio.
An ordinary pause resets that run on its first non-zero frame, so it can never
trigger. Threshold: ``AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from sales_copilot.core.config import env_float

logger = logging.getLogger(__name__)

# Digital-silence floor on the float32 [-1, 1] scale. See the module docstring
# for why this is deliberately not the VAD speech floor.
DEFAULT_SIGNAL_FLOOR_RMS = 0.0005

# Heartbeat cadence. One INFO line per stream per interval: enough to prove the
# tap is alive during a call, sparse enough not to bury the log (a 45-minute
# call yields ~90 lines per stream).
DEFAULT_HEARTBEAT_SECONDS = 30.0

# An attached tap that has delivered zero frames for this long is a fault, not a
# quiet moment: an open Core Audio tap pushes buffers continuously whether or not
# anything is playing through it.
DEFAULT_NO_FRAMES_WARN_SECONDS = 10.0

# An attached tap whose frames are all digital silence *and that has never once
# carried signal this call* is warned about after this long. Deliberately far
# past any natural pause in a sales call, and permanently disarmed by the first
# audible frame, so an ordinary silence can never trigger it.
DEFAULT_SILENT_WARN_SECONDS = 120.0

# A stream that has already carried audio and then delivers nothing but
# bit-for-bit zeros for this long has lost its source. 30 seconds is the
# reporter's proposal and it survives the arithmetic: the longest pause in the
# 2026-09-10 call that this is built from was well under 10 seconds, and every
# one of those pauses carried a noise floor, so they reset the run instead of
# extending it. The threshold therefore only sets how much audio is lost before
# the operator hears about it -- half a minute of a 40-minute call -- and never
# decides whether a pause counts as a fault. Override with
# ``AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS``.
DEFAULT_SIGNAL_LOST_WARN_SECONDS = 30.0

SILENCE_FLOOR_DB = -120.0

# Bounded auto-recovery for a tap that stays attached but stops carrying
# anything but digital silence (ATTACHED_SIGNAL_LOST). A dead subprocess
# already gets its own restart-once path (CallTapStream._supervise_loop); this
# is for the case that path can never see, because the subprocess never
# exits -- it just stops delivering real samples, most likely because macOS
# moved the audio route out from under it (device/output switch). Bounded to
# 3 attempts so a route that will never come back does not retry forever.
# Spacing is counted in monitor beats rather than wall-clock seconds: the
# heartbeat interval itself is the unit of time (30s in production), so this
# composes with whatever cadence the caller configures instead of needing its
# own clock. Scoped to the whole call, not per drop: a route that keeps
# flapping does not get a fresh budget of 3 every time it drops.
DEFAULT_REATTACH_MAX_ATTEMPTS = 3
_REATTACH_BACKOFF_BEATS: tuple[int, ...] = (1, 2, 4)

#: Marker recorded in the transcript for a forced reattach attempt, on the
#: same ``TRANSCRIPT_MARKER_TYPE`` channel as ``SIGNAL_LOST_MARKER`` below.
REATTACH_MARKER = "audio_tap_reattached"


class TapAttachState(StrEnum):
    """Lifecycle of the tap itself, independent of whether audio flows."""

    NOT_STARTED = "not_started"
    ATTACHED = "attached"
    ATTACH_FAILED = "attach_failed"
    DETACHED = "detached"


class TapSignalState(StrEnum):
    """The distinction this module exists to make."""

    #: The tap was never attached, or it attached and then died.
    NO_TAP = "no_tap"
    #: The tap is attached but has delivered no frames at all.
    ATTACHED_NO_FRAMES = "attached_no_frames"
    #: The tap is attached and frames are arriving, all at or below the
    #: digital-silence floor.
    ATTACHED_SILENT = "attached_silent"
    #: The tap is attached and has carried audible signal.
    ATTACHED_SIGNAL = "attached_signal"
    #: The tap carried audible signal earlier this call and is now delivering an
    #: unbroken run of bit-for-bit zero frames. Not a pause: a pause has a noise
    #: floor, and the first non-zero sample ends this state.
    ATTACHED_SIGNAL_LOST = "attached_signal_lost"


def _rms_dbfs(rms: float) -> float:
    if rms <= 0.0:
        return SILENCE_FLOOR_DB
    return max(SILENCE_FLOOR_DB, 20.0 * math.log10(rms))


def resolve_signal_floor_rms() -> float:
    """Read the configurable digital-silence floor, falling back to the default."""

    value = env_float("AUDIO_TAP_SIGNAL_FLOOR_RMS", DEFAULT_SIGNAL_FLOOR_RMS)
    if value is None or value <= 0.0:
        return DEFAULT_SIGNAL_FLOOR_RMS
    return value


def resolve_signal_lost_seconds() -> float:
    """Read how long an all-zero run must last before it counts as signal loss."""

    value = env_float("AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS", DEFAULT_SIGNAL_LOST_WARN_SECONDS)
    if value is None or value <= 0.0:
        return DEFAULT_SIGNAL_LOST_WARN_SECONDS
    return value


@dataclass(frozen=True, slots=True)
class TapHealth:
    """Point-in-time answer to "is this tap attached, and is anything coming in?"."""

    state: TapSignalState
    attach_state: TapAttachState
    target: str
    detail: str
    chunks: int
    signal_chunks: int
    attached_seconds: float
    #: Seconds since the last frame at or above the signal floor. ``None`` when
    #: no such frame has ever arrived on this stream.
    seconds_since_signal: float | None
    #: Seconds the tap has been attached without a single frame arriving.
    #: ``None`` once any frame has arrived.
    seconds_without_frames: float | None
    #: Frames in the current unbroken run of bit-for-bit zero frames. Reset to 0
    #: by the first frame carrying any non-zero sample, however quiet.
    zero_run_chunks: int
    #: Seconds the current all-zero run has lasted. ``None`` when the stream is
    #: not in one. This is the measurement that separates a stopped tap from a
    #: pause: a pause carries a noise floor and never starts a run.
    zero_run_seconds: float | None
    peak_dbfs: float
    last_dbfs: float
    #: True when the stream itself already told the operator about this state,
    #: so the heartbeat does not repeat it a second time.
    warning_emitted: bool
    #: True when the stream itself is a microphone capture (``MicStream``). The
    #: warning texts key on this, not on the ``self``/``prospect`` label: a label
    #: is only a name the caller picked, the stream is what knows what it is.
    is_microphone: bool = False

    def summary(self) -> str:
        """One-line human-readable verdict for the heartbeat."""

        parts = [
            f"state={self.state.value}",
            f"attach={self.attach_state.value}",
            f"target={self.target}",
            f"chunks={self.chunks}",
            f"signal_chunks={self.signal_chunks}",
        ]
        if self.seconds_since_signal is None:
            parts.append("last_signal=never")
        else:
            parts.append(f"last_signal={self.seconds_since_signal:.0f}s ago")
        if self.zero_run_seconds is not None:
            parts.append(f"zero_run={self.zero_run_seconds:.0f}s/{self.zero_run_chunks}frames")
        parts.append(f"peak={self.peak_dbfs:.1f}dBFS")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


class TapHealthTracker:
    """Per-stream attach state plus a running digital-silence measurement.

    Deliberately lock-free on the hot path. Every stream in this package has a
    single producer -- one PortAudio callback thread or one subprocess reader
    thread -- calling :meth:`observe`, and the reader (the heartbeat task) only
    ever wants a diagnostic snapshot, so a torn read across two counters is
    harmless. MicStream's callback runs on PortAudio's realtime thread, where
    taking a lock is exactly what you must not do.
    """

    def __init__(
        self,
        *,
        signal_floor_rms: float | None = None,
        signal_lost_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        is_microphone: bool = False,
    ) -> None:
        self._is_microphone = is_microphone
        self._floor = signal_floor_rms if signal_floor_rms is not None else resolve_signal_floor_rms()
        self._signal_lost_seconds = (
            signal_lost_seconds if signal_lost_seconds is not None else resolve_signal_lost_seconds()
        )
        self._clock = clock
        self._attach_state = TapAttachState.NOT_STARTED
        self._target = "unknown"
        self._detail = ""
        self._attached_at: float | None = None
        self._chunks = 0
        self._signal_chunks = 0
        self._last_signal_at: float | None = None
        self._zero_run_chunks = 0
        self._zero_run_started_at: float | None = None
        self._peak_rms = 0.0
        self._last_rms = 0.0
        self._warning_emitted = False

    @property
    def signal_floor_rms(self) -> float:
        return self._floor

    @property
    def signal_lost_seconds(self) -> float:
        return self._signal_lost_seconds

    @property
    def chunks(self) -> int:
        return self._chunks

    @property
    def signal_chunks(self) -> int:
        return self._signal_chunks

    @property
    def attach_state(self) -> TapAttachState:
        return self._attach_state

    def mark_attached(self, target: str) -> None:
        """The tap is open: subprocess spawned, or the input stream started."""

        self._attach_state = TapAttachState.ATTACHED
        self._target = target
        self._detail = ""
        self._attached_at = self._clock()
        self._warning_emitted = False

    def mark_attach_failed(self, detail: str, *, already_warned: bool = True) -> None:
        """The tap never opened. ``detail`` is the operator-facing reason.

        Every current caller either emits its own actionable ``audio_warning``
        or raises, so the default says "the operator has already been told".
        """

        self._attach_state = TapAttachState.ATTACH_FAILED
        self._detail = detail
        self._attached_at = None
        self._warning_emitted = already_warned

    def mark_detached(self, detail: str, *, already_warned: bool = False) -> None:
        """The tap was open and is now gone (subprocess exit, stream closed)."""

        self._attach_state = TapAttachState.DETACHED
        self._detail = detail
        self._warning_emitted = already_warned

    def observe(self, frame: np.ndarray | None) -> bool:
        """Account for one delivered frame. Returns True on the first audible one.

        The return value is the "first event" seam: a stream logs a single INFO
        line the first time its tap actually carries signal, so the transition
        from silence to audio is visible without raising the log level.

        Two silences are measured here, not one. "Below the signal floor" is a
        quiet room and keeps the stream healthy. "Every sample bit-for-bit zero"
        is a source that stopped delivering, and only that one extends the
        all-zero run behind ``ATTACHED_SIGNAL_LOST``. A frame with no samples at
        all (``None``, or empty) proves neither, so it leaves the run untouched
        rather than silently ending or extending it.
        """

        self._chunks += 1
        if frame is None:
            self._last_rms = 0.0
            return False
        arr = np.asarray(frame, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            self._last_rms = 0.0
            return False
        if arr.any():
            self._zero_run_chunks = 0
            self._zero_run_started_at = None
        elif self._zero_run_started_at is None:
            self._zero_run_chunks = 1
            self._zero_run_started_at = self._clock()
        else:
            self._zero_run_chunks += 1
        rms = float(np.sqrt(np.mean(np.square(arr.astype(np.float64)))))
        self._last_rms = rms
        if rms > self._peak_rms:
            self._peak_rms = rms
        if rms <= self._floor:
            return False
        first = self._signal_chunks == 0
        self._signal_chunks += 1
        self._last_signal_at = self._clock()
        return first

    def snapshot(self) -> TapHealth:
        now = self._clock()
        attach_state = self._attach_state
        chunks = self._chunks
        signal_chunks = self._signal_chunks
        attached_at = self._attached_at
        last_signal_at = self._last_signal_at
        zero_run_started_at = self._zero_run_started_at

        seconds_since_signal = None if last_signal_at is None else max(0.0, now - last_signal_at)
        # Measured against the clock, not against frame arrivals: a tap that
        # first writes zeros and then stops delivering entirely must keep
        # accumulating, or the run would freeze at the last frame it sent.
        zero_run_seconds = None if zero_run_started_at is None else max(0.0, now - zero_run_started_at)

        if attach_state in (TapAttachState.NOT_STARTED, TapAttachState.ATTACH_FAILED, TapAttachState.DETACHED):
            state = TapSignalState.NO_TAP
        elif chunks == 0:
            state = TapSignalState.ATTACHED_NO_FRAMES
        elif signal_chunks == 0:
            state = TapSignalState.ATTACHED_SILENT
        elif (
            zero_run_seconds is not None
            and zero_run_seconds >= self._signal_lost_seconds
            and seconds_since_signal is not None
            and seconds_since_signal >= self._signal_lost_seconds
        ):
            state = TapSignalState.ATTACHED_SIGNAL_LOST
        else:
            state = TapSignalState.ATTACHED_SIGNAL

        attached_seconds = 0.0 if attached_at is None else max(0.0, now - attached_at)
        return TapHealth(
            state=state,
            attach_state=attach_state,
            target=self._target,
            detail=self._detail,
            chunks=chunks,
            signal_chunks=signal_chunks,
            attached_seconds=attached_seconds,
            is_microphone=self._is_microphone,
            seconds_since_signal=seconds_since_signal,
            seconds_without_frames=attached_seconds if chunks == 0 and attached_at is not None else None,
            zero_run_chunks=self._zero_run_chunks,
            zero_run_seconds=zero_run_seconds,
            peak_dbfs=_rms_dbfs(self._peak_rms),
            last_dbfs=_rms_dbfs(self._last_rms),
            warning_emitted=self._warning_emitted,
        )


def _no_frames_message(label: str, health: TapHealth) -> str:
    if health.is_microphone:
        return (
            f"De {label}-microfoon is wel open ({health.target}) maar levert al "
            f"{health.attached_seconds:.0f} seconden geen enkel audioframe. Dat is geen stilte: "
            "een open microfoon blijft buffers doorgeven, ook als er niets gezegd wordt. Stop het "
            "gesprek en start het opnieuw; blijft dit, controleer dan de microfoontoestemming "
            "(Systeeminstellingen > Privacy en beveiliging > Microfoon)."
        )
    return (
        f"De {label}-tap is wel open ({health.target}) maar levert al "
        f"{health.attached_seconds:.0f} seconden geen enkel audioframe. Dat is geen stilte: "
        "een open tap blijft buffers doorgeven, ook als er niets speelt. Stop het gesprek en "
        "start het opnieuw; blijft dit, controleer dan de macOS-toestemming voor Core Audio "
        "process-taps (Systeeminstellingen > Privacy en beveiliging)."
    )


def _silent_message(label: str, health: TapHealth) -> str:
    if health.is_microphone:
        return (
            f"De {label}-microfoon is open ({health.target}) en levert audio, maar er zit sinds het "
            f"begin van het gesprek geen enkel signaal in ({health.chunks} frames, allemaal digitale "
            f"stilte, piek {health.peak_dbfs:.0f} dBFS). Drie oorzaken zijn realistisch: de "
            "microfoon staat gemute (hardware of in de app), er is een verkeerd invoerapparaat "
            f"gekozen ({health.target}; bijvoorbeeld BlackHole of een ander virtueel apparaat in "
            "plaats van je headset), of de app heeft geen microfoontoestemming (Systeeminstellingen "
            "> Privacy en beveiliging > Microfoon)."
        )
    return (
        f"De {label}-tap is open ({health.target}) en levert audio, maar er zit sinds het begin "
        f"van het gesprek geen enkel signaal in ({health.chunks} frames, allemaal digitale stilte, "
        f"piek {health.peak_dbfs:.0f} dBFS). Twee oorzaken zijn realistisch: de gespreksaudio speelt "
        "op een ander apparaat (telefoon, tweede laptop) in plaats van op deze Mac, of de output "
        "van deze Mac staat gemute. De tap zelf is in orde."
    )


def _signal_lost_message(label: str, health: TapHealth, peer_label: str | None) -> str:
    seconds = health.zero_run_seconds or 0.0
    kind = "microfoon" if health.is_microphone else "tap"
    peer = ""
    if peer_label:
        peer = (
            f" Het {peer_label}-kanaal draagt in diezelfde periode wel signaal. "
            f"Dit is dus geen stilte in het gesprek maar een defect in deze {kind}."
        )
    return (
        f"De {label}-{kind} ({health.target}) droeg eerder in dit gesprek audio en levert nu al "
        f"{seconds:.0f} seconden uitsluitend exacte nullen ({health.zero_run_chunks} frames). "
        f"Een levende {kind} van een stille bron geeft altijd een ruisvloer. Exact nul betekent dat de "
        f"bron niets meer aanlevert.{peer} Alles wat deze kant vanaf nu zegt gaat verloren. "
        "Stop het gesprek en start het opnieuw."
    )


def reattach_marker(label: str, health: TapHealth, *, attempt: int, max_attempts: int, at_ms: int) -> dict[str, Any]:
    """Build the transcript line that records a forced tap reattach attempt.

    Shares ``TRANSCRIPT_MARKER_TYPE`` with :func:`signal_lost_marker` so a
    reader of the saved transcript sees not just that audio went missing but
    that the system tried to recover it, and how many attempts remain.
    """

    return {
        "type": TRANSCRIPT_MARKER_TYPE,
        "marker": REATTACH_MARKER,
        "stream": label,
        "speaker": "system",
        "text": (
            f"[de {label}-tap ({health.target}) leverde alleen digitale stilte en wordt opnieuw "
            f"aangehaakt (poging {attempt}/{max_attempts}).]"
        ),
        "start_ms": at_ms,
        "end_ms": at_ms,
    }


def _detached_message(label: str, health: TapHealth) -> str:
    detail = f" ({health.detail})" if health.detail else ""
    return (
        f"De {label}-tap is tijdens het gesprek weggevallen{detail} na {health.chunks} frames. "
        "Er komt geen audio meer binnen. Stop het gesprek en start het opnieuw."
    )


def evaluate_tap_health(
    label: str,
    health: TapHealth,
    *,
    no_frames_warn_seconds: float,
    silent_warn_seconds: float,
    peer_label: str | None = None,
) -> str | None:
    """Return an operator-facing warning for ``health``, or None when quiet is fine.

    The rules that keep this from crying wolf on an ordinary pause in a sales
    call:

    * ``ATTACHED_SIGNAL`` never warns, no matter how long the current silence
      lasts. Once a stream has proved it can carry audio, a quiet stretch is the
      conversation, not a fault.
    * ``ATTACHED_SILENT`` only warns while ``signal_chunks == 0`` -- the tap has
      never carried a single audible frame this call -- and only after
      ``silent_warn_seconds``. A real call always breaks that condition within
      seconds of starting.
    * ``ATTACHED_NO_FRAMES`` warns quickly, because an open tap with a literally
      empty pipe is unambiguously broken.
    * ``ATTACHED_SIGNAL_LOST`` warns, and it is the one state above that a quiet
      stretch cannot reach. The tracker only enters it on an unbroken run of
      bit-for-bit zero frames, which a live capture of a quiet room never
      produces: its noise floor ends the run on the first frame.

    ``peer_label`` names another stream that is carrying signal right now. That
    is the second, independent argument that this is a defect and not the
    conversation, so the message says so when it is available.
    """

    if health.warning_emitted:
        # The stream already told the operator, with a message closer to the
        # actual cause than anything this function could reconstruct.
        return None
    if health.state is TapSignalState.NO_TAP:
        if health.attach_state is TapAttachState.DETACHED:
            return _detached_message(label, health)
        # ATTACH_FAILED without warning_emitted, and NOT_STARTED, are not facts
        # this heartbeat can improve on.
        return None
    if health.state is TapSignalState.ATTACHED_NO_FRAMES:
        if health.attached_seconds >= no_frames_warn_seconds:
            return _no_frames_message(label, health)
        return None
    if health.state is TapSignalState.ATTACHED_SILENT:
        if health.attached_seconds >= silent_warn_seconds:
            return _silent_message(label, health)
        return None
    if health.state is TapSignalState.ATTACHED_SIGNAL_LOST:
        return _signal_lost_message(label, health, peer_label)
    return None


def describe_silent_stream(label: str, device_label: str, health: TapHealth | None) -> str:
    """Explain a stream that delivered nothing during the warmup liveness check.

    The old message always read "check your microphone / audio routing", which
    is the right advice for exactly one of the three ways a stream can be
    silent. With the attach state available it can now name which one.
    """

    if health is None:
        return f"Geen audio van {label}-stream (device={device_label}). Controleer je microfoon/audio-routing."
    if health.state is TapSignalState.NO_TAP:
        detail = f": {health.detail}" if health.detail else ""
        return (
            f"De {label}-tap is niet geopend ({health.attach_state.value}{detail}). Er is geen bron "
            "om naar te luisteren, dus dit is geen stilte. Controleer de audio-toestemming en start "
            "het gesprek opnieuw."
        )
    if health.state is TapSignalState.ATTACHED_NO_FRAMES:
        return (
            f"De {label}-tap is open ({health.target}) maar leverde nog geen enkel frame. "
            "Blijft dat zo, dan hangt de tap; stop het gesprek en start het opnieuw."
        )
    return f"Geen audio van {label}-stream (device={device_label}). Controleer je microfoon/audio-routing."


#: Event type for the transcript-channel marker. Deliberately not ``transcript``:
#: every existing consumer keys on that type, and a marker fed to the detector
#: would be classified, summarised and scored as something a person said.
TRANSCRIPT_MARKER_TYPE = "transcript_marker"
SIGNAL_LOST_MARKER = "audio_signal_lost"


class DashboardTapState(StrEnum):
    """The three tap states the dashboard shows per side.

    The five :class:`TapSignalState` values answer questions an operator
    debugging a dead tap needs (frames vs. signal vs. a lost run); the
    dashboard only has room, per side, for "no tap", "tap but nothing coming
    in", and "tap carrying signal". :func:`dashboard_tap_state` is the one
    place that folds five into three -- nothing else may re-derive it.
    """

    NO_TAP = "no_tap"
    ATTACHED_NO_SIGNAL = "attached_no_signal"
    ATTACHED_SIGNAL = "attached_signal"


_DASHBOARD_STATE_BY_SIGNAL_STATE: dict[TapSignalState, DashboardTapState] = {
    TapSignalState.NO_TAP: DashboardTapState.NO_TAP,
    TapSignalState.ATTACHED_NO_FRAMES: DashboardTapState.ATTACHED_NO_SIGNAL,
    TapSignalState.ATTACHED_SILENT: DashboardTapState.ATTACHED_NO_SIGNAL,
    TapSignalState.ATTACHED_SIGNAL: DashboardTapState.ATTACHED_SIGNAL,
    # A lost run means frames have stopped carrying anything but zeros: from
    # the dashboard's three-state view that is indistinguishable from "tap
    # attached, nothing coming in", not from a healthy signal.
    TapSignalState.ATTACHED_SIGNAL_LOST: DashboardTapState.ATTACHED_NO_SIGNAL,
}


def dashboard_tap_state(state: TapSignalState) -> DashboardTapState:
    """Map one of the five backend tap states onto the three the dashboard shows."""

    return _DASHBOARD_STATE_BY_SIGNAL_STATE[state]


#: Event type for the per-stream dashboard status published every heartbeat.
TAP_STATUS_MESSAGE_TYPE = "tap_status"


def tap_status_message(label: str, health: TapHealth) -> dict[str, Any]:
    """Build the dashboard-facing per-stream tap status payload.

    Unlike ``audio_warning`` (fired once per stream, only on a fault), this is
    published on every heartbeat tick regardless of state, so the dashboard's
    per-side indicator reflects the current state for the whole call -- including
    the recovery from a fault back to ``attached_signal``, which a one-shot
    warning can never report.
    """

    return {
        "type": TAP_STATUS_MESSAGE_TYPE,
        "stream": label,
        "tap_state": health.state.value,
        "dashboard_state": dashboard_tap_state(health.state).value,
    }


def signal_lost_marker(label: str, health: TapHealth, *, peer_label: str | None, at_ms: int) -> dict[str, Any]:
    """Build the transcript line that says this channel stopped being recorded.

    Without it the transcript is not merely incomplete, it is misleading: the
    2026-09-10 call reads as if the prospect stopped talking after minute nine,
    and the first person to read it back drew exactly that conclusion. ``at_ms``
    is the moment the signal went, not the moment the monitor noticed.
    """

    peer = f" Het {peer_label}-kanaal loopt wel door." if peer_label else ""
    return {
        "type": TRANSCRIPT_MARKER_TYPE,
        "marker": SIGNAL_LOST_MARKER,
        "stream": label,
        "speaker": "system",
        "text": (
            f"[audio ontbreekt vanaf hier: de {label}-tap levert geen signaal meer."
            f"{peer} Wat deze kant hierna zegt staat niet in dit transcript.]"
        ),
        "start_ms": at_ms,
        "end_ms": at_ms,
    }


def stream_tap_health(stream: Any) -> TapHealth | None:
    """Read a stream's tap health, or None for a stream that does not report it."""

    getter = getattr(stream, "tap_health", None)
    if getter is None:
        return None
    try:
        health = getter()
    except Exception:  # pragma: no cover - defensive around third-party streams
        logger.debug("tap_health() raised on stream %r", stream, exc_info=True)
        return None
    return health if isinstance(health, TapHealth) else None


async def monitor_tap_health(
    streams_and_labels: list[tuple[Any, str]],
    broadcast_warning: Callable[[dict[str, Any]], Awaitable[None]],
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    no_frames_warn_seconds: float = DEFAULT_NO_FRAMES_WARN_SECONDS,
    silent_warn_seconds: float = DEFAULT_SILENT_WARN_SECONDS,
    already_reported: set[str] | None = None,
    max_beats: int | None = None,
    broadcast_marker: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    broadcast_status: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Log a bounded periodic INFO heartbeat per stream and warn on real faults.

    Mirrors the detector's consume-loop heartbeat (``_HEARTBEAT_CHUNK_INTERVAL``):
    the interesting per-frame detail stays at DEBUG, and one INFO line per
    interval carries the counters, so a dead tap and a healthy one no longer read
    the same at the level anybody actually runs at. Counted in wall-clock rather
    than chunks precisely because "no chunks at all" is one of the states this
    has to report.

    Each warning fires at most once per stream per session. ``already_reported``
    carries the labels the two-second warmup check (``check_streams_liveness``)
    has already warned about, so the operator is told a given stream is dead
    once, not twice. This never raises: a failing broadcast is logged and the
    loop continues.

    Every stream is snapshotted before any of them is judged, so a stream that
    lost its signal can be told that another stream is still carrying audio.
    That cross-channel fact is the second, independent argument that a run of
    zeros is a defect rather than the conversation going quiet: on 2026-09-10
    the mic held 100% signal across the 23 minutes the system tap delivered
    nothing.

    ``broadcast_marker`` publishes one transcript-channel marker at the moment a
    stream loses signal, so the transcript afterwards says the audio stopped
    instead of implying the speaker did.

    ``broadcast_status`` publishes the dashboard-facing per-stream tap state
    (see :func:`tap_status_message`) on every beat, independent of the
    once-per-stream warning gating below -- a stream that recovers has to be
    able to say so.

    Reattach (added 2026-09-27). ``ATTACHED_SIGNAL_LOST`` used to always fall
    straight to the one-shot warning. A tap in that state is still attached --
    its subprocess never died, it just stopped delivering anything but zeros,
    most likely because macOS moved the audio route out from under it. When a
    stream exposes a ``reattach()`` method (a clean stop/start cycle -- see
    ``AudioTeeStream.reattach``/``CallTapStream.reattach``) and a peer channel
    is currently carrying signal (the same cross-channel fact that sharpens
    the warning message), this calls it instead of warning immediately: up to
    ``DEFAULT_REATTACH_MAX_ATTEMPTS`` tries, spaced further apart each time
    (``_REATTACH_BACKOFF_BEATS``), before falling back to the ordinary
    warning. A reattach that restores signal needs no extra bookkeeping to
    "reset" the state -- the next snapshot already reads ``ATTACHED_SIGNAL``
    once real frames arrive again, the same way any other recovery does. No
    reattach is attempted when the peer is also silent (that is a pause or the
    end of the call, not a dropout) or when the stream does not support it.
    """

    reported: set[str] = set(already_reported or ())
    reattach_attempts: dict[str, int] = {}
    next_reattach_beat: dict[str, int] = {}
    stream_by_label = {label: stream for stream, label in streams_and_labels}
    started_at = clock()
    beats = 0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            pass

        beats += 1
        snapshots = [(label, stream_tap_health(stream)) for stream, label in streams_and_labels]
        carrying_signal = [
            label
            for label, health in snapshots
            if health is not None and health.state is TapSignalState.ATTACHED_SIGNAL
        ]
        for label, health in snapshots:
            if health is None:
                continue
            logger.info("Audio tap heartbeat [%s]: %s", label, health.summary())
            if broadcast_status is not None:
                try:
                    await broadcast_status(tap_status_message(label, health))
                except Exception:
                    logger.warning("Failed to broadcast tap_status for stream=%s", label, exc_info=True)
            if label in reported:
                continue
            peer_label = next((peer for peer in carrying_signal if peer != label), None)

            if health.state is TapSignalState.ATTACHED_SIGNAL_LOST and peer_label is not None:
                reattach_fn = getattr(stream_by_label.get(label), "reattach", None)
                if reattach_fn is not None and reattach_attempts.get(label, 0) < DEFAULT_REATTACH_MAX_ATTEMPTS:
                    if beats < next_reattach_beat.get(label, 0):
                        # Cooling down from the previous attempt: say nothing
                        # yet, this beat is not the one that decides.
                        continue
                    attempts = reattach_attempts.get(label, 0) + 1
                    reattach_attempts[label] = attempts
                    backoff = _REATTACH_BACKOFF_BEATS[min(attempts, len(_REATTACH_BACKOFF_BEATS)) - 1]
                    next_reattach_beat[label] = beats + backoff
                    logger.info(
                        "Audio tap [%s]: reattaching (attempt %s/%s) -- attached but delivering only "
                        "digital silence while %s carries signal.",
                        label,
                        attempts,
                        DEFAULT_REATTACH_MAX_ATTEMPTS,
                        peer_label,
                    )
                    try:
                        await asyncio.get_running_loop().run_in_executor(None, reattach_fn)
                    except Exception:
                        logger.warning("Tap reattach raised for stream=%s", label, exc_info=True)
                    if broadcast_marker is not None:
                        elapsed = clock() - started_at
                        marker = reattach_marker(
                            label,
                            health,
                            attempt=attempts,
                            max_attempts=DEFAULT_REATTACH_MAX_ATTEMPTS,
                            at_ms=max(0, int(elapsed * 1000)),
                        )
                        try:
                            await broadcast_marker(marker)
                        except Exception:
                            logger.warning(
                                "Failed to broadcast tap_health reattach marker for stream=%s",
                                label,
                                exc_info=True,
                            )
                    continue

            message = evaluate_tap_health(
                label,
                health,
                no_frames_warn_seconds=no_frames_warn_seconds,
                silent_warn_seconds=silent_warn_seconds,
                peer_label=peer_label,
            )
            if message is None:
                continue
            reported.add(label)
            logger.warning("Audio tap [%s]: %s", label, message)
            try:
                await broadcast_warning(
                    {
                        "type": "audio_warning",
                        "stream": label,
                        "tap_state": health.state.value,
                        "message": message,
                    }
                )
            except Exception:
                logger.warning("Failed to broadcast tap_health warning for stream=%s", label, exc_info=True)
            if broadcast_marker is None or health.state is not TapSignalState.ATTACHED_SIGNAL_LOST:
                continue
            # The transcript timeline starts where the bufferers do, which is the
            # same moment this monitor does. Rewind by the zero-run so the line
            # lands where the audio went, not where the heartbeat happened to
            # look.
            elapsed = clock() - started_at - (health.zero_run_seconds or 0.0)
            marker = signal_lost_marker(label, health, peer_label=peer_label, at_ms=max(0, int(elapsed * 1000)))
            try:
                await broadcast_marker(marker)
            except Exception:
                logger.warning("Failed to broadcast tap_health transcript marker for stream=%s", label, exc_info=True)

        if max_beats is not None and beats >= max_beats:
            return
