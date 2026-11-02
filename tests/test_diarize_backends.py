"""Backend selection for the after-the-call diarization: token detection, the ``auto`` rule,
the sherpa path with a fake sherpa-onnx module, and the eval script's use of the shared module.

No network, no real models.
"""

from __future__ import annotations

import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from sales_copilot.modules.reports import diarize_merge, diarize_sherpa
from sales_copilot.modules.reports.diarize_merge import (
    DiarizationTurn,
    diarize_audio,
    find_hf_token,
    resolve_backend,
)

_TOKEN_ENV = ("HUGGINGFACE_TOKEN", "HF_TOKEN", "HF_TOKEN_PATH", "HF_HOME")


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-empty"))


# ------------------------------------------------------------------ token detection


def test_token_from_argument_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "from-env")
    assert find_hf_token("  from-arg ") == "from-arg"


def test_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUGGINGFACE_TOKEN", "from-env")
    assert find_hf_token() == "from-env"


def test_token_from_cached_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "hf"
    home.mkdir()
    (home / "token").write_text("cached-token\n", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(home))
    assert find_hf_token() == "cached-token"


def test_token_path_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "elsewhere"
    path.write_text("custom\n", encoding="utf-8")
    monkeypatch.setenv("HF_TOKEN_PATH", str(path))
    assert find_hf_token() == "custom"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_token_counts_as_none(monkeypatch: pytest.MonkeyPatch, blank: str | None) -> None:
    if blank is not None:
        monkeypatch.setenv("HUGGINGFACE_TOKEN", blank)
    assert find_hf_token(blank) is None


def test_empty_cached_file_is_no_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "hf"
    home.mkdir()
    (home / "token").write_text("\n", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(home))
    assert find_hf_token() is None


# ------------------------------------------------------------------ the auto table


@pytest.mark.parametrize(
    ("backend", "num_speakers", "has_token", "expected", "warns"),
    [
        ("auto", None, False, "sherpa", True),
        ("auto", None, True, "pyannote", False),
        ("auto", 1, True, "sherpa", False),
        ("auto", 2, False, "sherpa", False),
        ("auto", 2, True, "sherpa", False),
        ("auto", 3, True, "pyannote", False),
        ("auto", 7, True, "pyannote", False),
        ("auto", 3, False, "sherpa", True),
        ("auto", 6, False, "sherpa", True),
        ("sherpa", 6, True, "sherpa", False),
        ("sherpa", None, False, "sherpa", False),
        ("pyannote", None, False, "pyannote", False),
        ("pyannote", 2, True, "pyannote", False),
    ],
)
def test_resolve_backend_table(
    backend: str, num_speakers: int | None, has_token: bool, expected: str, warns: bool
) -> None:
    chosen, warning = resolve_backend(backend, num_speakers, "tok" if has_token else None)  # type: ignore[arg-type]
    assert chosen == expected
    assert bool(warning) is warns
    if warns:
        assert "one label" in warning.lower()
        assert "token" in warning.lower()


def test_resolve_backend_unknown_count_warning_names_the_remedies() -> None:
    _, warning = resolve_backend("auto", None, None)
    assert warning is not None
    assert "unknown" in warning
    assert "--num-speakers 2" in warning


def test_resolve_backend_sees_cached_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "hf"
    home.mkdir()
    (home / "token").write_text("cached\n", encoding="utf-8")
    monkeypatch.setenv("HF_HOME", str(home))
    assert resolve_backend("auto", 4, None) == ("pyannote", None)


