"""Tests for scripts/spark_benchmark.py.

Every test here is fully mocked/synthetic -- no live whisper.cpp binary, no `say` TTS
invocation, no LLM network call, no `data/sessions/` fixtures. That mirrors the script's
own constraint (mock audio only) and the dispatch requirement of a mocked whisper backend
so CI never depends on a live model.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import spark_benchmark as sb  # noqa: E402

# ---------------------------------------------------------------------------
# Bundled mock sentences
# ---------------------------------------------------------------------------


def test_mock_sentences_are_a_representative_bundle() -> None:
    assert 10 <= len(sb._MOCK_SENTENCES) <= 15
    assert all(isinstance(s, str) and s.strip() for s in sb._MOCK_SENTENCES)
    assert len(set(sb._MOCK_SENTENCES)) == len(sb._MOCK_SENTENCES)


# ---------------------------------------------------------------------------
# Mock audio generation -- synthetic-tone fallback path (no TTS engine involved)
# ---------------------------------------------------------------------------


def test_generate_synthetic_tone_writes_valid_wav(tmp_path: Path) -> None:
    out_path = tmp_path / "tone.wav"
    sb._generate_synthetic_tone(0, out_path)

    audio = sb._read_wav_float32(out_path)
    assert audio.size > 0
    assert np.abs(audio).max() <= 1.0
    # index 0 -> duration_s = 1.5 (see _generate_synthetic_tone)
    assert audio.size == pytest.approx(1.5 * sb._SAMPLE_RATE, rel=0.01)


def test_generate_synthetic_tone_duration_varies_by_index(tmp_path: Path) -> None:
    durations = []
    for i in range(5):
        out_path = tmp_path / f"tone_{i}.wav"
        sb._generate_synthetic_tone(i, out_path)
        durations.append(sb._read_wav_float32(out_path).size / sb._SAMPLE_RATE)

    assert len(set(durations)) > 1
    assert all(1.0 <= d <= 4.0 for d in durations)


def test_generate_mock_audio_synthetic_fallback(tmp_path: Path) -> None:
    sentences = sb._MOCK_SENTENCES[:3]
    paths, source = sb.generate_mock_audio(tmp_path, sentences=sentences, use_tts=False, use_bundled=False)

    assert len(paths) == 3
    assert all(p.exists() for p in paths)
    assert "synthetic" in source
    for p in paths:
        audio = sb._read_wav_float32(p)
        assert audio.size > 0


def test_generate_mock_audio_never_invokes_say_when_use_tts_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_be_called(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("say should never be invoked when use_tts=False")

    monkeypatch.setattr(sb, "_generate_via_say", _must_not_be_called)

    paths, _source = sb.generate_mock_audio(
        tmp_path, sentences=sb._MOCK_SENTENCES[:2], use_tts=False, use_bundled=False
    )

    assert len(paths) == 2


def test_generate_mock_audio_never_reads_real_session_data() -> None:
    # No data/sessions/ path appears anywhere in the mock-audio generation source --
    # a static guard against accidentally wiring real fixtures into this script.
    import inspect

    source = (
        inspect.getsource(sb.generate_mock_audio)
        + inspect.getsource(sb._generate_synthetic_tone)
        + inspect.getsource(sb._regenerate_bundled_assets)
    )
    assert "data/sessions" not in source


# ---------------------------------------------------------------------------
# Mock audio generation -- "prefer bundled WAVs" selection logic
# ---------------------------------------------------------------------------


def _write_fake_bundled_wav(bundled_dir: Path, index: int, *, n_samples: int = 800) -> Path:
    """Write a tiny-but-valid WAV at the bundled path for ``index`` (fully synthetic, no `say`)."""
    path = sb._bundled_wav_path(index, bundled_dir)
    bundled_dir.mkdir(parents=True, exist_ok=True)
    audio = np.zeros(n_samples, dtype=np.float32)
    sb.write_wav(path, audio, sb._SAMPLE_RATE)
    return path


def test_generate_mock_audio_prefers_bundled_wav_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"
    out_dir = tmp_path / "out"
    _write_fake_bundled_wav(bundled_dir, 0)
    _write_fake_bundled_wav(bundled_dir, 1)

    def _must_not_be_called(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("say/tone generation should not run when a bundled WAV exists")

    monkeypatch.setattr(sb, "_generate_via_say", _must_not_be_called)
    monkeypatch.setattr(sb, "_generate_synthetic_tone", _must_not_be_called)
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)  # would prefer TTS if bundled lost

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:2], use_tts=True, use_bundled=True, bundled_dir=bundled_dir
    )

    assert len(paths) == 2
    assert all(p.exists() for p in paths)
    assert "bundled" in source
    assert "synthetic" not in source
    # Copied into out_dir, not read in place from bundled_dir.
    assert all(p.parent == out_dir for p in paths)


def test_generate_mock_audio_falls_back_to_say_when_bundled_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"  # never created -- no bundled WAVs at all
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)

    def _fake_say(_sentence: str, path: Path, _voice: str) -> bool:
        sb._generate_synthetic_tone(0, path)
        return True

    monkeypatch.setattr(sb, "_generate_via_say", _fake_say)

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:2], use_tts=True, use_bundled=True, bundled_dir=bundled_dir
    )

    assert len(paths) == 2
    assert "say" in source
    assert "bundled" not in source


def test_generate_mock_audio_falls_back_to_tone_when_neither_bundled_nor_tts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"  # never created
    out_dir = tmp_path / "out"
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: False)

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:2], use_tts=True, use_bundled=True, bundled_dir=bundled_dir
    )

    assert len(paths) == 2
    assert "synthetic" in source
    assert "bundled" not in source


def test_generate_mock_audio_ignores_bundled_file_that_is_just_a_bare_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"
    out_dir = tmp_path / "out"
    bundled_dir.mkdir(parents=True, exist_ok=True)
    bad_path = sb._bundled_wav_path(0, bundled_dir)
    bad_path.write_bytes(b"\x00" * 44)  # bare WAV header, no audio -- must not be trusted
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: False)

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:1], use_tts=True, use_bundled=True, bundled_dir=bundled_dir
    )

    assert len(paths) == 1
    assert "synthetic" in source


def test_generate_mock_audio_use_bundled_false_skips_bundled_even_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"
    out_dir = tmp_path / "out"
    _write_fake_bundled_wav(bundled_dir, 0)
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: False)

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:1], use_tts=True, use_bundled=False, bundled_dir=bundled_dir
    )

    assert len(paths) == 1
    assert "synthetic" in source
    assert "bundled" not in source


def test_generate_mock_audio_partial_bundled_coverage_reports_mixed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled_dir = tmp_path / "bundled"
    out_dir = tmp_path / "out"
    _write_fake_bundled_wav(bundled_dir, 0)  # only sentence 0 has a bundled WAV
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: False)  # sentence 1 -> synthetic tone

    paths, source = sb.generate_mock_audio(
        out_dir, sentences=sb._MOCK_SENTENCES[:2], use_tts=True, use_bundled=True, bundled_dir=bundled_dir
    )

    assert len(paths) == 2
    assert "mixed" in source
    assert "bundled" in source
    assert "synthetic" in source


def test_bundled_wav_path_uses_zero_padded_index(tmp_path: Path) -> None:
    assert sb._bundled_wav_path(3, tmp_path) == tmp_path / "mock_03.wav"


# ---------------------------------------------------------------------------
# Checked-in bundled assets -- static filesystem check, no `say`/live model needed
# ---------------------------------------------------------------------------


def test_bundled_assets_directory_has_representative_wavs_and_readme() -> None:
    assert sb._BUNDLED_AUDIO_DIR.is_dir()
    readme = sb._BUNDLED_AUDIO_DIR / "README.md"
    assert readme.is_file()

    wavs = sorted(sb._BUNDLED_AUDIO_DIR.glob("mock_*.wav"))
    assert 6 <= len(wavs) <= 10
    for i, wav in enumerate(wavs):
        assert wav.name == f"mock_{i:02d}.wav"  # contiguous, zero-indexed
        assert wav.stat().st_size > 44  # more than a bare WAV header
        audio = sb._read_wav_float32(wav)
        assert audio.size > 0


def test_bundled_assets_count_matches_n_bundled_sentences_constant() -> None:
    wavs = list(sb._BUNDLED_AUDIO_DIR.glob("mock_*.wav"))
    assert len(wavs) == sb._N_BUNDLED_SENTENCES


# ---------------------------------------------------------------------------
# Regenerate-bundled-assets CLI utility -- mocked `say`, never invoked for real here
# ---------------------------------------------------------------------------


def test_regenerate_bundled_assets_fails_closed_without_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: False)

    exit_code = sb._regenerate_bundled_assets()

    assert exit_code == 1


def test_regenerate_bundled_assets_writes_wav_per_sentence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)

    def _fake_say(_sentence: str, out_path: Path, _voice: str) -> bool:
        sb._generate_synthetic_tone(0, out_path)
        return True

    monkeypatch.setattr(sb, "_generate_via_say", _fake_say)
    out_dir = tmp_path / "regen"

    exit_code = sb._regenerate_bundled_assets(sentences=sb._MOCK_SENTENCES[:3], bundled_dir=out_dir)

    assert exit_code == 0
    wavs = sorted(out_dir.glob("mock_*.wav"))
    assert len(wavs) == 3


def test_regenerate_bundled_assets_fails_closed_on_say_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)
    monkeypatch.setattr(sb, "_generate_via_say", lambda *_a, **_k: False)
    out_dir = tmp_path / "regen"

    exit_code = sb._regenerate_bundled_assets(sentences=sb._MOCK_SENTENCES[:2], bundled_dir=out_dir)

    assert exit_code == 1


# ---------------------------------------------------------------------------
# WAV round-trip
# ---------------------------------------------------------------------------


def test_read_wav_float32_round_trips_write_wav(tmp_path: Path) -> None:
    original = (np.sin(np.linspace(0, 10, 1600)) * 0.5).astype(np.float32)
    path = tmp_path / "roundtrip.wav"
    sb.write_wav(path, original, sb._SAMPLE_RATE)

    read_back = sb._read_wav_float32(path)

    assert read_back.size == original.size
    assert np.abs(read_back - original).max() < 1e-3  # int16 quantization tolerance


def test_read_wav_float32_rejects_non_16bit(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "bad.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)  # 8-bit, unsupported
        handle.setframerate(sb._SAMPLE_RATE)
        handle.writeframes(b"\x00" * 100)

    with pytest.raises(ValueError, match="16-bit PCM"):
        sb._read_wav_float32(path)


# ---------------------------------------------------------------------------
# TTS availability / invocation -- fully mocked, no real `say` call
# ---------------------------------------------------------------------------


def test_tts_available_false_when_say_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: None)
    assert sb._tts_available("Xander") is False


def test_tts_available_true_when_voice_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/say")
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Xander              nl_NL    # Hallo\n", stderr=""
    )
    monkeypatch.setattr(sb.subprocess, "run", lambda *_a, **_k: fake_result)

    assert sb._tts_available("Xander") is True


def test_tts_available_false_when_voice_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/say")
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Alex                en_US    # Hi\n", stderr=""
    )
    monkeypatch.setattr(sb.subprocess, "run", lambda *_a, **_k: fake_result)

    assert sb._tts_available("Xander") is False


def test_generate_via_say_false_when_binary_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: None)
    assert sb._generate_via_say("hallo", tmp_path / "out.wav", "Xander") is False


def test_generate_via_say_false_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: "/usr/bin/say")

    def _raise(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, "say")

    monkeypatch.setattr(sb.subprocess, "run", _raise)

    assert sb._generate_via_say("hallo", tmp_path / "out.wav", "Xander") is False


def test_generate_mock_audio_falls_back_when_say_reports_success_but_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`say` "succeeding" without producing a usable file must still fall back."""
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)
    monkeypatch.setattr(sb, "_generate_via_say", lambda *_a, **_k: False)

    paths, source = sb.generate_mock_audio(
        tmp_path, sentences=sb._MOCK_SENTENCES[:2], use_tts=True, use_bundled=False
    )

    assert len(paths) == 2
    assert all(p.exists() for p in paths)
    assert "synthetic" in source


