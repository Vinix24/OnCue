#!/usr/bin/env python3
"""Portable whisper + LLM latency benchmark for a TARGET machine (e.g. an NVIDIA DGX
Spark / CUDA box), using ONLY non-sensitive MOCK audio bundled with this script.

MOCK AUDIO ONLY -- NO REAL CALL DATA, EVER. This script never reads ``data/sessions/``
or any other real recording. Every audio segment it transcribes is one of, in preference
order:

  1. a pre-generated WAV checked into ``scripts/benchmark_assets/`` -- itself macOS
     ``say`` TTS reading one of the invented sentences in ``_MOCK_SENTENCES`` below,
     generated once and committed so the kit is self-contained on machines with no TTS
     engine (e.g. Linux/the Spark itself),
  2. live macOS ``say`` TTS reading a ``_MOCK_SENTENCES`` entry that has no bundled WAV
     (e.g. beyond the bundled count, or the bundled file is missing), or
  3. a synthetic amplitude-modulated tone (no bundled WAV and no TTS engine available) of
     a representative, speech-like duration -- last resort.

The point: transcription latency is hardware-bound, not content-bound -- a whisper
segment's processing time depends on the GPU, not on what is said. So a fair hardware
benchmark needs representative, speech-like audio, not real calls -- and needs it without
requiring a TTS engine on the target machine. This lets Vincent hand the benchmark to a
third party (a Spark owner) without exposing a single real conversation.

It reuses the project's real transcription backend
(``sales_copilot.modules.transcriber.backends.create_backend``, same as
``scripts/stt_compare.py``) so it works with a whisper.cpp CUDA build on the Spark via
config alone (``WHISPER_CPP_BINARY`` / ``WHISPER_CPP_SERVER_BINARY`` /
``WHISPER_CPP_MODEL_PATH`` env vars) -- this file adds no new backend code.

Usage:

    # Whisper-only, mock audio, writes claudedocs/<date>-spark-benchmark.{md,json}
    .venv/bin/python scripts/spark_benchmark.py

    # Also probe the configured LLM_PROVIDER for generation latency + time-to-first-token
    .venv/bin/python scripts/spark_benchmark.py --llm

    # Force the synthetic-tone fallback even where `say` is available and bundled WAVs
    # exist (e.g. for a deterministic CI-like run, or to preview what a from-scratch
    # non-macOS box with no bundled assets will produce)
    .venv/bin/python scripts/spark_benchmark.py --no-tts --no-bundled-assets

    # Regenerate the bundled WAVs in scripts/benchmark_assets/ from _MOCK_SENTENCES
    # (Mac + `say` required; run this after editing _MOCK_SENTENCES)
    .venv/bin/python scripts/spark_benchmark.py --regenerate-bundled-assets

See ``scripts/SPARK_BENCHMARK.md`` for the full Spark (CUDA whisper.cpp build) runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import numpy as np  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from sales_copilot.core.config import DetectorConfig, TranscriberConfig, load_env  # noqa: E402
from sales_copilot.core.llm_client import LLMClient  # noqa: E402
from sales_copilot.modules.detector.eval_shared import is_provider_available, latency_stats  # noqa: E402
from sales_copilot.modules.transcriber.backends import create_backend  # noqa: E402
from sales_copilot.modules.transcriber.wav_utils import write_wav  # noqa: E402

_TODAY = "2026-07-18"
_DEFAULT_REPORT_MD = REPO_ROOT / "claudedocs" / f"{_TODAY}-spark-benchmark.md"
_DEFAULT_REPORT_JSON = REPO_ROOT / "claudedocs" / f"{_TODAY}-spark-benchmark.json"
_SAMPLE_RATE = 16000
_SAY_VOICE = "Xander"  # nl_NL macOS voice, verified present on Vincent's dev machine.

# Pre-generated WAVs (macOS `say` reading the first _N_BUNDLED_SENTENCES entries of
# _MOCK_SENTENCES below, checked into the repo) -- see scripts/benchmark_assets/README.md.
_BUNDLED_AUDIO_DIR = REPO_ROOT / "scripts" / "benchmark_assets"
_N_BUNDLED_SENTENCES = 8

# Reference numbers from scripts/stt_compare.py runs against real (gitignored) session
# fixtures on Vincent's Mac -- context only, NOT reproduced here (no real audio in this
# script). See claudedocs/<date>-stt-compare.md for the source run.
_REFERENCE_LOCAL_P50_MS = 990
_REFERENCE_GROQ_P50_MS = 209

# Ten to fifteen neutral Dutch sales-call-style sentences -- generic, non-sensitive,
# invented for this benchmark. NOT excerpted from any real call or transcript. The first
# _N_BUNDLED_SENTENCES of these have a pre-generated WAV checked into
# scripts/benchmark_assets/ (see _BUNDLED_AUDIO_DIR above); do not reorder this tuple
# without regenerating those files (`--regenerate-bundled-assets`).
_MOCK_SENTENCES: tuple[str, ...] = (
    "Hallo, bedankt dat u de tijd neemt voor dit gesprek.",
    "Kunt u me vertellen wat op dit moment de grootste uitdaging is binnen uw team?",
    "We zien vaak dat bedrijven tijd verliezen aan handmatige rapportages.",
    "Wat is voor u op dit moment de belangrijkste prioriteit?",
    "Onze oplossing helpt teams om sneller inzicht te krijgen in hun cijfers.",
    "Hoeveel mensen zijn er op dit moment betrokken bij dit proces?",
    "Ik begrijp dat budget een belangrijke factor is bij deze beslissing.",
    "Laten we samen kijken naar een aanpak die past bij uw situatie.",
    "Wat zou het voor u betekenen als dit probleem is opgelost?",
    "We werken meestal met een korte proefperiode van enkele weken.",
    "Zijn er nog andere collega's bij deze beslissing betrokken?",
    "Bedankt voor dit gesprek, ik stuur u een samenvatting per e-mail.",
    "Wanneer zou een vervolgafspraak het beste uitkomen?",
    "Hoe ziet uw huidige werkwijze eruit voor dit onderdeel?",
)


# ---------------------------------------------------------------------------
# Mock audio generation -- NEVER reads data/sessions/ or any real recording.
# ---------------------------------------------------------------------------


def _say_binary() -> str | None:
    return shutil.which("say")


def _tts_available(voice: str) -> bool:
    """Best-effort check that macOS ``say`` and the requested voice are present."""
    binary = _say_binary()
    if binary is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed args, no user input
            [binary, "-v", "?"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.strip().startswith(voice) for line in result.stdout.splitlines())


def _generate_via_say(sentence: str, out_path: Path, voice: str) -> bool:
    """Write ``sentence`` as a 16kHz mono WAV via macOS ``say``. Returns True on success."""
    binary = _say_binary()
    if binary is None:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(  # noqa: S603 - fixed flags, sentence is from the bundled tuple above
            [
                binary,
                "-v",
                voice,
                "-o",
                str(out_path),
                "--file-format=WAVE",
                f"--data-format=LEI16@{_SAMPLE_RATE}",
                sentence,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return out_path.exists() and out_path.stat().st_size > 44  # more than a bare WAV header


def _generate_synthetic_tone(index: int, out_path: Path) -> None:
    """Write a deterministic amplitude-modulated tone WAV -- NOT real/synthesized speech.

    Duration and pitch vary by ``index`` to mirror the range of real VAD segment lengths
    (~1.5-3.5s) without depending on a TTS engine. The ~3.5 Hz envelope roughly mirrors a
    syllable rate so the segment isn't pure silence-adjacent noise for the transcriber,
    but the transcript text itself is meaningless -- only latency is measured.
    """
    duration_s = 1.5 + (index % 5) * 0.5
    frequency_hz = 180.0 + (index % 7) * 40.0
    n_samples = int(duration_s * _SAMPLE_RATE)
    t = np.linspace(0.0, duration_s, n_samples, endpoint=False)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3.5 * t)
    audio = (np.sin(2 * np.pi * frequency_hz * t) * envelope * 0.4).astype(np.float32)
    write_wav(out_path, audio, _SAMPLE_RATE)


def _bundled_wav_path(index: int, bundled_dir: Path) -> Path:
    return bundled_dir / f"mock_{index:02d}.wav"


def generate_mock_audio(
    output_dir: Path,
    *,
    sentences: tuple[str, ...] = _MOCK_SENTENCES,
    use_tts: bool = True,
    use_bundled: bool = True,
    voice: str = _SAY_VOICE,
    bundled_dir: Path = _BUNDLED_AUDIO_DIR,
) -> tuple[list[Path], str]:
    """Generate/select one short mock WAV per sentence. Returns (paths, source description).

    Selection order per sentence, so the benchmark always has representative, speech-like
    audio to measure -- even on a machine with no TTS engine at all (e.g. Linux/the Spark):

      1. A pre-generated WAV checked into ``bundled_dir`` (when ``use_bundled`` and the
         file exists and looks like real audio, not a bare header).
      2. Live macOS ``say`` TTS (when ``use_tts`` and the voice is installed).
      3. A synthetic amplitude-modulated tone -- last resort.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    tts_ok = use_tts and _tts_available(voice)
    paths: list[Path] = []
    any_bundled = False
    any_tts = False
    any_synthetic = False
    for i, sentence in enumerate(sentences):
        out_path = output_dir / f"mock_{i:02d}.wav"
        bundled_path = _bundled_wav_path(i, bundled_dir)
        if use_bundled and bundled_path.is_file() and bundled_path.stat().st_size > 44:
            shutil.copyfile(bundled_path, out_path)
            any_bundled = True
            paths.append(out_path)
            continue
        generated = _generate_via_say(sentence, out_path, voice) if tts_ok else False
        if generated:
            any_tts = True
        else:
            _generate_synthetic_tone(i, out_path)
            any_synthetic = True
        paths.append(out_path)

    if any_bundled and not any_tts and not any_synthetic:
        source = (
            f"bundled pre-generated WAVs (checked into the repo; originally macOS `say` "
            f"voice={voice} reading {len(sentences)} invented Dutch sales sentences)"
        )
    elif any_tts and not any_bundled and not any_synthetic:
        source = f"macOS `say` TTS (voice={voice}), reading {len(sentences)} built-in Dutch sales sentences"
    elif any_synthetic and not any_bundled and not any_tts:
        source = f"synthetic amplitude-modulated tone ({len(sentences)} segments, no TTS engine available)"
    else:
        parts = []
        if any_bundled:
            parts.append("bundled pre-generated WAVs")
        if any_tts:
            parts.append(f"live macOS `say` TTS (voice={voice})")
        if any_synthetic:
            parts.append("synthetic-tone fallback")
        source = "mixed: " + " + ".join(parts)
    return paths, source


