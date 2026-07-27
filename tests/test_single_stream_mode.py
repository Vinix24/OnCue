import pytest

from sales_copilot.modules.transcriber import __main__ as transcriber_main


class _FakeStream:
    pass


def test_single_stream_all_prospect(monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert streams == [fake_mic]
    assert stream_speakers == ("prospect",)


def test_single_stream_default_speaker_invalid_falls_back_to_prospect() -> None:
    assert transcriber_main._single_stream_default_speaker("invalid") == "prospect"
