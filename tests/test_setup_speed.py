from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest
import yaml

from sales_copilot.core import i18n
from sales_copilot.core import setup_speed as ss
from sales_copilot.core.env_writer import read_env_value
from sales_copilot.modules.transcriber.backends.model_names import GGML_DOWNLOAD_COMMAND
from sales_copilot.wizard.detectors import detect_speed

TH = ss.SpeedThresholds(fast_max_rtf=0.5, slow_min_rtf=1.0, medium_model="base")
I18N = Path(__file__).resolve().parents[1] / "config" / "i18n"


@pytest.mark.parametrize(
    ("rtf", "tier", "model", "live_ok"),
    [
        (0.4999, "fast", "large-v3-turbo", True),
        (0.5, "medium", "base", True),
        (0.9999, "medium", "base", True),
        (1.0, "too_slow", "large-v3-turbo", False),
    ],
)
def test_decide_fast_medium_too_slow_and_exact_thresholds(rtf, tier, model, live_ok):
    choice = ss.decide(rtf, TH)
    assert (choice.tier, choice.model, choice.live_ok) == (tier, model, live_ok)


@pytest.mark.parametrize("bad", [None, "0.3", float("nan"), float("inf"), -1.0, 0, True])
def test_decide_refuses_a_missing_or_malformed_factor(bad):
    with pytest.raises(ValueError):
        ss.decide(bad, TH)


def test_thresholds_come_from_env_and_invalid_ones_are_refused(monkeypatch):
    monkeypatch.setenv("SETUP_SPEED_FAST_MAX_RTF", "0.3")
    monkeypatch.setenv("SETUP_SPEED_SLOW_MIN_RTF", "0.7")
    monkeypatch.setenv("SETUP_SPEED_MEDIUM_MODEL", "tiny")
    assert ss.SpeedThresholds.from_env() == ss.SpeedThresholds(0.3, 0.7, "tiny")
    for fast, slow, medium in [("0.9", "0.5", "base"), ("0", "1", "base"), ("0.5", "1", "nope")]:
        monkeypatch.setenv("SETUP_SPEED_FAST_MAX_RTF", fast)
        monkeypatch.setenv("SETUP_SPEED_SLOW_MIN_RTF", slow)
        monkeypatch.setenv("SETUP_SPEED_MEDIUM_MODEL", medium)
        with pytest.raises(ValueError):
            ss.SpeedThresholds.from_env()


def _clip(tmp_path: Path, name: str, seconds: float, rate: int = 16000, channels: int = 1) -> Path:
    path = tmp_path / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.zeros(int(rate * seconds) * channels, dtype=np.int16).tobytes())
    return path


class FakeBackend:
    def __init__(self, text: str = "hallo", delay: float = 0.0, fail: bool = False) -> None:
        self.text, self.delay, self.fail, self.calls = text, delay, fail, []

    async def start(self, stop_event):
        self.calls.append("start")

    async def warmup(self):
        if self.fail:
            raise RuntimeError("model missing")
        self.calls.append("warmup")

    async def transcribe(self, audio):
        await asyncio.sleep(self.delay)
        self.calls.append(len(audio))
        return self.text

    async def stop(self):
        self.calls.append("stop")


def test_measurement_divides_processing_time_by_audio_time(tmp_path):
    clips = [_clip(tmp_path, "a.wav", 2.0), _clip(tmp_path, "b.wav", 2.0)]
    backend = FakeBackend(delay=0.1)
    result = ss.measure_realtime_factor(backend, clips)
    assert result.audio_seconds == pytest.approx(4.0)
    assert result.rtf == pytest.approx(result.processing_seconds / 4.0)
    assert 0.04 < result.rtf < 0.2
    assert backend.calls == ["start", "warmup", 32000, 32000]


def test_measurement_failures_are_errors_never_a_fast_run(tmp_path):
    ok = [_clip(tmp_path, "a.wav", 1.0)]
    with pytest.raises(ss.SpeedCheckError, match="no text"):
        ss.measure_realtime_factor(FakeBackend(text="  "), ok)
    with pytest.raises(ss.SpeedCheckError, match="model missing"):
        ss.measure_realtime_factor(FakeBackend(fail=True), ok)
    with pytest.raises(ss.SpeedCheckError, match="16 kHz"):
        ss.measure_realtime_factor(FakeBackend(), [_clip(tmp_path, "b.wav", 1.0, 44100, 2)])


def test_shipped_clips_exist_and_are_short():
    paths = ss.clip_paths()
    audio = sum(len(ss._read_clip(p)) for p in paths) / ss.SAMPLE_RATE
    assert 5 < audio <= 15 and all(p.stat().st_size < 500_000 for p in paths)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("WHISPER_MODEL", ss.TOO_SLOW_KEY):
        monkeypatch.delenv(key, raising=False)