# ---------------------------------------------------------------------------
# Hardware fingerprint -- fully mocked, no real nvidia-smi/system_profiler dependency
# ---------------------------------------------------------------------------


def test_detect_gpu_uses_nvidia_smi_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    fake_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="NVIDIA GB10, 128000 MiB, 550.54.14\n", stderr=""
    )
    monkeypatch.setattr(sb.subprocess, "run", lambda *_a, **_k: fake_result)

    assert "NVIDIA GB10" in sb._detect_gpu()


def test_detect_gpu_falls_back_to_unknown_without_nvidia_smi_or_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb.shutil, "which", lambda _name: None)
    monkeypatch.setattr(sb.platform, "system", lambda: "Linux")

    assert sb._detect_gpu().startswith("unknown")


def test_detect_hardware_returns_populated_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "_detect_gpu", lambda: "fake-gpu")

    hw = sb.detect_hardware()

    assert hw.gpu == "fake-gpu"
    assert hw.python_version
    assert hw.platform
    assert hw.machine


# ---------------------------------------------------------------------------
# Whisper latency -- mocked TranscriptionBackend, no live model
# ---------------------------------------------------------------------------


class _StubWhisperBackend:
    """Fake ``TranscriptionBackend`` -- implements the protocol, no real whisper.cpp."""

    def __init__(self, *, fail_indices: set[int] | None = None, server_started: bool | None = True) -> None:
        self._fail_indices = fail_indices or set()
        self._server_started = server_started
        self.warmup_called = False
        self.stop_called = False
        self._n = 0

    async def start(self, _stop_event: asyncio.Event) -> None:
        return None

    async def warmup(self) -> None:
        self.warmup_called = True

    async def transcribe(self, _audio: np.ndarray) -> str:
        idx = self._n
        self._n += 1
        if idx in self._fail_indices:
            raise RuntimeError("simulated whisper failure")
        return f"mock transcript {idx}"

    async def transcribe_file(self, _path: Path) -> str:
        return ""

    async def stop(self) -> None:
        self.stop_called = True


