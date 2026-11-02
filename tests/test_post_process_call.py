"""Tests for scripts/post_process_call.py: the transcription step and the hand-off to the merge.

Hermetic: a generated WAV, a fake whisper-cli shell script, a fake diarizer. No model
download, no network, no real recordings.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from sales_copilot.modules.reports import diarize_merge
from sales_copilot.modules.reports.diarize_merge import DiarizationTurn
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend
from scripts import post_process_call as ppc

WHISPER_OUTPUT = (
    "[00:00:00.000 --> 00:00:04.000]   Goedemorgen, waarmee kan ik u helpen?\n"
    "[00:00:04.500 --> 00:00:09.250]   Ik zoek een offerte voor het CRM.\n"
)


def _write_wav(path: Path, seconds: int = 10, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * rate * seconds)
    return path


def _fake_whisper_cli(tmp_path: Path, *, stdout: str = WHISPER_OUTPUT, exit_code: int = 0) -> Path:
    payload = tmp_path / "whisper-output.txt"
    payload.write_text(stdout, encoding="utf-8")
    binary = tmp_path / "whisper-cli"
    binary.write_text(
        f"#!/bin/sh\ncat '{payload}'\n" + ("echo 'model exploded' >&2\n" if exit_code else "") + f"exit {exit_code}\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def whisper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the default (whisper.cpp) backend at a fake binary and run from a clean cwd."""

    def configure(binary: Path) -> None:
        model = tmp_path / "ggml-fake.bin"
        model.write_text("model", encoding="utf-8")
        monkeypatch.setenv("WHISPER_BACKEND", "whisper.cpp")
        monkeypatch.setenv("WHISPER_CPP_BINARY", str(binary))
        monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", str(model))
        monkeypatch.setenv("WHISPER_CPP_SERVER_ENABLED", "false")

    monkeypatch.chdir(tmp_path)
    return configure


def test_transcribe_to_segments_uses_default_backend_timestamps(tmp_path: Path, whisper_env) -> None:
    # Fails on the old code with: AttributeError: 'WhisperCppBackend' object has no
    # attribute 'transcribe_with_segments'.
    whisper_env(_fake_whisper_cli(tmp_path))
    wav = _write_wav(tmp_path / "call.wav")

    segments = ppc._transcribe_to_segments(wav)

    assert segments == [
        {"start": 0.0, "end": 4.0, "text": "Goedemorgen, waarmee kan ik u helpen?"},
        {"start": 4.5, "end": 9.25, "text": "Ik zoek een offerte voor het CRM."},
    ]


def test_segment_timestamps_reach_the_merge_so_two_speakers_get_different_text(
    tmp_path: Path, whisper_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    whisper_env(_fake_whisper_cli(tmp_path))
    wav = _write_wav(tmp_path / "call.wav")
    out = tmp_path / "labeled.txt"
    seen: dict[str, object] = {}

    def fake_diarize(path, **kwargs):
        seen["num_speakers"] = kwargs["num_speakers"]
        seen["model_id"] = kwargs["model_id"]
        return [DiarizationTurn(0.0, 4.2, "SPEAKER_00"), DiarizationTurn(4.2, 10.0, "SPEAKER_01")], "sherpa"

    monkeypatch.setattr(diarize_merge, "diarize_audio", fake_diarize)

    rc = ppc.main([str(wav), "--num-speakers", "2", "--model", "some/model", "--output", str(out)])

    assert rc == 0
    assert seen == {"num_speakers": 2, "model_id": "some/model"}
    assert out.read_text(encoding="utf-8").splitlines() == [
        "SPEAKER_00: Goedemorgen, waarmee kan ik u helpen?",
        "SPEAKER_01: Ik zoek een offerte voor het CRM.",
    ]


def test_backend_without_segment_support_fails_with_a_clear_error(
    tmp_path: Path, whisper_env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    whisper_env(_fake_whisper_cli(tmp_path))
    wav = _write_wav(tmp_path / "call.wav")

    class TextOnlyBackend:
        async def stop(self) -> None:
            return None

        async def transcribe(self, audio) -> str:
            return "alles in een tekst"

    import sales_copilot.modules.transcriber.backends as backends

    monkeypatch.setattr(backends, "create_backend", lambda _cfg: TextOnlyBackend())
    called: list[bool] = []
    monkeypatch.setattr(diarize_merge, "diarize_audio", lambda *a, **k: called.append(True))

    rc = ppc.main([str(wav), "--output", str(tmp_path / "out.txt")])

    assert rc == 1
    assert "cannot produce timestamped segments" in capsys.readouterr().err
    assert not called
    assert not (tmp_path / "out.txt").exists()


def test_whisper_failure_is_an_error_not_a_success(
    tmp_path: Path, whisper_env, capsys: pytest.CaptureFixture[str]
) -> None:
    whisper_env(_fake_whisper_cli(tmp_path, stdout="", exit_code=2))
    wav = _write_wav(tmp_path / "call.wav")

    rc = ppc.main([str(wav), "--output", str(tmp_path / "out.txt")])

    assert rc == 1
    assert "model exploded" in capsys.readouterr().err
    assert not (tmp_path / "out.txt").exists()


def test_silent_recording_without_segments_is_an_error(
    tmp_path: Path, whisper_env, capsys: pytest.CaptureFixture[str]
) -> None:
    whisper_env(_fake_whisper_cli(tmp_path, stdout="whisper_print_timings: total time = 1 ms\n"))
    wav = _write_wav(tmp_path / "call.wav")

    rc = ppc.main([str(wav), "--output", str(tmp_path / "out.txt")])

    assert rc == 1
    assert "no timestamped segments" in capsys.readouterr().err


def test_transcript_json_route_still_skips_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = _write_wav(tmp_path / "call.wav")
    transcript = tmp_path / "t.json"
    transcript.write_text(
        json.dumps({"segments": [{"start": 0, "end": 3, "text": "hallo"}, {"start": 3, "end": 6, "text": "dag"}]}),
        encoding="utf-8",
    )
    out = tmp_path / "labeled.txt"
    monkeypatch.setattr(
        ppc, "_transcribe_to_segments", lambda _wav: pytest.fail("transcription must be skipped")
    )
    monkeypatch.setattr(
        diarize_merge,
        "diarize_audio",
        lambda *a, **k: ([DiarizationTurn(0.0, 3.0, "SPEAKER_00"), DiarizationTurn(3.0, 6.0, "SPEAKER_01")], "sherpa"),
    )

    rc = ppc.main([str(wav), "--transcript-json", str(transcript), "--output", str(out)])

    assert rc == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["SPEAKER_00: hallo", "SPEAKER_01: dag"]


def test_parse_stdout_segments_handles_hours_and_skips_noise() -> None:
    stdout = (
        "whisper_init: loading model\n"
        "[01:02:03.500 --> 01:02:05.000]  later in the call\n"
        "[01:02:05.000 --> 01:02:06.000]   \n"
        "not a segment line\n"
    )

    assert WhisperCppBackend._parse_stdout_segments(stdout) == [  # noqa: SLF001
        {"start": 3723.5, "end": 3725.0, "text": "later in the call"}
    ]
