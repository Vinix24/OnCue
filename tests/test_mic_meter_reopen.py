"""The microphone meter keeps moving after the device is lost and reopened mid-recording."""

from __future__ import annotations

import importlib.util
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

import sales_copilot.audio.capture as cap
from sales_copilot.audio.recorder_engine import RecorderEngine
from tests.test_mic_resilience import _dev, _FakeSd, _make, _park_watchdog, _tick


@pytest.fixture(autouse=True)
def _reset_stream_count(monkeypatch):
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)
    monkeypatch.setattr(cap, "_live_input_names", lambda: None)
    yield
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)


_SERVER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "record_server.py"


def _snapshot_dict():
    spec = importlib.util.spec_from_file_location("record_server_under_test", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module._snapshot_dict


class _Silent:
    """Stand-in for the other track: never delivers."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self):
        return None


def _engine(tmp_path, mic) -> RecorderEngine:
    return RecorderEngine(out_dir=tmp_path, streams={"prospect": _Silent(), "self": mic})


def _self_entry(engine: RecorderEngine, now: float) -> dict:
    msg = _snapshot_dict()(engine.snapshot(now))
    return next(s for s in msg["streams"] if s["label"] == "self")


def _wav_peak(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    return int(np.abs(data).max())


def _run(monkeypatch, tmp_path, *, devices, configured, after_loss) -> tuple[RecorderEngine, dict]:
    sd = _FakeSd(devices)
    mic, clock = _make(monkeypatch, sd, device=configured)
    engine = _engine(tmp_path, mic)
    engine.start(run_loop=False)
    _park_watchdog(mic)

    _tick(sd, mic, clock, 5)
    engine.pump(clock.now)
    assert _self_entry(engine, clock.now)["db"] is not None

    after_loss(sd, mic, clock)
    # The watchdog declares the loss and reopens (or keeps retrying).
    clock.now += 2.5
    mic._watchdog_step()
    engine.pump(clock.now)
    # Fresh stream delivers loud blocks.
    _tick(sd, mic, clock, 10)
    engine.pump(clock.now)
    entry = _self_entry(engine, clock.now)
    engine.stop()
    return engine, entry


def _assert_live(entry: dict, tmp_path, engine: RecorderEngine) -> None:
    assert entry["started"] is True
    assert entry["silent"] is False
    assert entry["db"] is not None and entry["db"] > -50.0
    wavs = list(Path(engine._directory).glob("self.wav"))
    assert wavs and _wav_peak(wavs[0]) > 1000


def test_meter_moves_after_lost_device_is_reopened(monkeypatch, tmp_path) -> None:
    engine, entry = _run(
        monkeypatch,
        tmp_path,
        devices=[_dev(0, "Built-in Mic")],
        configured=None,
        after_loss=lambda sd, mic, clock: None,
    )
    _assert_live(entry, tmp_path, engine)


def test_meter_moves_when_closing_the_lost_stream_raises(monkeypatch, tmp_path) -> None:
    def _break_close(sd, mic, clock) -> None:
        def _boom() -> None:
            raise RuntimeError("PortAudio error -50")

        sd.live.stop = _boom
        sd.live.close = _boom

    engine, entry = _run(
        monkeypatch,
        tmp_path,
        devices=[_dev(0, "Built-in Mic")],
        configured=None,
        after_loss=_break_close,
    )
    _assert_live(entry, tmp_path, engine)
    assert cap._active_portaudio_stream_count() == 0


def test_meter_moves_when_reopen_lands_on_fallback_device(monkeypatch, tmp_path) -> None:
    def _remove_headset(sd, mic, clock) -> None:
        sd.devices = [_dev(0, "Built-in Mic")]

    engine, entry = _run(
        monkeypatch,
        tmp_path,
        devices=[_dev(0, "Built-in Mic"), _dev(1, "Headset")],
        configured=1,
        after_loss=_remove_headset,
    )
    _assert_live(entry, tmp_path, engine)


def test_reopen_is_logged_at_warning_with_devices_and_outage(monkeypatch, caplog) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset")])
    mic, clock = _make(monkeypatch, sd, device=1)
    mic.start()
    _park_watchdog(mic)
    _tick(sd, mic, clock, 5)
    sd.devices = [_dev(0, "Built-in Mic")]
    clock.now += 2.5
    with caplog.at_level("WARNING", logger=cap.logger.name):
        mic._watchdog_step()
    reopened = [r for r in caplog.records if "Mic device reopened" in r.getMessage()]
    assert len(reopened) == 1
    assert reopened[0].levelname == "WARNING"
    text = reopened[0].getMessage()
    assert "Headset" in text and "Built-in Mic" in text and "(fallback)" in text and "outage" in text
    mic.stop()
