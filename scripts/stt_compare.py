#!/usr/bin/env python3
"""STT comparison: local whisper.cpp (large-v3-turbo) vs Groq and OpenAI cloud whisper.

MEASURE-FIRST harness -- answers whether a cloud whisper model beats the local
``large-v3-turbo`` backend (the measured dominant latency cost, see
``claudedocs/2026-07-18-e2e-latency-breakdown.md``) on latency AND Dutch transcript
quality. This does NOT wire a cloud backend into the live copilot path -- it is a
standalone comparison script + report only.

Segments are collected ONCE by replaying real ``prospect.wav`` fixtures through the
production ``AudioBufferer`` (the same VAD segmentation the live copilot and
``scripts/e2e_latency_harness.py`` use), then every backend transcribes the IDENTICAL
audio for each segment -- so the latency and quality comparison is apples-to-apples.

Backends:
    local   ``TranscriptionBackend`` from ``sales_copilot.modules.transcriber.backends``
            (whisper.cpp resident ``whisper-server`` fast path by default, see
            ``backends/whisper_cpp_backend.py``). ``--local-threads`` optionally runs
            more than one thread-count variant to measure whether raising
            ``WHISPER_CPP_THREADS`` (4 by default, see ``.env.example``) is a free
            latency win independent of the cloud question.
    groq    Groq's OpenAI-compatible audio endpoint, ``whisper-large-v3-turbo``.
    openai  OpenAI's audio endpoint: ``whisper-1`` and (best-effort)
            ``gpt-4o-transcribe``.

The FIRST local variant is the WER/CER reference -- there is no ground truth, so
transcript quality is reported as cloud-vs-local pairwise agreement, not absolute
accuracy.

CLI:

    # Full comparison against real session fixtures (run manually -- data/sessions/,
    # a built whisper.cpp binary, GROQ_API_KEY and OPENAI_API_KEY are all required and
    # are not present in CI / dispatch worktrees).
    .venv/bin/python scripts/stt_compare.py --top-n 15

    # Compare WHISPER_CPP_THREADS=4 (the .env.example default) against 8:
    .venv/bin/python scripts/stt_compare.py --local-threads 4,8 --backends local
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from sales_copilot.audio.replay import ReplayAudioStream  # noqa: E402
from sales_copilot.core.config import TranscriberConfig, env, load_env, with_overrides  # noqa: E402
from sales_copilot.modules.detector.eval_shared import is_provider_available, latency_stats  # noqa: E402
from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer  # noqa: E402
from sales_copilot.modules.transcriber.backends import create_backend  # noqa: E402
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend  # noqa: E402
from sales_copilot.modules.transcriber.inference_queue import InferenceQueueItem, SharedInferenceQueue  # noqa: E402
from sales_copilot.modules.transcriber.wav_utils import encode_wav_bytes  # noqa: E402

_DEFAULT_SESSIONS_DIR = REPO_ROOT / "data" / "sessions"
_DEFAULT_REPORT_MD = REPO_ROOT / "claudedocs" / "2026-07-18-stt-compare.md"
_DEFAULT_REPORT_JSON = REPO_ROOT / "claudedocs" / "2026-07-18-stt-compare.json"
_DEFAULT_TOP_N = 15
_DEFAULT_TIMEOUT_MS = 20_000
_EOF_SETTLE_S = 3.0  # let AudioBufferer flush its final segment after EOF, mirrors e2e_latency_harness.py
_QUEUE_POLL_TIMEOUT_S = 0.2
_MAX_EXAMPLE_DIFFS = 3

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL = "whisper-large-v3-turbo"
_OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
_OPENAI_MODELS = ["whisper-1", "gpt-4o-transcribe"]

# List prices, NOT re-verified against the live provider pricing pages -- this worker
# profile has no WebFetch/WebSearch. Re-verify at https://groq.com/pricing and
# https://openai.com/api/pricing before using these for a real budget decision (same
# "list price" caveat pattern as config/eval_model_prices.yaml's OpenRouter entries).
_USD_PER_MINUTE: dict[str, float | None] = {
    f"groq:{_GROQ_MODEL}": 0.04 / 60,  # ~$0.04/hour list price -> $/minute
    "openai:whisper-1": 0.006,  # stable, well-documented flat per-minute rate
    "openai:gpt-4o-transcribe": None,  # token-based pricing, not a flat per-minute rate -- unknown
    "local": 0.0,
}


# ---------------------------------------------------------------------------
# WER / CER -- jiwer is NOT a declared project dependency (checked
# pyproject.toml / uv.lock), so per the dispatch this is a small, self-contained
# normalized edit-distance implementation instead of adding a heavy dep.
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace -- for WER/CER comparison only."""
    lowered = text.strip().lower()
    stripped = _PUNCT_RE.sub("", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _levenshtein(a: list[str], b: list[str]) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, token_b in enumerate(b, start=1):
            cost = 0 if token_a == token_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """Normalized word-level edit distance / reference word count. ``None`` if reference is empty."""
    ref_words = normalize_text(reference).split()
    if not ref_words:
        return None
    hyp_words = normalize_text(hypothesis).split()
    return _levenshtein(ref_words, hyp_words) / len(ref_words)


def char_error_rate(reference: str, hypothesis: str) -> float | None:
    """Normalized char-level edit distance / reference char count. ``None`` if reference is empty."""
    ref_chars = list(normalize_text(reference))
    if not ref_chars:
        return None
    hyp_chars = list(normalize_text(hypothesis))
    return _levenshtein(ref_chars, hyp_chars) / len(ref_chars)


# ---------------------------------------------------------------------------
# Segment collection -- real VAD segments via the production AudioBufferer,
# mirrors scripts/e2e_latency_harness.py's _run_session but only collects raw
# audio (no transcription during collection) so every backend transcribes the
# same audio.
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    session: str
    index: int
    start_ms: int
    end_ms: int
    audio: np.ndarray = field(repr=False)

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.end_ms - self.start_ms) / 1000.0)


