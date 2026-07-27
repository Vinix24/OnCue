"""Tests for the runtime auto-arm monitor (video + telephony paths).

Covers the full detect -> consent-arm -> session-arm pipeline with mocked
detection and consent-arm gates -- no real hardware, no real devices, no real
process/filesystem inspection. Every ``RuntimeAutostartMonitor`` constructed
here that calls ``.tick()`` explicitly overrides *both* ``detect_fn`` and
``telephony_detect_fn`` (via ``_absent_detector`` where a source is not under
test), since the class defaults to the real, subprocess-backed detectors for
production parity -- letting either default through would shell out to a real
``pgrep`` call from the test suite.
"""

from __future__ import annotations

import asyncio

from sales_copilot.core.autostart_monitor import (
    STATE_ARMED,
    STATE_CONSENT_PENDING,
    STATE_IDLE,
    AutostartMonitorConfig,
    RuntimeAutostartMonitor,
    run_autostart_monitor_loop,
)
from sales_copilot.wizard.steps import StepResult


class ScriptedDetector:
    """Fake ``resolve_meeting_app_target``/``resolve_telephony_call_target``-shaped
    detector for tests.

    Plays back a scripted sequence of present/absent booleans, one per call;
    holds on the last entry once the script is exhausted. Shape-compatible with
    both trigger sources, so the same class covers the video and telephony
    detect functions.
    """

    def __init__(self, script: list[bool], process_name: str = "Google Chrome") -> None:
        self._script = list(script)
        self._index = 0
        self.process_name = process_name
        self.calls = 0

    def __call__(self, candidates: tuple[str, ...]) -> tuple[str | None, int | None, list[str]]:
        self.calls += 1
        if self._index < len(self._script):
            present = self._script[self._index]
            self._index += 1
        else:
            present = self._script[-1] if self._script else False
        if present:
            return self.process_name, 4242, [self.process_name]
        return None, None, []


def _absent_detector(candidates: tuple[str, ...]) -> tuple[None, None, list[str]]:
    """A ``DetectFn`` that never reports a process present -- neutralizes a trigger source."""

    return None, None, []


def _ok_gate() -> StepResult:
    return StepResult(ok=True, message="consent-arm ok")


def _fail_gate() -> StepResult:
    return StepResult(ok=False, message="consent tracking disabled")


def _ambiguous_detector(candidates: tuple[str, ...]) -> tuple[None, None, list[str]]:
    return None, None, ["Google Chrome", "zoom.us"]


# ---------------------------------------------------------------------------
# detect -> consent-arm gating (video trigger)
# ---------------------------------------------------------------------------


def test_free_tier_never_arms(free_feature_policy) -> None:
    detector = ScriptedDetector([True, True, True])
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=free_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(3):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_consent_arm_gate_blocks_pro_tier(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True, True])
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_fail_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(3):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_pro_tier_arms_after_debounce(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True])
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    assert monitor.tick() == STATE_IDLE
    assert consent_calls == []
    assert monitor.tick() == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome"]


def test_flapping_presence_never_arms(pro_feature_policy) -> None:
    """A flapping/transient process presence must never accumulate an arm-streak."""
    detector = ScriptedDetector([True, False, True, False, True, False])
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(6):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_ambiguous_multiple_apps_never_arms(pro_feature_policy) -> None:
    """More than one candidate running at once must not auto-arm (can't pick which is the call)."""
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1),
        feature_policy=pro_feature_policy,
        detect_fn=_ambiguous_detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(3):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


# ---------------------------------------------------------------------------
# consent-arm pending: debounced disappearance, confirm, decline (video trigger)
# ---------------------------------------------------------------------------


def test_consent_pending_resets_when_app_closes(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True, False, False])
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2, debounce_disarm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    monitor.tick()  # absent, 1st
    assert monitor.state == STATE_CONSENT_PENDING
    monitor.tick()  # absent, 2nd -> debounced reset
    assert monitor.state == STATE_IDLE
    assert consent_calls == ["Google Chrome"]


def test_confirm_consent_arms_and_calls_session_arm_hook(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True])
    session_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_session_arm=session_calls.append,
    )
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    armed = monitor.confirm_consent()

    assert armed is True
    assert monitor.state == STATE_ARMED
    assert session_calls == ["Google Chrome"]


def test_confirm_consent_returns_false_when_nothing_pending(pro_feature_policy) -> None:
    session_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(feature_policy=pro_feature_policy, on_session_arm=session_calls.append)

    assert monitor.confirm_consent() is False
    assert monitor.state == STATE_IDLE
    assert session_calls == []


def test_decline_consent_resets_and_requires_fresh_debounce(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True, True, True])
    consent_calls: list[str] = []
    session_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
        on_session_arm=session_calls.append,
    )
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    monitor.decline_consent()

    assert monitor.state == STATE_IDLE
    assert session_calls == []
    # Re-detecting after a decline requires the full debounce again -- not an
    # immediate re-arm on the very next poll.
    assert monitor.tick() == STATE_IDLE
    assert monitor.tick() == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome", "Google Chrome"]


