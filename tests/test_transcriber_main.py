import pytest

from sales_copilot.modules.transcriber import __main__ as transcriber_main


class _FakeStream:
    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def read(self):
        return None


def test_hub_available_returns_false_on_error(monkeypatch) -> None:
    def _fail(*_args, **_kwargs):
        raise OSError("nope")

    monkeypatch.setattr(transcriber_main.socket, "create_connection", _fail)

    assert transcriber_main._hub_available("127.0.0.1", 9999) is False


def test_load_audio_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "AUDIO_CAPTURE_METHOD",
        "AUDIO_SAMPLE_RATE",
        "AUDIO_CHANNELS",
        "AUDIOTEE_BINARY_PATH",
        "TARGET_PROCESS_NAME",
    ]:
        monkeypatch.delenv(key, raising=False)

    config = transcriber_main._load_audio_config()

    assert config.capture_method == "audiotee"
    assert config.sample_rate == 16000
    assert config.channels == 1
    assert config.wasapi_endpoint_name is None


def test_load_audio_config_reads_wasapi_endpoint_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIO_WASAPI_ENDPOINT_NAME", "Realtek USB Headset")

    config = transcriber_main._load_audio_config()

    assert config.wasapi_endpoint_name == "Realtek USB Headset"


def test_load_transcription_engine_defaults_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSCRIPTION_ENGINE", raising=False)
    assert transcriber_main._load_transcription_engine() == "direct"

    monkeypatch.setenv("TRANSCRIPTION_ENGINE", "whisper.cpp")
    assert transcriber_main._load_transcription_engine() == "whisper.cpp"

    monkeypatch.setenv("TRANSCRIPTION_ENGINE", "wlk")
    with pytest.raises(ValueError):
        transcriber_main._load_transcription_engine()


def test_build_audio_streams_uses_dual_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    audio_config = transcriber_main.AudioConfig(capture_method="blackhole")
    fake_mic = _FakeStream()
    fake_system = _FakeStream()

    monkeypatch.setattr(
        transcriber_main.DualAudioCapture,
        "create",
        lambda self, *, on_warning=None: (fake_mic, fake_system),
    )
    monkeypatch.setattr(
        transcriber_main,
        "_use_blackhole_single_stream_fallback",
        lambda _cfg: False,
    )

    streams, stream_speakers = transcriber_main._build_audio_streams(audio_config, "prospect")

    assert stream_speakers == ("self", "prospect")
    assert len(streams) == 2
    assert streams[0] is fake_mic
    assert streams[1] is fake_system


def test_build_audio_streams_uses_single_stream_diarization_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_config = transcriber_main.AudioConfig(capture_method="blackhole")
    fake_mic = _FakeStream()
    fake_system = _FakeStream()

    monkeypatch.setattr(
        transcriber_main.DualAudioCapture,
        "create",
        lambda self, *, on_warning=None: (fake_mic, fake_system),
    )
    monkeypatch.setattr(
        transcriber_main,
        "_use_blackhole_single_stream_fallback",
        lambda _cfg: True,
    )

    streams, stream_speakers = transcriber_main._build_audio_streams(audio_config, "prospect")

    assert stream_speakers == ("prospect",)
    assert len(streams) == 1
    assert streams[0] is fake_mic


def test_backend_config_for_whisper_cpp() -> None:
    config = transcriber_main.TranscriberConfig(
        language="en",
        whisper_cpp_binary="/tmp/whisper-cli",
        whisper_cpp_model_path="/tmp/model.gguf",
        whisper_cpp_threads=6,
    )

    backend_cfg = transcriber_main._backend_config("whisper.cpp", config)

    assert backend_cfg["backend"] == "whisper.cpp"
    assert backend_cfg["language"] == "en"
    assert backend_cfg["whisper_cpp_binary"] == "/tmp/whisper-cli"
