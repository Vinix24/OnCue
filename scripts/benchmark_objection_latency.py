#!/usr/bin/env python3
"""Objection-detection latency: fast embedding route vs LLM window classifier.

Quantifies the cold-call fast-path win. Feeds real prospect utterances from a
Fireflies transcript through both routes and times each per-call latency:

- Fast route: ObjectionRouter.classify_async (semantic-router embedding match).
- LLM route:  WindowClassifier.classify (the configured provider, e.g. vertex).

This measures the isolatable per-call latency of each route. The live end-to-end
9-27s additionally stacks the min-chunks + debounce gates on top of the LLM call;
this benchmark strips those to compare the two classification mechanisms directly.

Usage:
    .venv/bin/python scripts/benchmark_objection_latency.py \
        --transcript data/fireflies/acme-industries-2026-04-09/fireflies-ca7cd311.md
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.core.preset import load_preset  # noqa: E402
from sales_copilot.modules.detector.objection_detector import ObjectionRouter  # noqa: E402
from sales_copilot.modules.detector.sliding_window import TranscriptChunk  # noqa: E402
from sales_copilot.modules.detector.window_classifier import WindowClassifier  # noqa: E402

_SPEAKER_LINE = re.compile(r"^\*\*.+?\*\*\s*\*\[\d+:\d+\]\*:\s*(.*)$")


def _utterances(md_path: Path, limit: int) -> list[str]:
    spoken: list[str] = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        match = _SPEAKER_LINE.match(line.strip())
        if match:
            text = match.group(1).strip()
            if len(text) >= 25:  # skip "ja", "oke" filler for a fair per-call measure
                spoken.append(text)
    return spoken[:limit]


def _stats(latencies: list[float]) -> dict:
    if not latencies:
        return {"n": 0}
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    return {
        "n": len(latencies),
        "p50": statistics.median(ordered),
        "mean": statistics.mean(ordered),
        "p95": p95,
        "max": ordered[-1],
    }


async def _measure(utterances: list[str], config: DetectorConfig) -> tuple[dict, dict, int]:
    # -- Fast route (embedding) --
    router = ObjectionRouter(config, language="nl")
    await router.classify_async("warmup zin om het model te laden")  # warm the encoder
    fast_lat: list[float] = []
    matches = 0
    for text in utterances:
        t0 = time.perf_counter()
        match = await router.classify_async(text)
        fast_lat.append(time.perf_counter() - t0)
        if match is not None:
            matches += 1

    # -- LLM route (window classifier) --
    llm_lat: list[float] = []
    try:
        clf = WindowClassifier(config, config.llm_provider, preset=load_preset(config.preset_name))
        await clf.classify("warmup zin", None)  # warm the client/model
        for text in utterances:
            chunk = TranscriptChunk(text=text, speaker="prospect", start_ms=0, end_ms=1000)
            t0 = time.perf_counter()
            await clf.classify(text, chunk)
            llm_lat.append(time.perf_counter() - t0)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] LLM-route kon niet meten ({config.llm_provider}): {type(exc).__name__}: {str(exc)[:160]}")

    return _stats(fast_lat), _stats(llm_lat), matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transcript",
        default="data/fireflies/acme-industries-2026-04-09/fireflies-ca7cd311.md",
    )
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    load_env()
    config = DetectorConfig.from_env()
    md = Path(args.transcript)
    if not md.exists():
        print(f"FATAL: transcript niet gevonden: {md}")
        return 1
    utterances = _utterances(md, args.limit)
    if not utterances:
        print("FATAL: geen bruikbare uitingen in het transcript")
        return 1

    print(f"Meting op {len(utterances)} echte prospect-uitingen uit {md.name}")
    print(f"LLM-provider: {config.llm_provider} ({config.llm_model})\n")

    fast, llm, matches = asyncio.run(_measure(utterances, config))

    def _row(name: str, s: dict) -> str:
        if not s.get("n"):
            return f"| {name} | — | — | — | — |"
        return (f"| {name} | {s['p50']*1000:.0f} ms | {s['mean']*1000:.0f} ms | "
                f"{s['p95']*1000:.0f} ms | {s['max']*1000:.0f} ms |")

    print("| Route | p50 | mean | p95 | max |")
    print("|---|---|---|---|---|")
    print(_row("Snelle route (embedding)", fast))
    print(_row(f"LLM-route ({config.llm_provider})", llm))
    print(f"\nSnelle route matchte {matches}/{fast.get('n', 0)} uitingen als bezwaar.")
    if fast.get("n") and llm.get("n"):
        print(f"Snelheidsfactor (LLM p50 / fast p50): {llm['p50'] / fast['p50']:.0f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