def test_decline_consent_is_noop_outside_pending(pro_feature_policy) -> None:
    monitor = RuntimeAutostartMonitor(feature_policy=pro_feature_policy)

    monitor.decline_consent()

    assert monitor.state == STATE_IDLE


# ---------------------------------------------------------------------------
# armed state: debounced disappearance, explicit call-ended reset (video trigger)
# ---------------------------------------------------------------------------


def test_armed_resets_when_app_closes(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True, False, False])
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2, debounce_disarm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
    )
    monitor.tick()
    monitor.tick()
    monitor.confirm_consent()
    assert monitor.state == STATE_ARMED

    monitor.tick()  # absent, 1st
    assert monitor.state == STATE_ARMED
    monitor.tick()  # absent, 2nd -> debounced reset
    assert monitor.state == STATE_IDLE


def test_notify_call_ended_resets_immediately_regardless_of_presence(pro_feature_policy) -> None:
    detector = ScriptedDetector([True, True, True, True, True])
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
    )
    monitor.tick()
    monitor.tick()
    monitor.confirm_consent()
    assert monitor.state == STATE_ARMED

    monitor.notify_call_ended()

    assert monitor.state == STATE_IDLE


def test_notify_call_ended_is_noop_outside_armed(pro_feature_policy) -> None:
    monitor = RuntimeAutostartMonitor(feature_policy=pro_feature_policy)

    monitor.notify_call_ended()

    assert monitor.state == STATE_IDLE


# ---------------------------------------------------------------------------
# detect -> consent-arm -> session-arm (telephony trigger)
# ---------------------------------------------------------------------------


def test_telephony_free_tier_never_arms(free_feature_policy) -> None:
    telephony = ScriptedDetector([True, True, True], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=free_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(3):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_telephony_consent_arm_gate_blocks_pro_tier(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True, True], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_fail_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(3):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_telephony_pro_tier_arms_after_debounce(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    assert monitor.tick() == STATE_IDLE
    assert consent_calls == []
    assert monitor.tick() == STATE_CONSENT_PENDING
    assert consent_calls == ["avconferenced"]


def test_telephony_flapping_presence_never_arms(pro_feature_policy) -> None:
    """A brief avconferenced blip must never accumulate an arm-streak."""
    telephony = ScriptedDetector([True, False, True, False, True, False], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    for _ in range(6):
        monitor.tick()

    assert monitor.state == STATE_IDLE
    assert consent_calls == []


def test_telephony_consent_pending_resets_when_call_ends(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True, False, False], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2, debounce_disarm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    monitor.tick()  # absent, 1st
    assert monitor.state == STATE_CONSENT_PENDING
    monitor.tick()  # absent, 2nd -> debounced reset
    assert monitor.state == STATE_IDLE
    assert consent_calls == ["avconferenced"]


def test_telephony_confirm_consent_arms_and_calls_session_arm_hook(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True], process_name="avconferenced")
    session_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_session_arm=session_calls.append,
    )
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    armed = monitor.confirm_consent()

    assert armed is True
    assert monitor.state == STATE_ARMED
    assert session_calls == ["avconferenced"]


def test_telephony_armed_resets_when_call_ends(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True, False, False], process_name="avconferenced")
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2, debounce_disarm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
    )
    monitor.tick()
    monitor.tick()
    monitor.confirm_consent()
    assert monitor.state == STATE_ARMED

    monitor.tick()  # absent, 1st
    assert monitor.state == STATE_ARMED
    monitor.tick()  # absent, 2nd -> debounced reset
    assert monitor.state == STATE_IDLE


def test_telephony_notify_call_ended_resets_immediately(pro_feature_policy) -> None:
    telephony = ScriptedDetector([True, True, True, True, True], process_name="avconferenced")
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
    )
    monitor.tick()
    monitor.tick()
    monitor.confirm_consent()
    assert monitor.state == STATE_ARMED

    monitor.notify_call_ended()

    assert monitor.state == STATE_IDLE


# ---------------------------------------------------------------------------
# cross-source independence: video and telephony must not interfere
# ---------------------------------------------------------------------------