def _write_mock_wavs(tmp_path: Path, n: int) -> list[Path]:
    paths = []
    for i in range(n):
        p = tmp_path / f"mock_{i:02d}.wav"
        sb._generate_synthetic_tone(i, p)
        paths.append(p)
    return paths


def test_measure_whisper_latency_uses_mocked_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wav_paths = _write_mock_wavs(tmp_path, 3)
    stub = _StubWhisperBackend()
    monkeypatch.setattr(sb, "create_backend", lambda _cfg: stub)

    results, server_mode = asyncio.run(sb.measure_whisper_latency(wav_paths, sb.TranscriberConfig()))

    assert len(results) == 3
    assert all(r.error is None for r in results)
    assert all(r.latency_ms is not None and r.latency_ms >= 0 for r in results)
    assert results[0].text == "mock transcript 0"
    assert "resident whisper-server" in server_mode
    assert stub.warmup_called
    assert stub.stop_called


def test_measure_whisper_latency_one_failing_segment_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav_paths = _write_mock_wavs(tmp_path, 3)
    stub = _StubWhisperBackend(fail_indices={1})
    monkeypatch.setattr(sb, "create_backend", lambda _cfg: stub)

    results, _server_mode = asyncio.run(sb.measure_whisper_latency(wav_paths, sb.TranscriberConfig()))

    assert results[0].error is None
    assert results[1].error is not None and "simulated whisper failure" in results[1].error
    assert results[2].error is None