def discover_sessions(sessions_dir: Path) -> list[Path]:
    """Return session directories under ``sessions_dir`` that have a ``prospect.wav``."""
    if not sessions_dir.exists():
        return []
    return sorted(p for p in sessions_dir.iterdir() if p.is_dir() and (p / "prospect.wav").exists())


async def _collect_session_segments(session_dir: Path, remaining_budget: int) -> list[Segment]:
    wav = session_dir / "prospect.wav"
    segments: list[Segment] = []
    if remaining_budget <= 0 or not wav.exists():
        return segments

    queue = SharedInferenceQueue(max_size=64)
    stream = ReplayAudioStream(wav, chunk_size_frames=512, real_time=True)
    bufferer = AudioBufferer(audio_stream=stream, queue=queue, priority=0, speaker="prospect", transcribe_live=True)
    stop_event = asyncio.Event()

    async def _watch_eof() -> None:
        while not stream.at_eof:
            await asyncio.sleep(0.01)
        await asyncio.sleep(_EOF_SETTLE_S)
        stop_event.set()

    async def _consume() -> None:
        index = 0
        while True:
            try:
                item: InferenceQueueItem = await asyncio.wait_for(queue.get(), timeout=_QUEUE_POLL_TIMEOUT_S)
            except TimeoutError:
                if stop_event.is_set() and queue.qsize() == 0:
                    return
                continue
            segments.append(
                Segment(
                    session=session_dir.name,
                    index=index,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                    audio=item.audio,
                )
            )
            index += 1
            if len(segments) >= remaining_budget:
                stop_event.set()
                return

    watcher_task = asyncio.create_task(_watch_eof())
    bufferer_task = asyncio.create_task(bufferer.run(stop_event))
    consumer_task = asyncio.create_task(_consume())

    await consumer_task
    watcher_task.cancel()
    bufferer_task.cancel()
    for task in (watcher_task, bufferer_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    return segments


async def collect_segments(sessions_dir: Path, top_n: int) -> tuple[list[Segment], int]:
    sessions = discover_sessions(sessions_dir)
    all_segments: list[Segment] = []
    for session_dir in sessions:
        remaining = top_n - len(all_segments)
        if remaining <= 0:
            break
        all_segments.extend(await _collect_session_segments(session_dir, remaining))
    return all_segments, len(sessions)


# ---------------------------------------------------------------------------
# STT candidates
# ---------------------------------------------------------------------------


@dataclass
class TranscriptResult:
    backend: str
    session: str
    index: int
    text: str = ""
    latency_ms: float | None = None
    error: str | None = None
    duration_s: float = 0.0


class LocalCandidate:
    """Wraps a real ``TranscriptionBackend`` (whisper.cpp resident server or mlx)."""

    kind = "local"

    def __init__(self, name: str, backend: TranscriptionBackend) -> None:
        self.name = name
        self._backend = backend

    async def start(self) -> None:
        await self._backend.start(asyncio.Event())
        await self._backend.warmup()

    async def transcribe(self, segment: Segment) -> str:
        return await self._backend.transcribe(segment.audio)

    async def close(self) -> None:
        await self._backend.stop()

    def server_mode(self) -> str:
        """Describe whether whisper.cpp is running the resident-server fast path.

        ``_server_started`` is a private attribute on ``WhisperCppBackend`` --
        introspected here (report-only, never mutated) so the report can flag an
        unfair reload-per-chunk baseline instead of silently comparing against it.
        """
        started = getattr(self._backend, "_server_started", None)  # noqa: SLF001
        if started is None:
            return "n/a (not whisper.cpp)"
        return "resident whisper-server (HTTP, model stays loaded)" if started else (
            "one-shot whisper-cli (model reloaded per chunk -- NOT a fair cloud comparison)"
        )


class _CloudCandidate:
    """Shared HTTP POST logic for the Groq/OpenAI OpenAI-compatible audio endpoints."""

    kind = "cloud"

    def __init__(self, name: str, url: str, api_key: str, model: str, language: str, timeout_s: float) -> None:
        self.name = name
        self._url = url
        self._api_key = api_key
        self._model = model
        self._language = language
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def transcribe(self, segment: Segment) -> str:
        wav_bytes = encode_wav_bytes(segment.audio, 16000)
        response = await self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
            data={"model": self._model, "language": self._language, "response_format": "json", "temperature": "0"},
        )
        response.raise_for_status()
        payload = response.json()
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        return text.strip() if isinstance(text, str) else ""

    async def close(self) -> None:
        await self._client.aclose()