def test_active_trigger_governs_disarm_not_the_other_source(pro_feature_policy) -> None:
    """Whichever source armed the session, only *its* absence should disarm it.

    Video and telephony both cross the debounce threshold on the same tick;
    video is checked first in ``_advance_from_idle`` so it wins the race. The
    telephony process then disappearing (it was never the active trigger)
    must not disarm the video-armed prompt; only video disappearing does.
    """
    video = ScriptedDetector([True, True, True, True, False, False], process_name="Google Chrome")
    telephony = ScriptedDetector([True, True, False, False, False, False], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2, debounce_disarm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=video,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome"]  # video won the race

    # Telephony vanishing (ticks 3-4) must not disarm a video-armed prompt.
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    # Video vanishing (ticks 5-6) is what actually disarms it.
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_IDLE


def test_telephony_streak_does_not_interfere_while_video_is_active(pro_feature_policy) -> None:
    """Telephony accumulating a present-streak while video already armed must be inert."""
    video = ScriptedDetector([True, True], process_name="Google Chrome")
    telephony = ScriptedDetector([True, True, True, True, True], process_name="avconferenced")
    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=video,
        telephony_detect_fn=telephony,
        consent_arm_check=_ok_gate,
        on_consent_required=consent_calls.append,
    )

    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome"]

    # Telephony has been present (and past its own debounce threshold) this
    # whole time too, but the state machine is not in STATE_IDLE anymore, so
    # it never gets a chance to also fire on_consent_required.
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome"]


def test_gate_fallthrough_lets_telephony_arm_when_video_check_fails_same_tick(pro_feature_policy) -> None:
    """A failed video arm-attempt must not block telephony's own attempt in the same tick."""
    video = ScriptedDetector([True, True], process_name="Google Chrome")
    telephony = ScriptedDetector([True, True], process_name="avconferenced")
    gate_calls = {"n": 0}

    def _flaky_gate() -> StepResult:
        gate_calls["n"] += 1
        # Fails on the first call this tick (video's check), succeeds on the
        # second (telephony's) -- exercises the fallthrough in
        # _advance_from_idle without needing genuinely independent gates
        # (video and telephony share one FEATURE_AUTOSTART/consent-arm gate).
        return StepResult(ok=gate_calls["n"] % 2 == 0, message="gate")

    consent_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=2),
        feature_policy=pro_feature_policy,
        detect_fn=video,
        telephony_detect_fn=telephony,
        consent_arm_check=_flaky_gate,
        on_consent_required=consent_calls.append,
    )

    monitor.tick()
    monitor.tick()

    assert monitor.state == STATE_CONSENT_PENDING
    assert consent_calls == ["avconferenced"]


# ---------------------------------------------------------------------------
# AutostartMonitorConfig.from_env
# ---------------------------------------------------------------------------


def test_config_from_env_reads_debounce_values(monkeypatch) -> None:
    monkeypatch.setenv("AUTOSTART_DEBOUNCE_ARM_POLLS", "5")
    monkeypatch.setenv("AUTOSTART_DEBOUNCE_DISARM_POLLS", "3")

    config = AutostartMonitorConfig.from_env()

    assert config.debounce_arm_polls == 5
    assert config.debounce_disarm_polls == 3


def test_config_from_env_clamps_non_positive_values_to_one(monkeypatch) -> None:
    monkeypatch.setenv("AUTOSTART_DEBOUNCE_ARM_POLLS", "0")
    monkeypatch.delenv("AUTOSTART_DEBOUNCE_DISARM_POLLS", raising=False)

    config = AutostartMonitorConfig.from_env()

    assert config.debounce_arm_polls == 1
    assert config.debounce_disarm_polls == 2


# ---------------------------------------------------------------------------
# run_autostart_monitor_loop
# ---------------------------------------------------------------------------


class _FakeMonitor:
    def __init__(self, *, raise_on_call: int | None = None) -> None:
        self.tick_calls = 0
        self._raise_on_call = raise_on_call

    def tick(self) -> str:
        self.tick_calls += 1
        if self._raise_on_call == self.tick_calls:
            raise RuntimeError("boom")
        return STATE_IDLE


async def _run_with_stop(coro_factory, stop_event: asyncio.Event, delay_s: float = 0.05) -> None:
    async def _stop_soon() -> None:
        await asyncio.sleep(delay_s)
        stop_event.set()

    stopper = asyncio.create_task(_stop_soon())
    await coro_factory()
    await stopper


async def test_run_autostart_monitor_loop_ticks_until_stopped() -> None:
    monitor = _FakeMonitor()
    stop_event = asyncio.Event()

    await _run_with_stop(
        lambda: run_autostart_monitor_loop(monitor, stop_event, poll_interval_s=0.01),
        stop_event,
    )

    assert monitor.tick_calls >= 2


async def test_run_autostart_monitor_loop_skips_ticks_while_call_active() -> None:
    monitor = _FakeMonitor()
    stop_event = asyncio.Event()

    await _run_with_stop(
        lambda: run_autostart_monitor_loop(
            monitor, stop_event, poll_interval_s=0.01, is_call_active=lambda: True
        ),
        stop_event,
    )

    assert monitor.tick_calls == 0


async def test_run_autostart_monitor_loop_survives_tick_exception() -> None:
    monitor = _FakeMonitor(raise_on_call=1)
    stop_event = asyncio.Event()

    await _run_with_stop(
        lambda: run_autostart_monitor_loop(monitor, stop_event, poll_interval_s=0.01),
        stop_event,
    )

    assert monitor.tick_calls >= 2
