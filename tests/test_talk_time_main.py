import pytest

from sales_copilot.modules.talk_time import __main__ as talk_time_main


def test_load_audio_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "AUDIO_CAPTURE_METHOD",
        "AUDIO_SAMPLE_RATE",
        "AUDIO_CHANNELS",
        "AUDIOTEE_BINARY_PATH",
        "TARGET_PROCESS_NAME",
    ]:
        monkeypatch.delenv(key, raising=False)

    config = talk_time_main._load_audio_config()

    assert config.capture_method == "audiotee"
    assert config.sample_rate == 16000
    assert config.channels == 1
    assert config.audiotee_path == "./bin/audiotee"
    assert config.target_process is None


def test_load_audio_config_invalid_method(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIO_CAPTURE_METHOD", "loopback")

    with pytest.raises(ValueError):
        talk_time_main._load_audio_config()


def test_load_audio_config_accepts_blackhole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIO_CAPTURE_METHOD", "blackhole")

    config = talk_time_main._load_audio_config()

    assert config.capture_method == "blackhole"
