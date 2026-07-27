"""Runtime auto-arm monitor for ``FEATURE_AUTOSTART`` (video + telephony paths).

This is distinct from the launch-at-login wizard step
(``sales_copilot.wizard.detectors.detect_autostart_consent_arm`` as consumed by
``sales_copilot.wizard.cli``): that step controls whether the app itself starts
when macOS boots. This module watches for a supported video meeting app, or an
active macOS phone/FaceTime call (``avconferenced``), becoming active *while
the app is already running* and, for Pro-entitled installs, automatically arms
the copilot session.

"Arm" never means "start recording silently". The state machine only ever
reaches ``STATE_ARMED`` through an explicit ``confirm_consent()`` call -- the
per-call consent tick -- so a caller must wire that to a real user action
(dashboard button, WebSocket confirmation, etc.). Detection alone only reaches
``STATE_CONSENT_PENDING`` and invokes ``on_consent_required``, which is where a
caller surfaces the prompt.

Two independent trigger sources feed the same detect -> consent-arm ->
session-arm state machine, each with its own debounce streak so a flapping
video app can never interfere with a stable phone call (or vice versa):

- video: ``resolve_meeting_app_target`` (the same meeting-app process
  detection built for the audio-onboarding path).
- telephony: ``resolve_telephony_call_target`` (the same ``avconferenced``
  process resolution used by the telephony-tap guard).

Both sources share the same ``detect_autostart_consent_arm`` consent-arm gate
and the same ``FEATURE_AUTOSTART`` entitlement check -- there is only one
auto-arm capability, with two ways to trigger it. The copilot only *detects*
an in-progress call; macOS places the underlying ``tel:``/FaceTime call itself
via Continuity/iPhone-relay, this module never dials anything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sales_copilot.audio.capture import MEETING_APP_CANDIDATES, resolve_meeting_app_target
from sales_copilot.audio.telephony_guard import DEFAULT_TELEPHONY_PROCESSES, resolve_telephony_call_target
from sales_copilot.auth.feature_policy import FeaturePolicy, get_feature_policy
from sales_copilot.auth.license_format import FEATURE_AUTOSTART
from sales_copilot.core.config import env_int
from sales_copilot.wizard.detectors import detect_autostart_consent_arm
from sales_copilot.wizard.steps import StepResult

logger = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_CONSENT_PENDING = "consent_pending"
STATE_ARMED = "armed"

# Trigger-source labels for the internal ``_active_trigger`` field -- which
# source's own debounce streak governs a disarm decision while pending/armed.
TRIGGER_VIDEO = "video"
TRIGGER_TELEPHONY = "telephony"

DetectFn = Callable[[tuple[str, ...]], tuple[str | None, int | None, list[str]]]
ConsentArmCheckFn = Callable[[], StepResult]


@dataclass(frozen=True)
class AutostartMonitorConfig:
    """Debounce tuning for the runtime auto-arm monitor.

    Both counts are in consecutive polls, not seconds -- the poll interval is
    the caller's concern (``run_autostart_monitor_loop``). Requiring more than
    one consecutive poll in either direction is what prevents a flapping or
    transient process presence from repeatedly arming/disarming the session.
    The same two counts debounce both trigger sources (video and telephony) --
    one auto-arm capability, tuned once.
    """

    debounce_arm_polls: int = 2
    debounce_disarm_polls: int = 2
    candidates: tuple[str, ...] = MEETING_APP_CANDIDATES
    telephony_candidates: tuple[str, ...] = DEFAULT_TELEPHONY_PROCESSES

    @classmethod
    def from_env(cls) -> AutostartMonitorConfig:
        return cls(
            debounce_arm_polls=max(1, env_int("AUTOSTART_DEBOUNCE_ARM_POLLS", 2)),
            debounce_disarm_polls=max(1, env_int("AUTOSTART_DEBOUNCE_DISARM_POLLS", 2)),
        )


@dataclass
class RuntimeAutostartMonitor:
    """Debounced detect -> consent-arm -> session-arm state machine.

    ``on_consent_required`` fires once per stable detection, when the monitor
    transitions into ``STATE_CONSENT_PENDING`` -- this is where a caller
    surfaces the consent-tick prompt (e.g. a WebSocket broadcast to the
    dashboard). ``on_session_arm`` fires from ``confirm_consent()``, once the
    user has explicitly ticked consent for this call -- this is where a caller
    actually starts the call (equivalent to a manual Start-Call click, so the
    same entitlement/consent gates in the runtime start-call path still apply).

    Two independent ``DetectFn`` sources (video, telephony) are polled every
    tick, each with its own present/absent streak so one source's flapping
    never resets the other's progress. Only one source can drive the state
    machine out of ``STATE_IDLE`` at a time; whichever source reaches its
    debounce threshold first wins for that detection cycle (video is checked
    first when both cross the threshold on the same tick -- an edge case with
    no behavioural preference either way, since only one real call is ever
    actually in progress). Once pending/armed, ``_active_trigger`` records
    which source is driving the state, so its own absence debounce -- not the
    other source's -- governs the reset.
    """

    config: AutostartMonitorConfig = field(default_factory=AutostartMonitorConfig)
    feature_policy: FeaturePolicy = field(default_factory=get_feature_policy)
    detect_fn: DetectFn = resolve_meeting_app_target
    telephony_detect_fn: DetectFn = resolve_telephony_call_target
    consent_arm_check: ConsentArmCheckFn = detect_autostart_consent_arm
    on_consent_required: Callable[[str], None] | None = None
    on_session_arm: Callable[[str], None] | None = None

    state: str = field(default=STATE_IDLE, init=False)
    _present_streak: int = field(default=0, init=False)
    _absent_streak: int = field(default=0, init=False)
    _telephony_present_streak: int = field(default=0, init=False)
    _telephony_absent_streak: int = field(default=0, init=False)
    _detected_process: str | None = field(default=None, init=False)
    _active_trigger: str | None = field(default=None, init=False)

    def tick(self) -> str:
        """Run one detection poll (both trigger sources) and advance the state machine.

        Ambiguous detection (more than one candidate process running at once)
        is treated the same as "no call detected" for each source -- both
        ``resolve_meeting_app_target`` and ``resolve_telephony_call_target``
        only return a process name when exactly one candidate is running, so
        auto-arm never has to guess which app/process is the real call.
        """

        video_process, _pid, _running = self.detect_fn(self.config.candidates)
        self._update_streak(video_process is not None, telephony=False)

        telephony_process, _tpid, _trunning = self.telephony_detect_fn(self.config.telephony_candidates)
        self._update_streak(telephony_process is not None, telephony=True)

        if self.state == STATE_IDLE:
            self._advance_from_idle(video_process, telephony_process)
        elif self.state == STATE_CONSENT_PENDING:
            self._reset_if_gone_while_pending()
        elif self.state == STATE_ARMED:
            self._reset_if_gone_while_armed()

        return self.state

    def _update_streak(self, present: bool, *, telephony: bool) -> None:
        if telephony:
            if present:
                self._telephony_present_streak += 1
                self._telephony_absent_streak = 0
            else:
                self._telephony_absent_streak += 1
                self._telephony_present_streak = 0
        else:
            if present:
                self._present_streak += 1
                self._absent_streak = 0
            else:
                self._absent_streak += 1
                self._present_streak = 0

    def _advance_from_idle(self, video_process: str | None, telephony_process: str | None) -> None:
        if self._present_streak >= self.config.debounce_arm_polls and self._try_arm(
            TRIGGER_VIDEO, video_process, "video call"
        ):
            return
        if self._telephony_present_streak >= self.config.debounce_arm_polls:
            self._try_arm(TRIGGER_TELEPHONY, telephony_process, "phone call")

    def _try_arm(self, trigger: str, process_name: str | None, label: str) -> bool:
        if process_name is None:
            return False

        if not self.feature_policy.allows(FEATURE_AUTOSTART):
            # Free tier: detection is a no-op. The manual Start-Call button
            # stays the only path to start a session.
            return False

        gate = self.consent_arm_check()
        if not gate.ok:
            logger.info(
                "Runtime auto-arm: %s detected (%s) but the consent-arm gate "
                "failed (%s); staying manual.",
                label,
                process_name,
                gate.message,
            )
            return False

        self._detected_process = process_name
        self._active_trigger = trigger
        self.state = STATE_CONSENT_PENDING
        if self.on_consent_required is not None:
            self.on_consent_required(process_name)
        return True

    def _active_absent_streak(self) -> int:
        if self._active_trigger == TRIGGER_TELEPHONY:
            return self._telephony_absent_streak
        return self._absent_streak

    def _reset_if_gone_while_pending(self) -> None:
        if self._active_absent_streak() >= self.config.debounce_disarm_polls:
            logger.info("Runtime auto-arm: trigger source gone before consent tick; resetting.")
            self._reset()

    def _reset_if_gone_while_armed(self) -> None:
        if self._active_absent_streak() >= self.config.debounce_disarm_polls:
            self._reset()

    def confirm_consent(self) -> bool:
        """Record the explicit per-call consent tick and trigger the session-arm.

        Returns ``True`` when a pending prompt was armed, ``False`` when there
        was nothing pending (already armed, idle, or the prompt was declined /
        expired before the tick arrived).
        """

        if self.state != STATE_CONSENT_PENDING:
            return False
        process_name = self._detected_process
        self.state = STATE_ARMED
        if self.on_session_arm is not None and process_name is not None:
            self.on_session_arm(process_name)
        return True

    def decline_consent(self) -> None:
        """User declined the auto-arm prompt for this detected call."""

        if self.state == STATE_CONSENT_PENDING:
            self._reset()

    def notify_call_ended(self) -> None:
        """Reset immediately once the armed session's call has ended.

        Called by the runtime when a call ends (regardless of whether it was
        started via auto-arm or the manual button), so the monitor does not
        stay stuck in ``STATE_ARMED`` waiting for the meeting app to close.
        """

        if self.state == STATE_ARMED:
            self._reset()

    def _reset(self) -> None:
        self.state = STATE_IDLE
        self._present_streak = 0
        self._absent_streak = 0
        self._telephony_present_streak = 0
        self._telephony_absent_streak = 0
        self._detected_process = None
        self._active_trigger = None


_active_monitor: RuntimeAutostartMonitor | None = None


def set_active_monitor(monitor: RuntimeAutostartMonitor | None) -> None:
    """Register the monitor instance driving the running app (or ``None``)."""

    global _active_monitor
    _active_monitor = monitor


def get_active_monitor() -> RuntimeAutostartMonitor | None:
    """Return the monitor instance driving the running app, if any.

    ``None`` outside a running app process (e.g. in most tests), or when the
    current install has no runtime auto-arm loop started (older setups,
    disabled via config).
    """

    return _active_monitor


async def run_autostart_monitor_loop(
    monitor: RuntimeAutostartMonitor,
    stop_event: asyncio.Event,
    *,
    poll_interval_s: float = 2.0,
    is_call_active: Callable[[], bool] = lambda: False,
) -> None:
    """Poll ``monitor.tick()`` on an interval until ``stop_event`` is set.

    Skips ticking while a call is already active (manual or auto-armed) so the
    monitor never fights an in-progress session; it resumes polling for the
    next meeting once the call ends.
    """

    while not stop_event.is_set():
        try:
            if not is_call_active():
                monitor.tick()
        except Exception:
            logger.exception("Runtime autostart monitor tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_s)
        except TimeoutError:
            pass
