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

import pytest

from sales_copilot.core.autostart_monitor import (
    OUTCOME_ACCEPTED,
    OUTCOME_DECLINED,
    OUTCOME_EXPIRED,
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


def _collect(into: list[str]):
    """An ``on_consent_required`` callback that records only the process name."""

    return lambda _prompt_id, process, _source: into.append(process)


def _outcomes(into: list[str]):
    """An ``on_consent_resolved`` callback that records only the outcome."""

    return lambda _prompt_id, outcome: into.append(outcome)


def _confirm(monitor: RuntimeAutostartMonitor) -> bool:
    """Answer the open consent prompt with Yes."""

    return monitor.confirm_consent(monitor._prompt_id or "no-open-prompt")


def _decline(monitor: RuntimeAutostartMonitor) -> bool:
    """Answer the open consent prompt with No."""

    return monitor.decline_consent(monitor._prompt_id or "no-open-prompt")


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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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

    armed = _confirm(monitor)

    assert armed is True
    assert monitor.state == STATE_ARMED
    assert session_calls == ["Google Chrome"]


def test_confirm_consent_returns_false_when_nothing_pending(pro_feature_policy) -> None:
    session_calls: list[str] = []
    monitor = RuntimeAutostartMonitor(feature_policy=pro_feature_policy, on_session_arm=session_calls.append)

    assert _confirm(monitor) is False
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
        on_consent_required=_collect(consent_calls),
        on_session_arm=session_calls.append,
    )
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING

    _decline(monitor)

    assert monitor.state == STATE_IDLE
    assert session_calls == []
    # Re-detecting after a decline requires the full debounce again -- not an
    # immediate re-arm on the very next poll.
    assert monitor.tick() == STATE_IDLE
    assert monitor.tick() == STATE_CONSENT_PENDING
    assert consent_calls == ["Google Chrome", "Google Chrome"]


def test_decline_consent_is_noop_outside_pending(pro_feature_policy) -> None:
    monitor = RuntimeAutostartMonitor(feature_policy=pro_feature_policy)

    _decline(monitor)

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
    _confirm(monitor)
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
    _confirm(monitor)
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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

    armed = _confirm(monitor)

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
    _confirm(monitor)
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
    _confirm(monitor)
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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
        on_consent_required=_collect(consent_calls),
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


# ---------------------------------------------------------------------------
# observation age (ADR-003 decision 3), pending_prompt, withdraw_pending
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Detector:
    """Present by default; ``raising`` makes the read fail instead of returning."""

    def __init__(self) -> None:
        self.present = True
        self.raising = False

    def __call__(self, candidates: tuple[str, ...]):
        if self.raising:
            raise OSError("pgrep failed")
        return ("Google Chrome", 1, ["Google Chrome"]) if self.present else (None, None, [])


def _aging_monitor(policy, *, polls: int = 1):
    clock, detector = _Clock(), _Detector()
    required: list[str] = []
    armed: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=polls),
        feature_policy=policy,
        detect_fn=detector,
        telephony_detect_fn=_absent_detector,
        consent_arm_check=_ok_gate,
        on_consent_required=_collect(required),
        on_session_arm=armed.append,
        clock=clock,
    )
    return monitor, clock, detector, required, armed


def test_pending_prompt_names_process_and_source(pro_feature_policy) -> None:
    monitor, _clock, _detector, _required, _armed = _aging_monitor(pro_feature_policy)
    assert monitor.pending_prompt() is None

    monitor.tick()

    assert monitor.pending_prompt() == {
        "prompt_id": monitor._prompt_id,
        "process": "Google Chrome",
        "source": "video",
    }


def test_prompt_expires_after_10s_without_observation_and_debounce_restarts(pro_feature_policy) -> None:
    monitor, clock, detector, required, armed = _aging_monitor(pro_feature_policy, polls=2)
    monitor.tick()
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING
    detector.raising = True
    clock.now += 10.5
    monitor.tick()

    assert monitor.state == STATE_IDLE and monitor.pending_prompt() is None
    detector.raising = False
    assert monitor.tick() == STATE_IDLE  # first successful observation: streak 1 of 2
    assert monitor.tick() == STATE_CONSENT_PENDING
    assert required == ["Google Chrome", "Google Chrome"] and armed == []


def test_yes_between_ticks_on_a_too_old_observation_is_refused(pro_feature_policy) -> None:
    monitor, clock, _detector, _required, armed = _aging_monitor(pro_feature_policy)
    monitor.tick()
    clock.now += 11

    assert monitor.pending_prompt() is None
    assert _confirm(monitor) is False
    assert armed == [] and monitor.state == STATE_IDLE


def test_yes_on_a_fresh_observation_still_arms(pro_feature_policy) -> None:
    monitor, clock, _detector, _required, armed = _aging_monitor(pro_feature_policy)
    monitor.tick()
    clock.now += 9

    assert _confirm(monitor) is True
    assert armed == ["Google Chrome"]