def test_server_mode_label_resident_server() -> None:
    label = sb._server_mode_label(_StubWhisperBackend(server_started=True))
    assert "resident whisper-server" in label


def test_server_mode_label_one_shot_cli_flags_unfair_comparison() -> None:
    label = sb._server_mode_label(_StubWhisperBackend(server_started=False))
    assert "whisper-cli" in label
    assert "NOT a fair" in label


def test_server_mode_label_not_applicable_for_non_whisper_cpp() -> None:
    class _NoServerFlag:
        pass

    assert sb._server_mode_label(_NoServerFlag()) == "n/a (not whisper.cpp)"


# ---------------------------------------------------------------------------
# LLM probe -- fully mocked, no real network/provider call
# ---------------------------------------------------------------------------


def test_measure_llm_latency_skips_when_provider_none() -> None:
    config = sb.DetectorConfig(llm_provider="none")

    result = asyncio.run(sb.measure_llm_latency(config))

    assert result.skipped_reason == "LLM_PROVIDER is 'none'"
    assert result.error is None
    assert result.ttft_ms is None


def test_measure_llm_latency_skips_when_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "is_provider_available", lambda _provider: (False, "GROQ_API_KEY is empty"))
    config = sb.DetectorConfig(llm_provider="groq")

    result = asyncio.run(sb.measure_llm_latency(config))

    assert result.skipped_reason == "GROQ_API_KEY is empty"