def _read_wav_float32(path: Path) -> np.ndarray:
    """Read a 16-bit PCM WAV file into a mono float32 array in [-1, 1]."""
    with wave.open(str(path), "rb") as handle:
        n_frames = handle.getnframes()
        raw = handle.readframes(n_frames)
        sample_width = handle.getsampwidth()
        channels = handle.getnchannels()
    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got sample width {sample_width} for {path}")
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


# ---------------------------------------------------------------------------
# Whisper latency -- reuses the real TranscriptionBackend, no whisper reimplementation.
# ---------------------------------------------------------------------------


@dataclass
class SegmentLatency:
    index: int
    duration_s: float
    latency_ms: float | None = None
    text: str = ""
    error: str | None = None


def _server_mode_label(backend: Any) -> str:
    """Describe whether whisper.cpp is running its resident-server fast path.

    Mirrors ``scripts/stt_compare.py``'s ``LocalCandidate.server_mode()`` -- a
    report-only introspection of the same private flag, never mutated here.
    """
    started = getattr(backend, "_server_started", None)  # noqa: SLF001
    if started is None:
        return "n/a (not whisper.cpp)"
    if started:
        return "resident whisper-server (HTTP, model stays loaded)"
    return "one-shot whisper-cli (model reloaded per chunk -- NOT a fair cloud comparison)"


