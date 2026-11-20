"""ADR-008 point 3: the loopback follows the call's endpoint without losing the timeline.

The selector (``read_snapshot``) and ``soundcard`` are fakes; time runs on an
injected clock, so gaps and outages are exact.
"""

from __future__ import annotations

import logging
import re

import numpy as np

from sales_copilot.audio import wasapi
from sales_copilot.audio.tap_health import TapSignalState
from sales_copilot.audio.wasapi import WasapiLoopbackStream
from sales_copilot.audio.wasapi_endpoint import EndpointSnapshot
from tests.test_wasapi_loopback import (
    _RECOVERY_CHUNK,
    _config,
    _drain,
    _FakeMicrophone,
    _FakeSoundcardModule,
    _FakeSpeaker,
    _good,
    _install_fake_soundcard,
    _ScriptedRecorderCM,
    _wait_until,
)

SPEAKERS = _FakeSpeaker("spk", "Speakers")
HEADSET = _FakeSpeaker("bt", "Headset")


class _TickClock:
    """Advances 1 ms per reading and by ``sleep``; ``jump`` models slow driver calls."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += 0.001
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def jump(self, seconds: float) -> None:
        self.now += seconds


class _TrackedCM(_ScriptedRecorderCM):
    def __init__(self, name: str, events: list[str], clock: _TickClock, open_cost: float = 0.0, **kw) -> None:
        super().__init__([], **kw)
        self._name, self._events, self._clock, self._open_cost = name, events, clock, open_cost

    def __enter__(self):
        self._clock.jump(self._open_cost)
        self._events.append(f"open:{self._name}")
        return super().__enter__()

    def __exit__(self, *exc):
        self._events.append(f"close:{self._name}")
        return super().__exit__(*exc)


class _FollowSoundcard(_FakeSoundcardModule):
    """Two endpoints; ``blocked`` ids refuse to open (headset switched off)."""

    def __init__(self, mics: dict[str, _FakeMicrophone]) -> None:
        super().__init__(
            default_speaker_holder=[SPEAKERS], all_speakers_list=[SPEAKERS, HEADSET], microphones_by_id=mics
        )
        self.blocked: set[str] = set()
        self.open_attempts: list[str] = []

    def get_microphone(self, id, include_loopback: bool = False):  # noqa: A002
        self.open_attempts.append(id)
        if id in self.blocked:
            raise RuntimeError("endpoint gone")
        return super().get_microphone(id, include_loopback=include_loopback)


class _Rig:
    def __init__(self, monkeypatch, *, follow=True, chosen="spk", gap_cost=0.5, **config) -> None:
        self.clock = _TickClock()
        self.events: list[str] = []
        self.chosen = [chosen]
        self.snapshot_calls = 0
        self.spk = _TrackedCM("spk", self.events, self.clock, repeat=_good(0.1))
        self.bt = _TrackedCM("bt", self.events, self.clock, gap_cost, repeat=_good(0.2))
        self.sc = _FollowSoundcard({"spk": _FakeMicrophone(self.spk), "bt": _FakeMicrophone(self.bt)})
        self.warnings: list[dict] = []

        def snapshot() -> EndpointSnapshot:
            self.snapshot_calls += 1
            return EndpointSnapshot(communications_id=self.chosen[0], console_id="spk")

        monkeypatch.setattr(wasapi, "read_snapshot", snapshot)
        _install_fake_soundcard(monkeypatch, self.sc)
        self.stream = WasapiLoopbackStream(
            _config(chunk_size=_RECOVERY_CHUNK, windows_endpoint_follow=follow, **config),
            on_warning=self.warnings.append,
            silence_timeout_multiplier=1e9,
            empty_pull_sleep_s=0.001,
            recovery_retry_s=1.0,
            follow_poll_seconds=0.05,
            outage_grace_s=10.0,
            clock=self.clock.monotonic,
            sleep=self.clock.sleep,
        )


def test_switch_off_makes_no_selector_call(monkeypatch) -> None:
    rig = _Rig(monkeypatch, follow=False, chosen="bt")
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 100, timeout=5.0)
    rig.stream.stop()
    assert rig.snapshot_calls == 0
    assert rig.sc.open_attempts == ["spk"]


def test_matched_override_wins_and_is_never_displaced(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt", wasapi_endpoint_name="Speakers")
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 100, timeout=5.0)
    rig.stream.stop()
    assert rig.snapshot_calls == 0
    assert rig.sc.open_attempts == ["spk"]


def test_start_opens_the_selected_endpoint(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    rig.stream.start()
    assert _wait_until(lambda: rig.bt.reads >= 3, timeout=5.0)
    rig.stream.stop()
    assert rig.sc.open_attempts[0] == "bt"
    assert rig.spk.reads == 0


def test_selector_without_information_falls_back_to_the_default(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    monkeypatch.setattr(wasapi, "read_snapshot", lambda: None)
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 100, timeout=5.0)
    rig.stream.stop()
    assert rig.sc.open_attempts == ["spk"]


def test_changed_choice_opens_new_before_closing_old_and_fills_the_gap(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger=wasapi.logger.name)
    rig = _Rig(monkeypatch, gap_cost=0.5)
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 3, timeout=5.0)
    rig.chosen[0] = "bt"
    assert _wait_until(lambda: rig.bt.reads >= 3, timeout=5.0)
    rig.stream.stop()

    assert rig.events[:3] == ["open:spk", "open:bt", "close:spk"]
    line = next(r.getMessage() for r in caplog.records if "switched endpoint" in r.getMessage())
    assert "Speakers -> Headset" in line and "communications_default" in line
    gap_ms = int(re.search(r"gap: (\d+) ms", line).group(1))
    assert 500 <= gap_ms <= 510

    values = [float(c[0, 0]) for c in _drain(rig.stream)]
    first_zero = values.index(0.0)
    zeros = 0
    while values[first_zero + zeros] == 0.0:
        zeros += 1
    # 0.5 s at 0.125 s per chunk: the frame count across the switch matches elapsed time.
    assert zeros == int(gap_ms / 1000 / (_RECOVERY_CHUNK / 16000))
    assert values[first_zero - 1] == np.float32(0.1)
    assert values[first_zero + zeros] == np.float32(0.2)


def test_failed_open_keeps_the_old_endpoint_and_retries_at_the_next_poll(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 3, timeout=5.0)
    rig.sc.blocked.add("bt")
    rig.chosen[0] = "bt"
    assert _wait_until(lambda: rig.sc.open_attempts.count("bt") >= 2, timeout=5.0)
    assert "close:spk" not in rig.events
    reads = rig.spk.reads
    assert _wait_until(lambda: rig.spk.reads > reads, timeout=5.0)  # old endpoint keeps being read
    rig.sc.blocked.clear()
    assert _wait_until(lambda: rig.bt.reads >= 3, timeout=5.0)
    rig.stream.stop()
    assert rig.events.index("open:bt") < rig.events.index("close:spk")


def test_endpoint_lost_then_found_within_ten_seconds_recovers_without_warning(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    rig.spk._script = [_good(0.1), RuntimeError("headset off")]  # noqa: SLF001
    rig.spk._repeat = None  # noqa: SLF001

    def headset_off() -> None:
        rig.chosen[0] = "bt"
        rig.sc.blocked.add("bt")

    rig.spk._on_raise = headset_off  # noqa: SLF001
    real_get = rig.sc.get_microphone

    def get(id, include_loopback=False):  # noqa: A002
        if id == "bt" and rig.sc.open_attempts.count("bt") >= 2:
            rig.sc.blocked.clear()  # the headset is back, still inside the 10 s grace
        return real_get(id, include_loopback=include_loopback)

    rig.sc.get_microphone = get
    rig.stream.start()
    assert _wait_until(lambda: rig.bt.reads >= 3, timeout=5.0)
    rig.stream.stop()
    assert rig.sc.open_attempts.count("bt") == 3
    assert rig.warnings == []
    assert rig.stream.tap_health().state != TapSignalState.NO_TAP


def test_nothing_for_ten_seconds_reports_no_tap_and_one_warning(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    rig.bt._script = [_good(0.2), RuntimeError("headset off")]  # noqa: SLF001
    rig.bt._repeat = None  # noqa: SLF001
    seen: list[tuple[float, TapSignalState, int]] = []
    real_get = rig.sc.get_microphone
    armed = [False]

    def get(id, include_loopback=False):  # noqa: A002
        if armed[0]:
            seen.append((rig.clock.now, rig.stream.tap_health().state, len(rig.warnings)))
            if rig.clock.now > 12.0:
                rig.sc.blocked.clear()
                rig.bt._repeat = _good(0.2)  # noqa: SLF001
        return real_get(id, include_loopback=include_loopback)

    def lost() -> None:
        armed[0] = True
        rig.sc.blocked.update({"bt", "spk"})

    rig.bt._on_raise = lost  # noqa: SLF001
    rig.sc.get_microphone = get
    rig.stream.start()
    assert _wait_until(
        lambda: rig.bt.reads >= 4 and rig.stream.tap_health().state != TapSignalState.NO_TAP, timeout=5.0
    )
    rig.stream.stop()

    early = [(state, warned) for t, state, warned in seen if t < seen[0][0] + 9.0]
    assert early and all(w == 0 and st != TapSignalState.NO_TAP for st, w in early)
    late = [(state, warned) for t, state, warned in seen if t > seen[0][0] + 10.5]
    assert late and all(w == 1 and st == TapSignalState.NO_TAP for st, w in late)
    assert len(rig.warnings) == 1


def test_no_information_while_polling_follows_the_default_output(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 3, timeout=5.0)
    monkeypatch.setattr(wasapi, "read_snapshot", lambda: None)  # selector has no information
    rig.sc._default_speaker_holder[0] = HEADSET  # noqa: SLF001
    assert _wait_until(lambda: rig.bt.reads >= 3, timeout=5.0)
    rig.stream.stop()
    assert rig.sc.open_attempts[-1] == "bt"


def test_reads_failing_after_a_good_open_still_reach_no_tap_with_one_warning(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    rig.spk._script = [_good(0.1)] + [RuntimeError("read failed")] * 200  # noqa: SLF001
    rig.spk._repeat = None  # noqa: SLF001
    # After the first block every read raises, while opening keeps succeeding.
    rig.stream.start()
    # Wall-clock wait: the reader thread must run 10 s of injected outage grace, and under
    # heavy load 5 s was too short (measured load 20-11 during a local .dmg build). A real
    # bug still fails, because no_tap then never comes.
    assert _wait_until(lambda: rig.stream.tap_health().state == TapSignalState.NO_TAP, timeout=30.0)
    rig.stream.stop()
    assert len(rig.warnings) == 1
    assert rig.clock.now >= 10.0


def test_broken_call_app_list_is_logged_once_across_polls(monkeypatch, caplog) -> None:
    def broken() -> frozenset[str]:
        raise OSError("no such file")

    rig = _Rig(monkeypatch)
    monkeypatch.setattr(wasapi, "load_call_apps", broken)
    caplog.set_level(logging.WARNING, logger=wasapi.logger.name)
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert len([r for r in caplog.records if "call-app list" in r.getMessage()]) == 1


def test_slow_snapshot_is_logged_with_its_duration_and_a_fast_one_is_not(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger=wasapi.logger.name)
    rig = _Rig(monkeypatch)

    def slow() -> EndpointSnapshot:
        rig.clock.jump(0.25)
        return EndpointSnapshot(communications_id="spk", console_id="spk")

    monkeypatch.setattr(wasapi, "read_snapshot", slow)
    rig.stream._select_speaker(rig.sc)  # noqa: SLF001
    lines = [r.getMessage() for r in caplog.records if "snapshot took" in r.getMessage()]
    assert len(lines) == 1 and re.search(r"took (2[5-9]\d) ms", lines[0])

    caplog.clear()
    monkeypatch.setattr(wasapi, "read_snapshot", lambda: EndpointSnapshot(communications_id="spk", console_id="spk"))
    rig.stream._select_speaker(rig.sc)  # noqa: SLF001
    assert not [r for r in caplog.records if "snapshot took" in r.getMessage()]


def test_shape_is_validated_again_after_a_switch(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    rig.bt._repeat = np.zeros((4,), dtype=np.float32)  # noqa: SLF001  1-D block from the new endpoint
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 3, timeout=5.0)
    rig.chosen[0] = "bt"
    assert _wait_until(lambda: len(rig.warnings) >= 1, timeout=5.0)
    rig.stream.stop()
    assert len(rig.warnings) == 1 and rig.warnings[0]["type"] == "audio_warning"


def test_selected_endpoint_that_cannot_open_falls_back_in_the_same_pass(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    rig.sc.blocked.add("bt")
    rig.stream.start()
    assert _wait_until(lambda: rig.spk.reads >= 3, timeout=5.0)
    rig.stream.stop()
    assert rig.sc.open_attempts[:2] == ["bt", "spk"]
    assert rig.bt.reads == 0
    assert rig.warnings == []


def test_recovery_open_falls_back_to_the_default_when_the_selected_one_fails(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    rig.sc.blocked.add("bt")
    rig.stream._sc = rig.sc  # noqa: SLF001
    assert rig.stream._open_recorder(silent=True) is True  # noqa: SLF001
    assert rig.sc.open_attempts == ["bt", "spk"]
    assert rig.stream._speaker_id == "spk"  # noqa: SLF001
    rig.stream._close_recorder()  # noqa: SLF001


def test_every_candidate_failing_stays_a_visible_failure(monkeypatch) -> None:
    rig = _Rig(monkeypatch, chosen="bt")
    rig.sc.blocked.update({"bt", "spk"})
    rig.stream._sc = rig.sc  # noqa: SLF001
    assert rig.stream._open_recorder(silent=True) is False  # noqa: SLF001
    assert rig.sc.open_attempts == ["bt", "spk"]
    assert rig.stream._open_recorder() is False  # noqa: SLF001
    assert len(rig.warnings) == 1


def test_many_short_switches_keep_the_emitted_frames_on_elapsed_time(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    chunk_s = _RECOVERY_CHUNK / 16000
    emitted: list[int] = []
    monkeypatch.setattr(rig.stream, "_emit_silence_chunk", lambda: emitted.append(1))
    gap = chunk_s / 3
    for _ in range(30):
        rig.stream._fill_silence(gap)  # noqa: SLF001
    elapsed = 30 * gap
    assert abs(len(emitted) * chunk_s - elapsed) < chunk_s
    assert len(emitted) == 10


def test_call_app_list_resolves_in_the_installed_layout(tmp_path, monkeypatch) -> None:
    from sales_copilot.audio.wasapi_endpoint import load_call_apps
    from sales_copilot.core import paths

    resources = tmp_path / "_resources"
    (resources / "config").mkdir(parents=True)
    (resources / "config" / "wasapi_call_apps.yaml").write_text("call_apps:\n  - Teams.exe\n", encoding="utf-8")
    monkeypatch.setattr(paths, "_find_repo_root", lambda: None)
    monkeypatch.setattr(paths, "is_frozen_app", lambda: False)
    monkeypatch.setattr(paths, "_installed_package_resources_dir", lambda: resources)
    monkeypatch.setattr(paths, "_installed_package_data_dir", lambda: tmp_path / "support")

    assert load_call_apps() == frozenset({"teams.exe"})


def test_missing_call_app_file_logs_one_warning_naming_the_path(tmp_path, monkeypatch, caplog) -> None:
    from sales_copilot.core import paths

    resources = tmp_path / "_resources"
    resources.mkdir()
    monkeypatch.setattr(paths, "_find_repo_root", lambda: None)
    monkeypatch.setattr(paths, "is_frozen_app", lambda: False)
    monkeypatch.setattr(paths, "_installed_package_resources_dir", lambda: resources)
    monkeypatch.setattr(paths, "_installed_package_data_dir", lambda: tmp_path / "support")
    rig = _Rig(monkeypatch)
    caplog.set_level(logging.WARNING, logger=wasapi.logger.name)
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    lines = [r.getMessage() for r in caplog.records if "call-app list" in r.getMessage()]
    assert len(lines) == 1
    assert str(resources / "config" / "wasapi_call_apps.yaml") in lines[0]


def test_selected_id_without_a_matching_speaker_names_the_real_reason(monkeypatch, caplog) -> None:
    rig = _Rig(monkeypatch, chosen="ghost")
    caplog.set_level(logging.INFO, logger=wasapi.logger.name)
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_reason == "communications_default, no matching speaker"  # noqa: SLF001
    assert rig.stream._follow_endpoint() is False  # noqa: SLF001


def test_default_speaker_lookup_failure_is_logged_once_with_type_and_message(monkeypatch, caplog) -> None:
    rig = _Rig(monkeypatch, chosen="ghost")
    rig.stream._sc = rig.sc  # noqa: SLF001

    def broken():
        raise OSError("enumeration blew up")

    monkeypatch.setattr(rig.sc, "default_speaker", broken)
    caplog.set_level(logging.WARNING, logger=wasapi.logger.name)
    assert rig.stream._follow_endpoint() is False  # noqa: SLF001
    assert rig.stream._follow_endpoint() is False  # noqa: SLF001
    hits = [r.getMessage() for r in caplog.records if "default-speaker lookup failed" in r.getMessage()]
    assert len(hits) == 1
    assert "OSError" in hits[0]
    assert "enumeration blew up" in hits[0]


def test_default_speaker_lookup_failure_in_the_drift_check_is_logged(monkeypatch, caplog) -> None:
    rig = _Rig(monkeypatch)
    rig.stream._sc = rig.sc  # noqa: SLF001
    rig.stream._following_default = True  # noqa: SLF001

    def broken():
        raise RuntimeError("no audio service")

    monkeypatch.setattr(rig.sc, "default_speaker", broken)
    caplog.set_level(logging.WARNING, logger=wasapi.logger.name)
    rig.stream._check_default_endpoint_drift()  # noqa: SLF001
    assert any("RuntimeError: no audio service" in r.getMessage() for r in caplog.records)


def test_no_endpoint_result_sets_the_precise_select_reason(monkeypatch) -> None:
    rig = _Rig(monkeypatch)
    monkeypatch.setattr(wasapi, "pick_endpoint_id", lambda *a, **k: (None, "no_endpoint"))
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_reason == "no_endpoint"  # noqa: SLF001


def test_failed_call_app_list_sets_the_precise_select_reason_on_every_poll(monkeypatch) -> None:
    def broken() -> frozenset[str]:
        raise OSError("no such file")

    rig = _Rig(monkeypatch)
    monkeypatch.setattr(wasapi, "load_call_apps", broken)
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_reason == "call_apps_unavailable"  # noqa: SLF001
    rig.stream._select_reason = ""  # noqa: SLF001
    assert rig.stream._select_speaker(rig.sc) is None  # noqa: SLF001
    assert rig.stream._select_reason == "call_apps_unavailable"  # noqa: SLF001


def test_primary_recorder_failure_is_not_masked_by_a_later_loopback_failure(monkeypatch) -> None:
    from sales_copilot.core.i18n import t

    rig = _Rig(monkeypatch, chosen="bt")

    class _NoRecorder:
        def recorder(self, **_kw):
            raise RuntimeError("recorder refused")

    def get_microphone(id, include_loopback: bool = False):  # noqa: A002
        if id == "bt":
            return _NoRecorder()
        raise RuntimeError("endpoint gone")

    monkeypatch.setattr(rig.sc, "get_microphone", get_microphone)
    rig.stream._sc = rig.sc  # noqa: SLF001
    assert rig.stream._open_recorder() is False  # noqa: SLF001
    assert [w["message"] for w in rig.warnings] == [t("audio.wasapi_recorder_open_failed")]