class _FakeLLMClient:
    def __init__(self, provider: str, *, timeout_ms: int) -> None:
        self.provider = provider
        self.timeout_ms = timeout_ms
        self.last_ttft_ms = 42.0

    async def astream(self, **_kwargs: object):
        yield sb._BenchActionItems(
            items=[sb._BenchActionItem(description="bel morgen terug", owner="sales", due_hint="morgen")]
        )


def test_measure_llm_latency_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "is_provider_available", lambda _provider: (True, "ok"))
    monkeypatch.setattr(sb, "LLMClient", _FakeLLMClient)
    config = sb.DetectorConfig(llm_provider="fakeprov", llm_model="fake-model")

    result = asyncio.run(sb.measure_llm_latency(config))

    assert result.provider == "fakeprov"
    assert result.model == "fake-model"
    assert result.ttft_ms == 42.0
    assert result.total_latency_ms is not None and result.total_latency_ms >= 0
    assert result.n_items == 1
    assert result.error is None
    assert result.skipped_reason is None


class _FailingLLMClient:
    def __init__(self, provider: str, *, timeout_ms: int) -> None:
        self.provider = provider
        self.last_ttft_ms = None

    async def astream(self, **_kwargs: object):
        raise RuntimeError("boom")
        yield  # pragma: no cover - unreachable, keeps this an async generator function


def test_measure_llm_latency_records_error_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sb, "is_provider_available", lambda _provider: (True, "ok"))
    monkeypatch.setattr(sb, "LLMClient", _FailingLLMClient)
    config = sb.DetectorConfig(llm_provider="fakeprov")

    result = asyncio.run(sb.measure_llm_latency(config))

    assert result.error is not None and "boom" in result.error
    assert result.skipped_reason is None


# ---------------------------------------------------------------------------
# Report aggregation + markdown/json rendering
# ---------------------------------------------------------------------------


def _fake_hardware() -> sb.HardwareFingerprint:
    return sb.HardwareFingerprint(
        platform="macOS-test", machine="arm64", processor="arm", python_version="3.12.0", cpu_count=8, gpu="fake-gpu"
    )


def test_build_report_aggregates_ok_and_failed_segments() -> None:
    results = [
        sb.SegmentLatency(index=0, duration_s=1.0, latency_ms=100.0, text="a"),
        sb.SegmentLatency(index=1, duration_s=1.0, error="boom"),
    ]

    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=results,
        whisper_server_mode="resident whisper-server (HTTP, model stays loaded)",
        llm_probe=None,
    )

    assert report.n_segments == 2
    assert report.whisper_n_ok == 1
    assert report.whisper_n_failed == 1
    assert report.whisper_errors_sample == ["boom"]
    assert report.llm_probe is None


def test_render_markdown_contains_expected_sections_and_no_llm_run() -> None:
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic amplitude-modulated tone (3 segments, no TTS engine available)",
        whisper_results=[sb.SegmentLatency(index=0, duration_s=1.0, latency_ms=990.0, text="hallo")],
        whisper_server_mode="resident whisper-server (HTTP, model stays loaded)",
        llm_probe=None,
    )

    md = sb.render_markdown(report)

    assert "Mock audio only" in md
    assert "## Hardware fingerprint" in md
    assert "## Mock audio" in md
    assert "## Whisper transcription latency" in md
    assert "## LLM probe" in md
    assert "Not run -- pass `--llm`" in md
    assert "990" in md and "209" in md  # reference baseline numbers


def test_render_markdown_llm_probe_success_section() -> None:
    probe = sb.LLMProbeResult(provider="ollama", model="gemma4:e4b", ttft_ms=50.0, total_latency_ms=500.0, n_items=2)
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=[],
        whisper_server_mode="n/a (not whisper.cpp)",
        llm_probe=probe,
    )

    md = sb.render_markdown(report)

    assert "**Provider:** ollama (gemma4:e4b)" in md
    assert "**TTFT:** 50 ms" in md
    assert "Action items extracted:** 2" in md