async def measure_whisper_latency(
    wav_paths: list[Path], config: TranscriberConfig
) -> tuple[list[SegmentLatency], str]:
    """Transcribe every mock WAV with the configured backend; return (results, server mode)."""
    backend = create_backend(
        {
            "backend": config.backend,
            "language": config.language,
            "whisper_cpp_binary": config.whisper_cpp_binary,
            "whisper_cpp_model_path": config.whisper_cpp_model_path,
            "whisper_cpp_threads": config.whisper_cpp_threads,
            "whisper_cpp_server_binary": config.whisper_cpp_server_binary,
        }
    )
    await backend.start(asyncio.Event())
    await backend.warmup()
    server_mode = _server_mode_label(backend)

    results: list[SegmentLatency] = []
    for i, wav_path in enumerate(wav_paths):
        audio = _read_wav_float32(wav_path)
        duration_s = len(audio) / _SAMPLE_RATE
        started = time.perf_counter()
        try:
            text = await backend.transcribe(audio)
            latency_ms = (time.perf_counter() - started) * 1000
            results.append(SegmentLatency(index=i, duration_s=duration_s, latency_ms=latency_ms, text=text))
        except Exception as exc:  # noqa: BLE001 -- one bad segment must not crash the run
            results.append(SegmentLatency(index=i, duration_s=duration_s, error=str(exc)))

    await backend.stop()
    return results, server_mode