def test_resolve_backend_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        resolve_backend("whisperx", None, None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the sherpa path with a fake sherpa-onnx


class _FakeResult(list):
    def sort_by_start_time(self) -> _FakeResult:
        return _FakeResult(sorted(self, key=lambda r: r.start))


def _install_fake_sherpa(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """A stand-in ``sherpa_onnx`` module that records its config and returns two fixed turns."""
    seen: dict = {}

    class _Cfg:
        def __init__(self, **kw: object) -> None:
            self.kw = kw

        def validate(self) -> bool:
            return True

    class _Seg:
        def __init__(self, start: float, end: float, speaker: int) -> None:
            self.start, self.end, self.speaker = start, end, speaker

    class _Diar:
        sample_rate = 16000

        def __init__(self, config: _Cfg) -> None:
            seen["config"] = config.kw

        def process(self, audio: np.ndarray) -> _FakeResult:
            seen["samples"] = len(audio)
            return _FakeResult([_Seg(2.0, 3.0, 1), _Seg(0.0, 1.5, 0)])

    class _Top(_Cfg):
        pass

    fake = types.ModuleType("sherpa_onnx")
    fake.OfflineSpeakerDiarizationConfig = _Top
    fake.OfflineSpeakerSegmentationModelConfig = _Cfg
    fake.OfflineSpeakerSegmentationPyannoteModelConfig = _Cfg
    fake.SpeakerEmbeddingExtractorConfig = _Cfg
    fake.FastClusteringConfig = _Cfg
    fake.OfflineSpeakerDiarization = _Diar
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(
        diarize_sherpa, "ensure_sherpa_models", lambda cache_dir, emb: (cache_dir / "seg.onnx", cache_dir / "emb.onnx")
    )
    monkeypatch.setattr(diarize_sherpa, "default_cache_dir", lambda: tmp_path / "models")
    return seen


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000, channels: int = 1) -> None:
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes((np.zeros(n * channels, dtype=np.int16)).tobytes())


def test_sherpa_path_clusters_by_threshold_and_ignores_num_speakers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen = _install_fake_sherpa(monkeypatch, tmp_path)
    wav = tmp_path / "call.wav"
    _write_wav(wav, seconds=2.0)

    with caplog.at_level("INFO", logger=diarize_merge.logger.name):
        turns, used = diarize_audio(wav, backend="sherpa", num_speakers=2, cluster_threshold=0.65)

    assert used == "sherpa"
    assert turns == [
        DiarizationTurn(0.0, 1.5, "SPEAKER_00"),
        DiarizationTurn(2.0, 3.0, "SPEAKER_01"),
    ]
    clustering = seen["config"]["clustering"].kw
    assert clustering == {"num_clusters": -1, "threshold": 0.65}  # count not forced
    assert seen["samples"] == 32000
    ignored = [r for r in caplog.records if "ignored for sherpa" in r.getMessage()]
    assert len(ignored) == 1


def test_sherpa_path_default_threshold_and_no_ignore_line_without_num_speakers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen = _install_fake_sherpa(monkeypatch, tmp_path)
    wav = tmp_path / "call.wav"
    _write_wav(wav)
    with caplog.at_level("INFO", logger=diarize_merge.logger.name):
        diarize_audio(wav, backend="sherpa")
    assert seen["config"]["clustering"].kw["threshold"] == 0.8
    assert not [r for r in caplog.records if "ignored for sherpa" in r.getMessage()]


def test_sherpa_path_does_not_import_torch_or_pyannote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_sherpa(monkeypatch, tmp_path)
    for name in [m for m in sys.modules if m == "torch" or m.startswith(("torch.", "pyannote"))]:
        monkeypatch.delitem(sys.modules, name)
    wav = tmp_path / "call.wav"
    _write_wav(wav)
    diarize_audio(wav, backend="sherpa")
    assert "torch" not in sys.modules
    assert "pyannote.audio" not in sys.modules


def test_auto_with_group_and_token_calls_pyannote(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_diarize_wav(wav_path: object, **kwargs: object) -> list[DiarizationTurn]:
        calls.append(kwargs)
        return [DiarizationTurn(0.0, 1.0, "SPEAKER_00")]

    monkeypatch.setattr(diarize_merge, "diarize_wav", fake_diarize_wav)
    turns, used = diarize_audio(tmp_path / "x.wav", backend="auto", hf_token="tok", num_speakers=5)
    assert used == "pyannote"
    assert calls == [
        {"hf_token": "tok", "model_id": "pyannote/speaker-diarization-3.1", "num_speakers": 5, "device": None}
    ]
    assert turns[0].speaker == "SPEAKER_00"


def test_auto_group_without_token_warns_and_uses_sherpa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fake_sherpa(monkeypatch, tmp_path)
    wav = tmp_path / "call.wav"
    _write_wav(wav)
    with caplog.at_level("WARNING", logger=diarize_merge.logger.name):
        _, used = diarize_audio(wav, backend="auto", num_speakers=6)
    assert used == "sherpa"
    assert any("one label" in r.getMessage() for r in caplog.records)


def test_unknown_sherpa_embedding_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown sherpa embedding"):
        diarize_sherpa.SherpaBackend("nope", False, "cpu", tmp_path)


def test_sherpa_backend_refuses_missing_process_before_prepare(tmp_path: Path) -> None:
    backend = diarize_sherpa.SherpaBackend("wespeaker", False, "cpu", tmp_path)
    with pytest.raises(AssertionError):
        backend.process(np.zeros(16000, dtype=np.float32), 16000)


# ------------------------------------------------------------------ audio loading


def test_load_audio_downmixes_stereo_16k(tmp_path: Path) -> None:
    wav = tmp_path / "s.wav"
    _write_wav(wav, seconds=0.5, channels=2)
    audio = diarize_sherpa.load_audio_16k_mono(wav)
    assert audio.shape == (8000,)
    assert audio.dtype == np.float32


def test_load_audio_unreadable_file_without_ffmpeg_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")

    def no_ffmpeg(*_a: object, **_k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(diarize_sherpa.subprocess, "run", no_ffmpeg)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        diarize_sherpa.load_audio_16k_mono(bad)


# ------------------------------------------------------------------ the eval script uses the shared module


def test_eval_script_builds_sherpa_backends_from_the_shared_module(tmp_path: Path) -> None:
    from scripts import eval_diarization as ed

    backends = ed.build_backends("sherpa:wespeaker,sherpa:eres2net@oracle", ["cpu"], tmp_path, 0.5, 2)
    assert all(isinstance(b, diarize_sherpa.SherpaBackend) for b in backends)
    assert [b.name for b in backends] == ["sherpa:wespeaker", "sherpa:eres2net@oracle"]
    assert ed.default_cache_dir is diarize_sherpa.default_cache_dir
    assert not hasattr(ed, "ensure_sherpa_models")  # one copy only, in the product module


def test_eval_sherpa_backend_returns_seg_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from scripts import eval_diarization as ed

    _install_fake_sherpa(monkeypatch, tmp_path)
    backend = ed.SherpaBackend("wespeaker", True, "cpu", tmp_path, 0.5, 1)
    backend.prepare(2)
    segs = backend.diarize(np.zeros(16000, dtype=np.float32), 16000)
    assert segs == [ed.Seg(0.0, 1.5, "SPEAKER_00"), ed.Seg(2.0, 3.0, "SPEAKER_01")]


# ------------------------------------------------------------------ the script's CLI


def test_post_process_script_reports_the_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from scripts import post_process_call as ppc

    wav = tmp_path / "call.wav"
    _write_wav(wav)
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps({"segments": [{"start": 0.0, "end": 1.0, "text": "hallo"}]}), encoding="utf-8")
    seen: dict = {}

    def fake_diarize_audio(path: object, **kwargs: object) -> tuple[list[DiarizationTurn], str]:
        seen.update(kwargs)
        return [DiarizationTurn(0.0, 1.0, "SPEAKER_00")], "sherpa"

    monkeypatch.setattr(diarize_merge, "diarize_audio", fake_diarize_audio)
    out = tmp_path / "out.txt"
    rc = ppc.main([str(wav), "--transcript-json", str(transcript), "-o", str(out), "--cluster-threshold", "0.7"])
    assert rc == 0
    assert seen["backend"] == "auto"
    assert seen["cluster_threshold"] == 0.7
    assert out.read_text(encoding="utf-8").strip() == "SPEAKER_00: hallo"
    assert "backend: sherpa" in capsys.readouterr().err


def test_post_process_script_rejects_unknown_backend(tmp_path: Path) -> None:
    from scripts import post_process_call as ppc

    with pytest.raises(SystemExit):
        ppc.main([str(tmp_path / "x.wav"), "--backend", "nope"])