def test_read_error_is_not_an_observation_and_starts_nothing(pro_feature_policy) -> None:
    monitor, clock, detector, required, armed = _aging_monitor(pro_feature_policy)
    detector.raising = True

    for _ in range(20):
        clock.now += 2
        assert monitor.tick() == STATE_IDLE

    assert required == [] and armed == []


def test_observation_stale_after_30s_logs_error_once(pro_feature_policy, caplog) -> None:
    monitor, clock, detector, _required, _armed = _aging_monitor(pro_feature_policy)
    monitor.tick()
    detector.raising = True
    assert monitor.observation_stale is False
    with caplog.at_level("ERROR"):
        for _ in range(20):
            clock.now += 2
            monitor.tick()

    assert monitor.observation_stale is True
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1
    detector.raising = False
    monitor.tick()
    assert monitor.observation_stale is False


def test_withdraw_pending_expires_an_open_prompt_only(pro_feature_policy) -> None:
    monitor, _clock, _detector, _required, armed = _aging_monitor(pro_feature_policy)
    monitor.withdraw_pending()
    monitor.tick()
    monitor.withdraw_pending()

    assert monitor.state == STATE_IDLE and monitor.pending_prompt() is None
    monitor.tick()
    _confirm(monitor)
    monitor.withdraw_pending()
    assert monitor.state == STATE_ARMED and armed == ["Google Chrome"]


# ---------------------------------------------------------------------------
# one exit for an open prompt: on_consent_resolved fires exactly once
# ---------------------------------------------------------------------------


def _resolving_monitor(policy, *, polls: int = 1):
    monitor, clock, detector, required, armed = _aging_monitor(policy, polls=polls)
    resolved: list[str] = []
    monitor.on_consent_resolved = _outcomes(resolved)
    return monitor, clock, detector, resolved


def test_yes_resolves_accepted_once(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    assert _confirm(monitor) is True
    monitor.withdraw_pending()
    _decline(monitor)

    assert resolved == [OUTCOME_ACCEPTED] and monitor.state == STATE_ARMED


def test_no_resolves_declined_once(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    assert _decline(monitor) is True
    assert _decline(monitor) is False
    assert _confirm(monitor) is False

    assert resolved == [OUTCOME_DECLINED] and monitor.state == STATE_IDLE


def test_source_gone_resolves_expired_once(pro_feature_policy) -> None:
    monitor, _clock, detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    detector.present = False
    for _ in range(5):
        monitor.tick()

    assert resolved == [OUTCOME_EXPIRED] and monitor.state == STATE_IDLE


def test_observation_too_old_in_a_tick_resolves_expired_once(pro_feature_policy) -> None:
    monitor, clock, detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    detector.raising = True
    clock.now += 10.5
    monitor.tick()
    monitor.tick()

    assert resolved == [OUTCOME_EXPIRED]


def test_refused_yes_on_too_old_observation_resolves_expired_once(pro_feature_policy) -> None:
    monitor, clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    clock.now += 11

    assert _confirm(monitor) is False
    assert _confirm(monitor) is False
    assert resolved == [OUTCOME_EXPIRED]


def test_refused_no_on_too_old_observation_resolves_expired_once(pro_feature_policy) -> None:
    monitor, clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    clock.now += 11

    assert _decline(monitor) is False
    assert _decline(monitor) is False
    assert resolved == [OUTCOME_EXPIRED]
    assert monitor.state == STATE_IDLE


def test_consent_required_carries_the_source(pro_feature_policy) -> None:
    sources: list[tuple[str, str]] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1),
        feature_policy=pro_feature_policy,
        detect_fn=_absent_detector,
        telephony_detect_fn=lambda candidates: ("avconferenced", 1, ["avconferenced"]),
        consent_arm_check=_ok_gate,
        on_consent_required=lambda _prompt_id, process, source: sources.append((process, source)),
    )
    monitor.tick()

    assert sources == [("avconferenced", "telephony")]


