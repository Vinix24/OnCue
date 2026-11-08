"""Auto-arm picks the prospect audio source from the trigger.

A telephony-triggered auto-arm must capture the phone-call tap
(``prospect_source = "audiotee_call"``); a video-triggered one keeps the
configured source. The tests drive the real ``RuntimeAutostartMonitor`` built
by ``hub._make_autostart_monitor()`` through tick -> confirm_consent, with
scripted detectors, so the trigger really travels monitor -> hub callback ->
start-call config -> module audio config. Hermetic: no ``pgrep``, no call, no
audio devices.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from sales_copilot.audio.capture import resolve_prospect_source
from sales_copilot.auth.license_format import FEATURE_AUTOSTART, FEATURE_CALLTAP
from sales_copilot.core.autostart_monitor import (
    TRIGGER_TELEPHONY,
    TRIGGER_VIDEO,
    AutostartMonitorConfig,
    RuntimeAutostartMonitor,
)
from sales_copilot.modules.talk_time.__main__ import _load_audio_config as talk_time_audio_config
from sales_copilot.modules.transcriber.__main__ import _load_audio_config as transcriber_audio_config
from sales_copilot.websocket import hub, hub_core
from sales_copilot.wizard.steps import StepResult


class _AllowAllExcept:
    """Feature policy stub: grants everything except the listed features."""

    def __init__(self, denied: frozenset[str] = frozenset()) -> None:
        self._denied = denied

    def allows(self, feature_id: str) -> bool:
        return feature_id not in self._denied


def _present(candidates: tuple[str, ...]) -> tuple[str, int, list[str]]:
    return "fake-process", 4242, ["fake-process"]


def _absent(candidates: tuple[str, ...]) -> tuple[None, None, list[str]]:
    return None, None, []


@pytest.fixture(autouse=True)
def _reset_hub_state(monkeypatch: pytest.MonkeyPatch):
    # Deterministic entitlements: the license on the test machine must not matter.
    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _AllowAllExcept())
    hub_core.reset_config_state()
    yield
    hub_core.reset_config_state()


@pytest.fixture()
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    sent: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        sent.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    return sent


def _armed_monitor(trigger: str, policy: _AllowAllExcept) -> RuntimeAutostartMonitor:
    """The hub's real monitor, with scripted detection, driven to the confirm tick."""
    monitor = hub._make_autostart_monitor()
    monitor.config = AutostartMonitorConfig(debounce_arm_polls=1)
    monitor.feature_policy = policy  # type: ignore[assignment]
    monitor.consent_arm_check = lambda: StepResult(ok=True, message="ok")
    monitor.detect_fn = _present if trigger == TRIGGER_VIDEO else _absent
    monitor.telephony_detect_fn = _present if trigger == TRIGGER_TELEPHONY else _absent
    monitor.tick()
    assert monitor.active_trigger == trigger
    return monitor


async def _confirm(monitor: RuntimeAutostartMonitor) -> None:
    assert monitor.confirm_consent() is True
    await asyncio.sleep(0)


def test_build_config_telephony_trigger_sets_calltap_source() -> None:
    assert hub._build_autostart_call_config(TRIGGER_TELEPHONY)["prospect_source"] == "audiotee_call"


def test_build_config_video_and_unknown_trigger_do_not_override() -> None:
    assert "prospect_source" not in hub._build_autostart_call_config(TRIGGER_VIDEO)
    assert "prospect_source" not in hub._build_autostart_call_config(None)
    assert "prospect_source" not in hub._build_autostart_call_config()


async def test_telephony_trigger_arms_with_calltap_source(broadcasts) -> None:
    monitor = _armed_monitor(TRIGGER_TELEPHONY, _AllowAllExcept())

    await _confirm(monitor)

    config = hub_core.get_latest_config()
    assert config is not None
    assert config["prospect_source"] == "audiotee_call"
    assert hub_core._call_active is True
    assert [b for b in broadcasts if b[0] == "config"] == [("config", {"type": "start_call", "config": config})]


async def test_video_trigger_arms_without_override(broadcasts) -> None:
    monitor = _armed_monitor(TRIGGER_VIDEO, _AllowAllExcept())

    await _confirm(monitor)

    config = hub_core.get_latest_config()
    assert config is not None
    assert "prospect_source" not in config
    assert hub_core._call_active is True


async def test_telephony_without_calltap_falls_back_with_one_warning(
    broadcasts, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _AllowAllExcept(frozenset({FEATURE_CALLTAP})))
    monitor = _armed_monitor(TRIGGER_TELEPHONY, _AllowAllExcept())

    with caplog.at_level(logging.WARNING, logger=hub.logger.name):
        await _confirm(monitor)

    config = hub_core.get_latest_config()
    assert config is not None
    assert "prospect_source" not in config
    assert hub_core._call_active is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert FEATURE_CALLTAP in warnings[0].getMessage()


async def test_other_missing_entitlements_still_block(broadcasts, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_core, "missing_entitlements", lambda _config: ["some.feature"])
    monitor = _armed_monitor(TRIGGER_VIDEO, _AllowAllExcept())

    await _confirm(monitor)

    assert hub_core.get_latest_config() is None
    assert hub_core._call_active is False
    assert [b for b in broadcasts if b[0] == "config"] == []


def test_confirm_consent_keeps_existing_one_argument_callback() -> None:
    seen: list[str] = []
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1),
        feature_policy=_AllowAllExcept(),  # type: ignore[arg-type]
        detect_fn=_absent,
        telephony_detect_fn=_present,
        consent_arm_check=lambda: StepResult(ok=True, message="ok"),
        on_session_arm=seen.append,
    )
    monitor.tick()

    assert monitor.confirm_consent() is True
    assert seen == ["fake-process"]
    assert FEATURE_AUTOSTART  # entitlement id the monitor gates on is unchanged


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [(TRIGGER_TELEPHONY, "audiotee_call"), (TRIGGER_VIDEO, "blackhole")],
)
async def test_per_call_source_reaches_module_audio_config(
    broadcasts, monkeypatch: pytest.MonkeyPatch, trigger: str, expected: str
) -> None:
    """Step 4: the per-call source wins over the env in both capture modules.

    ``PROSPECT_SOURCE`` is pinned to blackhole. The modules are spawned by
    ``_run_call`` after ``wait_for_start_call`` returns, i.e. after
    ``apply_start_call`` stored the config, so ``_load_audio_config()`` sees it.
    """
    monkeypatch.setenv("PROSPECT_SOURCE", "blackhole")
    monkeypatch.setattr("sales_copilot.audio.capture.sys.platform", "darwin")
    monitor = _armed_monitor(trigger, _AllowAllExcept())

    await _confirm(monitor)

    assert transcriber_audio_config().prospect_source == expected
    assert talk_time_audio_config().prospect_source == expected
    assert resolve_prospect_source("blackhole", hub_core.get_latest_config(), platform="darwin") == expected