def test_render_markdown_llm_probe_skipped_section() -> None:
    probe = sb.LLMProbeResult(provider="none", model="n/a", skipped_reason="LLM_PROVIDER is 'none'")
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=[],
        whisper_server_mode="n/a (not whisper.cpp)",
        llm_probe=probe,
    )

    md = sb.render_markdown(report)

    assert "**Skipped:** LLM_PROVIDER is 'none'" in md


def test_render_markdown_llm_probe_error_section() -> None:
    probe = sb.LLMProbeResult(provider="ollama", model="gemma4:e4b", error="RuntimeError: boom")
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=[],
        whisper_server_mode="n/a (not whisper.cpp)",
        llm_probe=probe,
    )

    md = sb.render_markdown(report)

    assert "**Error:** RuntimeError: boom" in md


def test_report_to_json_dict_is_json_serializable() -> None:
    probe = sb.LLMProbeResult(provider="ollama", model="gemma4:e4b", ttft_ms=50.0, total_latency_ms=500.0, n_items=1)
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=[sb.SegmentLatency(index=0, duration_s=1.0, latency_ms=100.0, text="hallo")],
        whisper_server_mode="resident whisper-server (HTTP, model stays loaded)",
        llm_probe=probe,
    )

    payload = json.dumps(report.to_json_dict())

    assert "whisper_latency" in payload
    parsed = json.loads(payload)
    assert parsed["llm_probe"]["provider"] == "ollama"


def test_report_to_json_dict_with_no_llm_probe_is_null() -> None:
    report = sb.build_report(
        hardware=_fake_hardware(),
        mock_audio_dir=Path("/tmp/mock"),
        mock_audio_source="synthetic",
        whisper_results=[],
        whisper_server_mode="n/a (not whisper.cpp)",
        llm_probe=None,
    )

    assert report.to_json_dict()["llm_probe"] is None


# ---------------------------------------------------------------------------
# CLI -- mocked backend, --no-tts, no LLM probe (default), no network anywhere
# ---------------------------------------------------------------------------


def test_main_writes_reports_with_mocked_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubWhisperBackend()
    monkeypatch.setattr(sb, "create_backend", lambda _cfg: stub)

    report_md = tmp_path / "report.md"
    report_json = tmp_path / "report.json"
    mock_dir = tmp_path / "mock_audio"

    exit_code = sb.main(
        [
            "--no-tts",
            "--no-bundled-assets",
            "--limit",
            "2",
            "--mock-audio-dir",
            str(mock_dir),
            "--report-md",
            str(report_md),
            "--report-json",
            str(report_json),
        ]
    )

    assert exit_code == 0
    assert report_md.exists()
    assert report_json.exists()

    payload = json.loads(report_json.read_text())
    assert payload["n_segments"] == 2
    assert payload["whisper_n_ok"] == 2
    assert payload["llm_probe"] is None
    assert "synthetic" in payload["mock_audio_source"]


def test_main_prefers_bundled_wavs_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubWhisperBackend()
    monkeypatch.setattr(sb, "create_backend", lambda _cfg: stub)

    exit_code = sb.main(
        [
            "--limit",
            "2",
            "--mock-audio-dir",
            str(tmp_path / "mock_audio"),
            "--report-md",
            str(tmp_path / "report.md"),
            "--report-json",
            str(tmp_path / "report.json"),
        ]
    )

    assert exit_code == 0
    payload = json.loads((tmp_path / "report.json").read_text())
    assert "bundled" in payload["mock_audio_source"]


def test_main_regenerate_bundled_assets_flag_short_circuits_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_be_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("run_benchmark should not run when --regenerate-bundled-assets is passed")

    monkeypatch.setattr(sb, "run_benchmark", _must_not_be_called)
    monkeypatch.setattr(sb, "_tts_available", lambda _voice: True)

    def _fake_say(_sentence: str, out_path: Path, _voice: str) -> bool:
        sb._generate_synthetic_tone(0, out_path)
        return True

    monkeypatch.setattr(sb, "_generate_via_say", _fake_say)
    monkeypatch.setattr(sb, "_BUNDLED_AUDIO_DIR", tmp_path / "regen")

    exit_code = sb.main(["--regenerate-bundled-assets"])

    assert exit_code == 0
    assert len(list((tmp_path / "regen").glob("mock_*.wav"))) == sb._N_BUNDLED_SENTENCES