# ---------------------------------------------------------------------------
# Optional local-LLM probe -- generation latency + time-to-first-token.
# ---------------------------------------------------------------------------


class _BenchActionItem(BaseModel):
    description: str = Field(..., description="Concrete, beknopte actiebeschrijving in het Nederlands.")
    owner: str | None = Field(None, description="Wie de actie uitvoert: 'prospect', 'sales', of 'beide'.")
    due_hint: str | None = Field(None, description="Wanneer, indien genoemd in het gesprek.")


class _BenchActionItems(BaseModel):
    """Small structured-output schema, representative of the live copilot's summary call."""

    items: list[_BenchActionItem] = Field(default_factory=list)


_LLM_PROBE_SYSTEM_PROMPT = (
    "Je bent een sales-assistent die concrete actiepunten haalt uit een verkoopgesprek. "
    "Geef 1-3 korte, concrete actiepunten in het Nederlands, met wie de actie uitvoert "
    "en wanneer (indien genoemd)."
)


@dataclass
class LLMProbeResult:
    provider: str
    model: str
    ttft_ms: float | None = None
    total_latency_ms: float | None = None
    n_items: int = 0
    error: str | None = None
    skipped_reason: str | None = None


async def measure_llm_latency(config: DetectorConfig) -> LLMProbeResult:
    """Send one representative structured-output prompt to the configured LLM.

    Skips gracefully (populated ``skipped_reason``, no exception) when no LLM is
    configured or its credentials/health-check are missing -- this benchmark's whisper
    measurement must never depend on an LLM being set up.
    """
    provider = config.llm_provider.strip().lower()
    if provider in ("", "none"):
        return LLMProbeResult(
            provider=provider or "none", model=config.llm_model, skipped_reason="LLM_PROVIDER is 'none'"
        )

    available, reason = is_provider_available(provider)
    if not available:
        return LLMProbeResult(provider=provider, model=config.llm_model, skipped_reason=reason)

    client = LLMClient(provider, timeout_ms=config.llm_timeout_ms)
    user_text = "Gesprekstranscript:\n" + "\n".join(f"- {s}" for s in _MOCK_SENTENCES)

    started = time.perf_counter()
    last_item: _BenchActionItems | None = None
    try:
        async for item in client.astream(
            model=config.llm_model,
            system_prompt=_LLM_PROBE_SYSTEM_PROMPT,
            user_text=user_text,
            response_model=_BenchActionItems,
            temperature=config.llm_temperature,
            allow_local=True,
            max_tokens=512,
        ):
            last_item = item
    except Exception as exc:  # noqa: BLE001 -- report as a failed probe, never crash the run
        return LLMProbeResult(
            provider=provider, model=config.llm_model, error=f"{type(exc).__name__}: {exc}"
        )

    total_latency_ms = (time.perf_counter() - started) * 1000
    n_items = len(last_item.items) if last_item is not None else 0
    return LLMProbeResult(
        provider=provider,
        model=config.llm_model,
        ttft_ms=client.last_ttft_ms,
        total_latency_ms=total_latency_ms,
        n_items=n_items,
    )


