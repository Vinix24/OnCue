import wave

import numpy as np

from sales_copilot.modules.transcriber.wav_utils import write_wav


def test_write_wav_writes_mono_16khz_pcm(tmp_path) -> None:
    wav_path = tmp_path / "chunk.wav"
    audio = np.array([[0.25, -0.25], [0.5, -0.5], [0.0, 0.0]], dtype=np.float32)

    write_wav(wav_path, audio, sample_rate=16000)

    with wave.open(str(wav_path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 3


def test_write_wav_rejects_invalid_dimensions(tmp_path) -> None:
    wav_path = tmp_path / "chunk.wav"
    audio = np.zeros((1, 2, 3), dtype=np.float32)

    try:
        write_wav(wav_path, audio, sample_rate=16000)
    except ValueError as exc:
        assert "audio_array must be 1D mono or 2D multi-channel" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid audio dimensions")