def build_groq_candidate(api_key: str, language: str, timeout_s: float) -> _CloudCandidate:
    return _CloudCandidate(f"groq:{_GROQ_MODEL}", _GROQ_URL, api_key, _GROQ_MODEL, language, timeout_s)


def build_openai_candidate(api_key: str, model: str, language: str, timeout_s: float) -> _CloudCandidate:
    return _CloudCandidate(f"openai:{model}", _OPENAI_URL, api_key, model, language, timeout_s)


# ---------------------------------------------------------------------------
# Per-candidate run
# ---------------------------------------------------------------------------


async def run_candidate(candidate: Any, segments: list[Segment], *, timeout_s: float) -> list[TranscriptResult]:
    """Transcribe every segment with ``candidate``. One segment failing (timeout,
    HTTP error, provider hiccup) is recorded and the run continues -- a single bad
    segment or an unavailable backend must never crash the whole comparison."""
    results: list[TranscriptResult] = []
    for segment in segments:
        started = time.perf_counter()
        try:
            text = await asyncio.wait_for(candidate.transcribe(segment), timeout=timeout_s)
            latency_ms = (time.perf_counter() - started) * 1000
            results.append(
                TranscriptResult(
                    backend=candidate.name,
                    session=segment.session,
                    index=segment.index,
                    text=text.strip(),
                    latency_ms=latency_ms,
                    duration_s=segment.duration_s,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- report as a failed segment, never crash the run
            results.append(
                TranscriptResult(
                    backend=candidate.name,
                    session=segment.session,
                    index=segment.index,
                    error=str(exc),
                    duration_s=segment.duration_s,
                )
            )
    return results


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


@dataclass
class ExampleDiff:
    session: str
    index: int
    local_text: str
    cloud_text: str
    wer: float | None


@dataclass
class BackendSummary:
    name: str
    kind: str
    n_ok: int
    n_failed: int
    latency: dict[str, float]
    errors_sample: list[str] = field(default_factory=list)
    mean_wer: float | None = None
    mean_cer: float | None = None
    n_compared: int = 0
    cost_usd: float | None = None
    cost_note: str = ""
    verdict: str = ""


@dataclass
class Report:
    sessions_scanned: int
    segments_collected: int
    local_server_mode: str
    backends: list[BackendSummary] = field(default_factory=list)
    example_diffs: dict[str, list[ExampleDiff]] = field(default_factory=dict)
    all_results: list[TranscriptResult] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "sessions_scanned": self.sessions_scanned,
            "segments_collected": self.segments_collected,
            "local_server_mode": self.local_server_mode,
            "backends": [asdict(b) for b in self.backends],
            "example_diffs": {name: [asdict(d) for d in diffs] for name, diffs in self.example_diffs.items()},
            "all_results": [asdict(r) for r in self.all_results],
        }


def _reference_text_by_key(reference_results: list[TranscriptResult]) -> dict[tuple[str, int], str]:
    return {(r.session, r.index): r.text for r in reference_results if r.error is None and r.text}


def build_backend_summary(
    name: str,
    kind: str,
    results: list[TranscriptResult],
    *,
    reference: dict[tuple[str, int], str] | None,
) -> tuple[BackendSummary, list[ExampleDiff]]:
    ok = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
    total_minutes = sum(r.duration_s for r in results) / 60.0
    rate = _USD_PER_MINUTE.get(name)
    cost_usd = rate * total_minutes if rate is not None else None
    cost_note = "list price, not re-verified" if rate is not None else "token-based pricing -- see script header"
    if kind == "local":
        cost_usd = 0.0
        cost_note = "own hardware"

    summary = BackendSummary(
        name=name,
        kind=kind,
        n_ok=len(ok),
        n_failed=len(failed),
        latency=latency_stats(latencies),
        errors_sample=[r.error for r in failed[:3] if r.error],
        cost_usd=cost_usd,
        cost_note=cost_note,
    )

    diffs: list[ExampleDiff] = []
    if reference is not None:
        wers: list[float] = []
        cers: list[float] = []
        for r in ok:
            ref_text = reference.get((r.session, r.index))
            if ref_text is None:
                continue
            wer = word_error_rate(ref_text, r.text)
            cer = char_error_rate(ref_text, r.text)
            if wer is None or cer is None:
                continue
            wers.append(wer)
            cers.append(cer)
            diffs.append(ExampleDiff(session=r.session, index=r.index, local_text=ref_text, cloud_text=r.text, wer=wer))
        if wers:
            summary.mean_wer = sum(wers) / len(wers)
            summary.mean_cer = sum(cers) / len(cers)
            summary.n_compared = len(wers)
        diffs.sort(key=lambda d: d.wer or 0.0, reverse=True)
        diffs = diffs[:_MAX_EXAMPLE_DIFFS]

    summary.verdict = _verdict_line(summary, local_p50=None)
    return summary, diffs


def _verdict_line(summary: BackendSummary, *, local_p50: float | None) -> str:
    if summary.n_ok == 0:
        return "no successful transcriptions -- see errors_sample"
    parts: list[str] = []
    if local_p50 is not None and summary.latency.get("p50"):
        delta = summary.latency["p50"] - local_p50
        direction = "slower" if delta > 0 else "faster"
        parts.append(f"p50 {abs(delta):.0f}ms {direction} than local")
    if summary.mean_wer is not None:
        parts.append(f"WER {summary.mean_wer * 100:.1f}% vs local")
    if summary.cost_usd is not None:
        parts.append(f"${summary.cost_usd:.4f} for this run" if summary.cost_usd > 0 else "free (own hardware)")
    return "; ".join(parts) if parts else "insufficient data for a verdict"


def build_report(
    *,
    sessions_scanned: int,
    segments_collected: int,
    local_server_mode: str,
    results_by_backend: dict[str, list[TranscriptResult]],
    backend_kinds: dict[str, str],
    reference_backend: str | None,
) -> Report:
    reference = None
    if reference_backend is not None and reference_backend in results_by_backend:
        reference = _reference_text_by_key(results_by_backend[reference_backend])

    all_results: list[TranscriptResult] = []
    summaries: list[BackendSummary] = []
    example_diffs: dict[str, list[ExampleDiff]] = {}
    local_p50 = None

    for name, results in results_by_backend.items():
        all_results.extend(results)
        kind = backend_kinds[name]
        use_reference = reference if (kind == "cloud" and name != reference_backend) else None
        summary, diffs = build_backend_summary(name, kind, results, reference=use_reference)
        if name == reference_backend:
            local_p50 = summary.latency.get("p50")
        summaries.append(summary)
        if diffs:
            example_diffs[name] = diffs

    for summary in summaries:
        if summary.kind == "cloud":
            summary.verdict = _verdict_line(summary, local_p50=local_p50)

    return Report(
        sessions_scanned=sessions_scanned,
        segments_collected=segments_collected,
        local_server_mode=local_server_mode,
        backends=summaries,
        example_diffs=example_diffs,
        all_results=all_results,
    )


def _fmt_ms(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f} ms"


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def render_markdown(report: Report) -> str:
    # latency_stats() (reused from eval_shared, see module imports) reports
    # n/p50/p95/mean only -- no min/max -- so the table below sticks to that contract.
    lines: list[str] = [
        "# STT comparison -- local large-v3-turbo vs Groq vs OpenAI cloud whisper",
        "",
        f"**Sessions scanned:** {report.sessions_scanned}  ",
        f"**Segments compared:** {report.segments_collected}  ",
        f"**Local backend server mode:** {report.local_server_mode}",
        "",
        "## Per-backend latency (wall-clock per segment, incl. network for cloud)",
        "",
        "| Backend | n ok | n failed | p50 | p95 | mean |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for b in report.backends:
        lat = b.latency
        lines.append(
            f"| {b.name} | {b.n_ok} | {b.n_failed} | {_fmt_ms(lat.get('p50'))} | "
            f"{_fmt_ms(lat.get('p95'))} | {_fmt_ms(lat.get('mean'))} |"
        )
    lines.append("")

    lines.append("## Transcript quality (pairwise vs local, no ground truth available)")
    lines.append("")
    lines.append("| Backend | n compared | mean WER | mean CER |")
    lines.append("|---|---:|---:|---:|")
    for b in report.backends:
        if b.kind != "cloud":
            continue
        lines.append(f"| {b.name} | {b.n_compared} | {_fmt_pct(b.mean_wer)} | {_fmt_pct(b.mean_cer)} |")
    lines.append("")

    lines.append("## Cost estimate for this run")
    lines.append("")
    lines.append("| Backend | Cost | Note |")
    lines.append("|---|---:|---|")
    for b in report.backends:
        cost = "unknown" if b.cost_usd is None else f"${b.cost_usd:.4f}"
        lines.append(f"| {b.name} | {cost} | {b.cost_note} |")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    for b in report.backends:
        lines.append(f"- **{b.name}**: {b.verdict}")
    lines.append("")

    if report.example_diffs:
        lines.append("## Example diffs (highest-WER segments per cloud backend)")
        lines.append("")
        for name, diffs in report.example_diffs.items():
            lines.append(f"### {name}")
            lines.append("")
            for d in diffs:
                lines.append(f"- `{d.session}#{d.index}` (WER {_fmt_pct(d.wer)})")
                lines.append(f"  - local: {d.local_text!r}")
                lines.append(f"  - {name}: {d.cloud_text!r}")
            lines.append("")

    if any(b.errors_sample for b in report.backends):
        lines.append("## Errors (sample, up to 3 per backend)")
        lines.append("")
        for b in report.backends:
            for err in b.errors_sample:
                lines.append(f"- **{b.name}**: {err}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def run_comparison(args: argparse.Namespace) -> Report:
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    transcriber_config = TranscriberConfig.from_env()
    timeout_s = args.timeout_ms / 1000.0

    sessions_dir = Path(args.sessions_dir)
    segments, sessions_scanned = await collect_segments(sessions_dir, args.top_n)

    requested_backends = {b.strip().lower() for b in args.backends.split(",") if b.strip()}
    results_by_backend: dict[str, list[TranscriptResult]] = {}
    backend_kinds: dict[str, str] = {}
    local_server_mode = "not run"
    reference_backend: str | None = None

    if "local" in requested_backends:
        local_threads_arg = args.local_threads or str(transcriber_config.whisper_cpp_threads)
        thread_counts = [int(t.strip()) for t in local_threads_arg.split(",") if t.strip()]
        if transcriber_config.backend not in {"whisper.cpp", "whisper_cpp"} and len(thread_counts) > 1:
            print(
                f"WARNING: backend={transcriber_config.backend!r} is not whisper.cpp -- "
                "--local-threads comparison only applies to whisper.cpp, running a single variant.",
                file=sys.stderr,
            )
            thread_counts = thread_counts[:1]

        for i, threads in enumerate(thread_counts):
            variant_config = with_overrides(transcriber_config, whisper_cpp_threads=threads)
            name = "local" if i == 0 else f"local(threads={threads})"
            backend = create_backend(
                {
                    "backend": variant_config.backend,
                    "language": variant_config.language,
                    "whisper_cpp_binary": variant_config.whisper_cpp_binary,
                    "whisper_cpp_model_path": variant_config.whisper_cpp_model_path,
                    "whisper_cpp_threads": variant_config.whisper_cpp_threads,
                    "whisper_cpp_server_binary": variant_config.whisper_cpp_server_binary,
                }
            )
            candidate = LocalCandidate(name, backend)
            await candidate.start()
            if i == 0:
                local_server_mode = candidate.server_mode()
                reference_backend = name
            results_by_backend[name] = await run_candidate(candidate, segments, timeout_s=timeout_s)
            backend_kinds[name] = "local"
            await candidate.close()

    if "groq" in requested_backends:
        available, reason = is_provider_available("groq")
        if not available:
            print(f"WARNING: skipping groq -- {reason}", file=sys.stderr)
        else:
            candidate = build_groq_candidate(env("GROQ_API_KEY", "") or "", transcriber_config.language, timeout_s)
            name = candidate.name
            results_by_backend[name] = await run_candidate(candidate, segments, timeout_s=timeout_s)
            backend_kinds[name] = "cloud"
            await candidate.close()

    if "openai" in requested_backends:
        available, reason = is_provider_available("openai")
        if not available:
            print(f"WARNING: skipping openai -- {reason}", file=sys.stderr)
        else:
            api_key = env("OPENAI_API_KEY", "") or ""
            for model in _OPENAI_MODELS:
                candidate = build_openai_candidate(api_key, model, transcriber_config.language, timeout_s)
                name = candidate.name
                candidate_results = await run_candidate(candidate, segments, timeout_s=timeout_s)
                await candidate.close()
                # gpt-4o-transcribe may not be reachable on every account -- if every
                # segment failed for the SAME reason, report it as unavailable rather than
                # a wall of per-segment errors (still keeps the failed rows for the JSON).
                if candidate_results and all(r.error is not None for r in candidate_results):
                    print(
                        f"WARNING: {name} failed on every segment -- likely unavailable for this "
                        f"account. First error: {candidate_results[0].error}",
                        file=sys.stderr,
                    )
                results_by_backend[name] = candidate_results
                backend_kinds[name] = "cloud"

    return build_report(
        sessions_scanned=sessions_scanned,
        segments_collected=len(segments),
        local_server_mode=local_server_mode,
        results_by_backend=results_by_backend,
        backend_kinds=backend_kinds,
        reference_backend=reference_backend,
    )


def _write_reports(report: Report, report_md: Path, report_json: Path) -> None:
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markdown(report), encoding="utf-8")
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report.to_json_dict(), indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=_DEFAULT_SESSIONS_DIR,
        help=f"Root with <session-id>/prospect.wav fixtures. Default: {_DEFAULT_SESSIONS_DIR}",
    )
    parser.add_argument("--top-n", type=int, default=_DEFAULT_TOP_N, help=f"Cap on segments. Default: {_DEFAULT_TOP_N}")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=_DEFAULT_TIMEOUT_MS,
        help=f"Per-segment transcription timeout guard (local + cloud). Default: {_DEFAULT_TIMEOUT_MS}",
    )
    parser.add_argument(
        "--backends",
        type=str,
        default="local,groq,openai",
        help="Comma-separated subset of local,groq,openai. Default: local,groq,openai",
    )
    parser.add_argument(
        "--local-threads",
        type=str,
        default=None,
        help=(
            "Comma-separated WHISPER_CPP_THREADS values to compare (whisper.cpp only), e.g. "
            "'4,8'. The first value is the WER/CER reference. Default: the configured "
            "WHISPER_CPP_THREADS (single variant, no comparison)."
        ),
    )
    parser.add_argument("--report-md", type=Path, default=_DEFAULT_REPORT_MD, help=f"Default: {_DEFAULT_REPORT_MD}")
    parser.add_argument(
        "--report-json", type=Path, default=_DEFAULT_REPORT_JSON, help=f"Default: {_DEFAULT_REPORT_JSON}"
    )
    args = parser.parse_args(argv)

    sessions_dir = Path(args.sessions_dir)
    if not discover_sessions(sessions_dir):
        print(
            f"No session fixtures with prospect.wav found under {sessions_dir}. "
            "This is expected in CI/dispatch worktrees (data/sessions/ is gitignored); "
            "run this in the main checkout with real session recordings.",
            file=sys.stderr,
        )
        return 0

    report = asyncio.run(run_comparison(args))
    _write_reports(report, Path(args.report_md), Path(args.report_json))
    print(render_markdown(report))
    print(f"\nMarkdown report: {args.report_md}")
    print(f"JSON report: {args.report_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
