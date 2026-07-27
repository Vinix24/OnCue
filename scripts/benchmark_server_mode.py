#!/usr/bin/env python3
"""whisper.cpp A/B: one-shot CLI per chunk vs persistent whisper-server.

Proves the live-latency win of server mode. It reuses the exact streaming
harness from ``benchmark_transcription.py`` (1x real-time feeder → single
sequential consumer → audio-aligned latency) and only toggles
``whisper_cpp_server_enabled``. Server mode is pre-warmed before the stream, so
the one-time model load is not charged to steady-state p50/p95 — that matches
the live app, where the server starts at launch, before the call.

Usage:
    .venv/bin/python scripts/benchmark_server_mode.py \
        --call acme-industries-2026-04-09 --stream-seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

from benchmark_transcription import (  # type: ignore[import-not-found]
    FIREFLIES_DIR,
    LANGUAGE,
    SAMPLE_RATE,
    WHISPER_CPP_BINARY,
    WHISPER_CPP_MODEL,
    RealtimeFeeder,
    _stats,
    ensure_ffmpeg,
    run_chunked_stream,
    to_wav_16k_mono,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WHISPER_CPP_SERVER = REPO_ROOT / "vendor" / "whisper.cpp" / "build" / "bin" / "whisper-server"
REPORT_PATH = REPO_ROOT / "claudedocs" / "2026-06-28-WHISPER-SERVER-MODE-LATENCY.md"


def _build_backend(*, server_enabled: bool):
    from sales_copilot.core.config import TranscriberConfig, with_overrides
    from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend

    cfg = with_overrides(
        TranscriberConfig.from_env(),
        language=LANGUAGE,
        whisper_cpp_binary=str(WHISPER_CPP_BINARY),
        whisper_cpp_model_path=str(WHISPER_CPP_MODEL),
        whisper_cpp_server_binary=str(WHISPER_CPP_SERVER),
        whisper_cpp_server_enabled=server_enabled,
        whisper_cpp_timeout_s=60.0,
    )
    return WhisperCppBackend(cfg)


def _transcribe_fn(backend):
    def _fn(audio) -> str:
        end_ms = max(1, int(len(audio) / SAMPLE_RATE * 1000))
        payload = backend.transcribe_chunk(audio, start_ms=0, end_ms=end_ms)
        return "" if payload is None else str(payload.get("text", ""))

    return _fn


def _measure(wav_path: Path, *, server_enabled: bool, chunk_ms: int, stream_seconds: float):
    backend = _build_backend(server_enabled=server_enabled)
    warmup_s = None
    if server_enabled:
        t0 = time.perf_counter()
        asyncio.run(backend.warmup())  # boot + load the server before timing the stream
        warmup_s = time.perf_counter() - t0
    try:
        feeder = RealtimeFeeder(wav_path, max_seconds=stream_seconds)
        segs = run_chunked_stream(feeder, _transcribe_fn(backend), chunk_ms=chunk_ms)
        ttft, p50, p95, n = _stats(segs)
    finally:
        asyncio.run(backend.stop())
    return {"ttft": ttft, "p50": p50, "p95": p95, "n": n, "warmup_s": warmup_s}


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.2f}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", default="acme-industries-2026-04-09")
    parser.add_argument("--stream-seconds", type=float, default=60.0)
    parser.add_argument("--chunk-ms", type=int, action="append", dest="chunk_ms")
    args = parser.parse_args()
    chunk_sizes = args.chunk_ms or [3000, 1000]

    try:
        ensure_ffmpeg()
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 1
    if not WHISPER_CPP_SERVER.exists():
        print(f"FATAL: whisper-server missing: {WHISPER_CPP_SERVER} (run scripts/install_whisper_cpp.sh)")
        return 1

    call_dir = FIREFLIES_DIR / args.call
    mp3 = call_dir / "audio.mp3"
    if not mp3.exists():
        print(f"FATAL: missing audio.mp3 for {args.call}")
        return 1
    tmp_dir = Path(tempfile.gettempdir()) / "sc_bench"
    tmp_dir.mkdir(exist_ok=True)
    wav_path = to_wav_16k_mono(mp3, tmp_dir / f"{args.call}.wav")

    rows: list[tuple[str, dict]] = []
    for chunk_ms in chunk_sizes:
        for server_enabled in (False, True):
            mode = "server" if server_enabled else "CLI (one-shot)"
            label = f"whisper.cpp {mode} @ {chunk_ms}ms"
            print(f"  measuring {label} ({args.stream_seconds:.0f}s @ 1x)...")
            rows.append((label, _measure(
                wav_path, server_enabled=server_enabled, chunk_ms=chunk_ms,
                stream_seconds=args.stream_seconds,
            )))

    lines = [
        "# whisper.cpp server mode — live-latency A/B",
        "",
        f"Generated: 2026-06-28. Call `{args.call}`, first {args.stream_seconds:.0f}s at 1x real-time, "
        "Apple Silicon, `ggml-large-v3-turbo`.",
        "",
        "Same streaming harness as `benchmark_transcription.py` (1x feeder → single sequential "
        "consumer → audio-aligned latency). Server mode is pre-warmed before timing, so the model "
        "load is excluded from p50/p95 (the live server boots at app launch, before the call).",
        "",
        "| Backend | TTFT | latency p50 | latency p95 | segments | warmup |",
        "|---|---|---|---|---|---|",
    ]
    for label, r in rows:
        lines.append(
            f"| {label} | {_fmt(r['ttft'])} | {_fmt(r['p50'])} | {_fmt(r['p95'])} | "
            f"{r['n']} | {_fmt(r['warmup_s'])} |"
        )
    lines.append("")
    report = "\n".join(lines)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nReport written to {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
