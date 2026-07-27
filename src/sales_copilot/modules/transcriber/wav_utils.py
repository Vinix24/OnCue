from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np


def _to_pcm16(audio_array: np.ndarray) -> bytes:
    audio = np.asarray(audio_array, dtype=np.float32)
    if audio.ndim == 2:
        audio = audio.mean(axis=1, dtype=np.float32)
    elif audio.ndim != 1:
        raise ValueError("audio_array must be 1D mono or 2D multi-channel")

    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def encode_wav_bytes(audio_array: np.ndarray, sample_rate: int) -> bytes:
    """Encode mono WAV bytes from float32 samples in the [-1, 1] range."""

    pcm = _to_pcm16(audio_array)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def write_wav(path: str | Path, audio_array: np.ndarray, sample_rate: int) -> None:
    """Write mono WAV audio from float32 samples in the [-1, 1] range."""

    wav_path = Path(path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    wav_path.write_bytes(encode_wav_bytes(audio_array, sample_rate))