def test_manual_start_withdrawal_resolves_expired_once(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.withdraw_pending()
    assert resolved == []
    monitor.tick()
    monitor.withdraw_pending()
    monitor.withdraw_pending()

    assert resolved == [OUTCOME_EXPIRED] and monitor.state == STATE_IDLE


# ---------------------------------------------------------------------------
# prompt ids: every prompt has its own id, and only that id answers it
# ---------------------------------------------------------------------------


def test_every_prompt_gets_a_fresh_id_and_reports_process_and_source(pro_feature_policy) -> None:
    monitor, _clock, _detector, _required, _armed = _aging_monitor(pro_feature_policy)
    seen: list[tuple[str, str, str]] = []
    monitor.on_consent_required = lambda prompt_id, process, source: seen.append((prompt_id, process, source))

    monitor.tick()
    first_id = monitor._prompt_id
    _decline(monitor)
    monitor.tick()

    assert first_id and monitor._prompt_id and monitor._prompt_id != first_id
    assert seen == [
        (first_id, "Google Chrome", "video"),
        (monitor._prompt_id, "Google Chrome", "video"),
    ]


@pytest.mark.parametrize("answer", ["confirm_consent", "decline_consent"])
@pytest.mark.parametrize("stale_id", ["", "unknown-id", "old-id"])
def test_wrong_old_or_unknown_prompt_id_changes_nothing(pro_feature_policy, answer: str, stale_id: str) -> None:
    monitor, _clock, _detector, resolved = _resolving_monitor(pro_feature_policy)
    monitor.tick()
    before: list[str] = []
    if stale_id == "old-id":
        stale_id = monitor._prompt_id or ""
        _decline(monitor)
        monitor.tick()
        before = [OUTCOME_DECLINED]

    assert getattr(monitor, answer)(stale_id) is False
    assert monitor.state == STATE_CONSENT_PENDING
    assert monitor.pending_prompt() is not None
    assert resolved == before


def test_consent_resolved_carries_the_id_of_the_prompt_it_resolves(pro_feature_policy) -> None:
    monitor, _clock, _detector, _required, _armed = _aging_monitor(pro_feature_policy)
    resolved: list[tuple[str, str]] = []
    monitor.on_consent_resolved = lambda prompt_id, outcome: resolved.append((prompt_id, outcome))

    monitor.tick()
    first_id = monitor._prompt_id
    _decline(monitor)
    monitor.tick()
    second_id = monitor._prompt_id
    _confirm(monitor)

    assert resolved == [(first_id, OUTCOME_DECLINED), (second_id, OUTCOME_ACCEPTED)]


# ---------------------------------------------------------------------------
# safety net: the other route runs (ADR-004 decision 8)
# ---------------------------------------------------------------------------


class _Switch:
    def __init__(self) -> None:
        self.on = False

    def __call__(self) -> bool:
        return self.on


def _route_monitor(policy):
    monitor, clock, detector, resolved = _resolving_monitor(policy)
    switch = _Switch()
    monitor.other_route_running = switch
    prompts: list[str] = []
    monitor.on_consent_required = lambda prompt_id, _process, _source: prompts.append(prompt_id)
    return monitor, clock, detector, resolved, switch, prompts


def test_polls_pause_while_the_other_route_runs_and_log_once(pro_feature_policy, caplog) -> None:
    monitor, _clock, detector, _resolved, switch, _prompts = _route_monitor(pro_feature_policy)
    detector_calls = {"n": 0}
    inner = monitor.detect_fn

    def counting(candidates):
        detector_calls["n"] += 1
        return inner(candidates)

    monitor.detect_fn = counting
    telephony = ScriptedDetector([True])
    monitor.telephony_detect_fn = telephony
    switch.on = True
    with caplog.at_level("INFO"):
        for _ in range(4):
            monitor.tick()

    assert detector_calls["n"] == 0 and telephony.calls == 0
    assert monitor.state == STATE_IDLE
    assert sum("polls paused" in r.getMessage() for r in caplog.records) == 1


def test_open_prompt_expires_once_when_the_other_route_starts(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved, switch, prompts = _route_monitor(pro_feature_policy)
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING and len(prompts) == 1
    switch.on = True
    monitor.tick()
    monitor.tick()

    assert resolved == [OUTCOME_EXPIRED]
    assert monitor.state == STATE_IDLE and monitor.pending_prompt() is None


def test_polls_resume_after_the_other_route_stops_with_a_new_prompt_id(pro_feature_policy, caplog) -> None:
    monitor, _clock, _detector, _resolved, switch, prompts = _route_monitor(pro_feature_policy)
    monitor.tick()
    switch.on = True
    monitor.tick()
    switch.on = False
    with caplog.at_level("INFO"):
        monitor.tick()
        monitor.tick()

    assert monitor.state == STATE_CONSENT_PENDING
    assert len(prompts) == 2 and prompts[0] != prompts[1]
    assert sum("polls resume" in r.getMessage() for r in caplog.records) == 1


def test_yes_and_no_are_refused_while_the_other_route_runs(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved, switch, _prompts = _route_monitor(pro_feature_policy)
    armed: list[str] = []
    monitor.on_session_arm = armed.append
    monitor.tick()
    prompt_id = monitor._prompt_id
    switch.on = True

    assert monitor.confirm_consent(prompt_id) is False
    assert monitor.decline_consent(prompt_id) is False
    assert armed == [] and resolved == [OUTCOME_EXPIRED]
    assert monitor.state == STATE_IDLE


def test_no_is_refused_while_the_other_route_runs(pro_feature_policy) -> None:
    monitor, _clock, _detector, resolved, switch, _prompts = _route_monitor(pro_feature_policy)
    monitor.tick()
    switch.on = True

    assert _decline(monitor) is False
    assert resolved == [OUTCOME_EXPIRED]