# ---------------------------------------------------------------------------
# Hardware fingerprint -- best-effort, self-describing report only.
# ---------------------------------------------------------------------------


@dataclass
class HardwareFingerprint:
    platform: str
    machine: str
    processor: str
    python_version: str
    cpu_count: int | None
    gpu: str


def _detect_gpu() -> str:
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(  # noqa: S603 - fixed args
                [nvidia_smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                return "; ".join(lines)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass

    if platform.system() == "Darwin":
        try:
            result = subprocess.run(  # noqa: S603 - fixed args
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            for line in result.stdout.splitlines():
                if "Chipset Model" in line:
                    return line.split(":", 1)[1].strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass

    return "unknown (no nvidia-smi, not Darwin, or detection failed)"


def detect_hardware() -> HardwareFingerprint:
    return HardwareFingerprint(
        platform=platform.platform(),
        machine=platform.machine(),
        processor=platform.processor() or platform.machine(),
        python_version=platform.python_version(),
        cpu_count=os.cpu_count(),
        gpu=_detect_gpu(),
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class SparkBenchmarkReport:
    hardware: HardwareFingerprint
    mock_audio_dir: str
    mock_audio_source: str
    n_segments: int
    whisper_server_mode: str
    whisper_latency: dict[str, float]
    whisper_n_ok: int
    whisper_n_failed: int
    whisper_errors_sample: list[str] = field(default_factory=list)
    llm_probe: LLMProbeResult | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "hardware": asdict(self.hardware),
            "mock_audio_dir": self.mock_audio_dir,
            "mock_audio_source": self.mock_audio_source,
            "n_segments": self.n_segments,
            "whisper_server_mode": self.whisper_server_mode,
            "whisper_latency": self.whisper_latency,
            "whisper_n_ok": self.whisper_n_ok,
            "whisper_n_failed": self.whisper_n_failed,
            "whisper_errors_sample": self.whisper_errors_sample,
            "llm_probe": asdict(self.llm_probe) if self.llm_probe is not None else None,
        }


def build_report(
    *,
    hardware: HardwareFingerprint,
    mock_audio_dir: Path,
    mock_audio_source: str,
    whisper_results: list[SegmentLatency],
    whisper_server_mode: str,
    llm_probe: LLMProbeResult | None,
) -> SparkBenchmarkReport:
    ok = [r for r in whisper_results if r.error is None]
    failed = [r for r in whisper_results if r.error is not None]
    latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
    return SparkBenchmarkReport(
        hardware=hardware,
        mock_audio_dir=str(mock_audio_dir),
        mock_audio_source=mock_audio_source,
        n_segments=len(whisper_results),
        whisper_server_mode=whisper_server_mode,
        whisper_latency=latency_stats(latencies),
        whisper_n_ok=len(ok),
        whisper_n_failed=len(failed),
        whisper_errors_sample=[r.error for r in failed[:3] if r.error],
        llm_probe=llm_probe,
    )


def _fmt_ms(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f} ms"


def render_markdown(report: SparkBenchmarkReport) -> str:
    lines: list[str] = [
        "# Spark benchmark -- whisper + LLM latency (mock audio only, no real call data)",
        "",
        "**Mock audio only.** Every segment measured below is a pre-generated bundled WAV, "
        "live macOS `say` TTS, or a synthetic tone reading/representing a non-sensitive, "
        "invented Dutch sales sentence -- never real call audio. This script never reads "
        "`data/sessions/` or any other real recording.",
        "",
        "## Hardware fingerprint",
        "",
        f"- **Platform:** {report.hardware.platform}",
        f"- **Machine:** {report.hardware.machine}",
        f"- **Processor:** {report.hardware.processor}",
        f"- **CPU count:** {report.hardware.cpu_count}",
        f"- **GPU:** {report.hardware.gpu}",
        f"- **Python:** {report.hardware.python_version}",
        "",
        "## Mock audio",
        "",
        f"- **Source:** {report.mock_audio_source}",
        f"- **Segments:** {report.n_segments}",
        f"- **Directory:** {report.mock_audio_dir}",
        "",
        "## Whisper transcription latency",
        "",
        f"**Backend server mode:** {report.whisper_server_mode}",
        "",
        "| n ok | n failed | p50 | p95 | mean |",
        "|---:|---:|---:|---:|---:|",
        (
            f"| {report.whisper_n_ok} | {report.whisper_n_failed} | "
            f"{_fmt_ms(report.whisper_latency.get('p50'))} | {_fmt_ms(report.whisper_latency.get('p95'))} | "
            f"{_fmt_ms(report.whisper_latency.get('mean'))} |"
        ),
        "",
        "### Reference baseline (Mac, real-call fixtures via `scripts/stt_compare.py`)",
        "",
        f"Local whisper.cpp large-v3-turbo p50 ~{_REFERENCE_LOCAL_P50_MS}ms; Groq cloud "
        f"whisper-large-v3-turbo p50 ~{_REFERENCE_GROQ_P50_MS}ms. Context only -- not a "
        "segment-for-segment replay of the same audio (this run uses mock audio, that run "
        "used real, gitignored session fixtures), but the same `latency_stats` p50/p95/mean "
        "contract, so the shapes are directly comparable.",
        "",
        "## LLM probe",
        "",
    ]

    if report.llm_probe is None:
        lines.append("Not run -- pass `--llm` to measure local-LLM generation latency + time-to-first-token.")
    else:
        p = report.llm_probe
        lines.append(f"- **Provider:** {p.provider} ({p.model})")
        if p.skipped_reason:
            lines.append(f"- **Skipped:** {p.skipped_reason}")
        elif p.error:
            lines.append(f"- **Error:** {p.error}")
        else:
            lines.append(f"- **TTFT:** {_fmt_ms(p.ttft_ms)}")
            lines.append(f"- **Total latency:** {_fmt_ms(p.total_latency_ms)}")
            lines.append(f"- **Action items extracted:** {p.n_items}")
            lines.append("")
            lines.append(
                "Context: on a 24GB Mac, the fastest locally-measured structured-output call "
                "was ~2815ms p50 (mistral-small via Ollama) -- too slow for the live <=300ms "
                "coaching target, which is why the live copilot's local-LLM path is parked in "
                "favor of cloud/BYO-tenant. This probe tests whether stronger GPU headroom "
                "changes that verdict on this machine."
            )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def run_benchmark(args: argparse.Namespace) -> SparkBenchmarkReport:
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    hardware = detect_hardware()

    if args.mock_audio_dir:
        mock_dir = Path(args.mock_audio_dir)
    else:
        mock_dir = Path(tempfile.mkdtemp(prefix="spark-benchmark-mock-"))
    sentences = _MOCK_SENTENCES[: max(1, args.limit)]
    wav_paths, mock_source = generate_mock_audio(
        mock_dir, sentences=sentences, use_tts=not args.no_tts, use_bundled=not args.no_bundled_assets
    )

    transcriber_config = TranscriberConfig.from_env()
    whisper_results, server_mode = await measure_whisper_latency(wav_paths, transcriber_config)

    llm_probe: LLMProbeResult | None = None
    if args.llm:
        detector_config = DetectorConfig.from_env()
        llm_probe = await measure_llm_latency(detector_config)

    return build_report(
        hardware=hardware,
        mock_audio_dir=mock_dir,
        mock_audio_source=mock_source,
        whisper_results=whisper_results,
        whisper_server_mode=server_mode,
        llm_probe=llm_probe,
    )


def _regenerate_bundled_assets(
    *,
    sentences: tuple[str, ...] = _MOCK_SENTENCES[:_N_BUNDLED_SENTENCES],
    bundled_dir: Path = _BUNDLED_AUDIO_DIR,
    voice: str = _SAY_VOICE,
) -> int:
    """Regenerate the checked-in bundled WAVs from ``sentences`` via macOS ``say``.

    Requires a Mac with ``say`` and ``voice`` installed -- this is a dev-time utility, not
    part of a normal benchmark run (a normal run just reads the already-bundled files).
    """
    if not _tts_available(voice):
        print(
            f"macOS `say` (voice={voice}) is not available -- bundled assets can only be "
            "regenerated on a Mac with that voice installed.",
            file=sys.stderr,
        )
        return 1
    bundled_dir.mkdir(parents=True, exist_ok=True)
    for i, sentence in enumerate(sentences):
        out_path = _bundled_wav_path(i, bundled_dir)
        if not _generate_via_say(sentence, out_path, voice):
            print(f"[FAIL] could not generate {out_path}", file=sys.stderr)
            return 1
        print(f"[ok] {out_path} ({out_path.stat().st_size} bytes)")
    print(f"\nRegenerated {len(sentences)} bundled WAVs in {bundled_dir}")
    return 0


def _write_reports(report: SparkBenchmarkReport, report_md: Path, report_json: Path) -> None:
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markdown(report), encoding="utf-8")
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report.to_json_dict(), indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Also probe the configured LLM_PROVIDER for generation latency + time-to-first-token.",
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Force the synthetic-tone fallback for any sentence with no bundled WAV, even "
        "where macOS `say` is available.",
    )
    parser.add_argument(
        "--no-bundled-assets",
        action="store_true",
        help="Skip the bundled WAVs in scripts/benchmark_assets/ even when present, falling "
        "through to `say` (or the synthetic tone). Mainly for testing/determinism.",
    )
    parser.add_argument(
        "--regenerate-bundled-assets",
        action="store_true",
        help="Regenerate the bundled WAVs in scripts/benchmark_assets/ from _MOCK_SENTENCES via "
        "macOS `say`, then exit without running the benchmark. Requires a Mac with `say`.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=len(_MOCK_SENTENCES),
        help=f"Cap on mock segments. Default: all {len(_MOCK_SENTENCES)} sentences "
        f"(first {_N_BUNDLED_SENTENCES} have a bundled WAV; the rest use `say`/the tone).",
    )
    parser.add_argument(
        "--mock-audio-dir",
        type=Path,
        default=None,
        help="Directory to write generated mock WAVs. Default: a fresh temp directory.",
    )
    parser.add_argument("--report-md", type=Path, default=_DEFAULT_REPORT_MD, help=f"Default: {_DEFAULT_REPORT_MD}")
    parser.add_argument(
        "--report-json", type=Path, default=_DEFAULT_REPORT_JSON, help=f"Default: {_DEFAULT_REPORT_JSON}"
    )
    args = parser.parse_args(argv)

    if args.regenerate_bundled_assets:
        return _regenerate_bundled_assets(bundled_dir=_BUNDLED_AUDIO_DIR)

    report = asyncio.run(run_benchmark(args))
    _write_reports(report, Path(args.report_md), Path(args.report_json))
    print(render_markdown(report))
    print(f"\nMarkdown report: {args.report_md}")
    print(f"JSON report: {args.report_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