def _apply(tmp_path, monkeypatch, rtf, text="WHISPER_MODEL=large-v3-turbo\n", present=True, model=ss.DEFAULT_MODEL):
    monkeypatch.setattr(ss, "_ggml_present", lambda m: present)
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return ss.apply_choice(ss.decide(rtf, TH, model), env), env


def test_medium_writes_the_smaller_model_and_keeps_other_lines(tmp_path, monkeypatch):
    keys, env = _apply(tmp_path, monkeypatch, 0.7, "WHISPER_MODEL=large-v3-turbo\n# note\n")
    assert keys == ["setup_speed.medium"]
    assert read_env_value("WHISPER_MODEL", env_path=env) == "base"
    assert read_env_value(ss.TOO_SLOW_KEY, env_path=env) == "false"
    assert "# note" in env.read_text()


def test_hand_set_whisper_model_is_kept(tmp_path, monkeypatch):
    keys, env = _apply(tmp_path, monkeypatch, 0.7, "WHISPER_MODEL=large-v3\n", model="large-v3")
    assert keys == ["setup_speed.kept_by_hand"]
    assert read_env_value("WHISPER_MODEL", env_path=env) == "large-v3"


def test_medium_without_the_model_file_changes_nothing(tmp_path, monkeypatch):
    keys, env = _apply(tmp_path, monkeypatch, 0.7, present=False)
    assert keys == ["setup_speed.medium_missing"]
    assert read_env_value("WHISPER_MODEL", env_path=env) == "large-v3-turbo"


def test_too_slow_keeps_the_model_and_fast_clears_the_mark(tmp_path, monkeypatch):
    keys, env = _apply(tmp_path, monkeypatch, 2.0)
    assert keys == ["setup_speed.too_slow"]
    assert read_env_value("WHISPER_MODEL", env_path=env) == "large-v3-turbo"
    assert read_env_value(ss.TOO_SLOW_KEY, env_path=env) == "true"
    assert ss.apply_choice(ss.decide(0.1, TH), env) == ["setup_speed.fast"]
    assert read_env_value(ss.TOO_SLOW_KEY, env_path=env) == "false"


def test_wizard_step_reports_the_choice_or_the_failure(monkeypatch):
    def boom():
        raise ss.SpeedCheckError("no binary")

    monkeypatch.setattr(ss, "run_speed_check", boom)
    failed = detect_speed()
    assert failed.details["error"] == "speed_check_failed" and "no binary" in failed.message
    choice = ss.SpeedChoice("medium", "base", True)
    monkeypatch.setattr(ss, "run_speed_check", lambda: (choice, ss.SpeedMeasurement(0.7, 12.0, 8.4), "ok"))
    assert detect_speed().details["tier"] == "medium"


def _no_backend(_config):
    raise RuntimeError("whisper.cpp unavailable and no mlx fallback")


def test_missing_backend_gives_the_advisory_result_in_wizard_and_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sales_copilot.modules.transcriber.backends.create_backend", _no_backend)
    monkeypatch.setattr(ss, "clip_paths", lambda: [_clip(tmp_path, "a.wav", 1.0)])
    monkeypatch.setattr("sales_copilot.core.config.load_env", lambda: None)
    failed = detect_speed()
    assert failed.details["error"] == "speed_check_failed" and "unavailable" in failed.message
    assert ss.main(["--env-path", str(tmp_path / ".env")]) == 1
    err = capsys.readouterr().err
    assert "unavailable" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("mlx-whisper", "mlx-community/whisper-base-mlx"), ("whisper.cpp", None)],
)
def test_model_availability_follows_the_configured_backend(monkeypatch, tmp_path, backend, expected):
    monkeypatch.setenv("WHISPER_BACKEND", backend)
    seen = []

    def fake_mlx(model):
        seen.append(model)
        return True

    monkeypatch.setattr("sales_copilot.wizard.detectors._mlx_model_present", fake_mlx)
    monkeypatch.setattr(ss, "resolve_app_resource", lambda rel: tmp_path / rel)
    if expected is None:
        assert ss._ggml_present("base") is False and seen == []
        target = tmp_path / "vendor/whisper.cpp/models/ggml-base.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert ss._ggml_present("base") is True and ss._hint("base").startswith(GGML_DOWNLOAD_COMMAND)
    else:
        assert ss._ggml_present("base") is True and seen == ["base"]
        assert ss._hint("base") == f"huggingface-cli download {expected}"


def test_i18n_keys_exist_in_en_and_nl_with_parity():
    sections = {
        lang: yaml.safe_load((I18N / f"{lang}.yaml").read_text(encoding="utf-8"))["setup_speed"]
        for lang in ("en", "nl")
    }
    assert set(sections["en"]) == set(sections["nl"])
    assert {"fast", "medium", "medium_missing", "kept_by_hand", "too_slow", "failed"} <= set(sections["en"])
    for lang in ("en", "nl"):
        text = i18n.t("setup_speed.too_slow", lang=lang, rtf="1.20")
        assert "1.20" in text and "setup_speed" not in text
