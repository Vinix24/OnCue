#!/usr/bin/env python3
"""Latency measurement for the live suggestion path (llm-routering-per-taak, D1).

Drives the real suggestion path -- ``SuggestionLLMClient`` from
``sales_copilot.modules.detector.suggestions`` (system prompt, user prompt, ``LLMClient`` from
``core/llm_client.py``, ``astream`` and ``acreate``) -- against a fixed prospect fragment taken
from the replay-fixture dialogue script (``tests/operator_scripts/scripted_sales_call_nl.md``,
the same V:/P: script format ``scripts/build_replay_fixture.sh`` renders to audio). It measures
wall-clock latency per call for each candidate, in streaming and non-streaming mode.

Why the calls go to ``self._llm`` and not through ``SuggestionLLMClient.suggest()``: ``suggest()``
swallows every exception and returns ``[]``, so a failed call would be indistinguishable from an
empty answer. This script issues the same ``acreate`` / ``astream`` calls with the same kwargs so
the error type of each failure is recorded.

Candidates:
  haiku   openrouter  anthropic/claude-haiku-4.5   (current default)
  qwen    openrouter  qwen/qwen3-30b-a3b           provider pinned to DeepInfra, thinking off
  gemini  vertex      gemini-2.5-flash             genai structured outputs, no temperature

The per-call timeout is 30 s so the real latency is measured, not the local LLM_TIMEOUT_MS.
API keys come from the process environment (load the main checkout's ``.env`` in the shell);
this script never reads, prints or stores them.

Usage:
    set -a; . /path/to/.env; set +a
    PYTHONPATH=src .venv/bin/python scripts/measure_suggestion_latency.py --out /tmp/latency.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_client import build_create_partial
from sales_copilot.core.llm_routing import task_max_output_tokens
from sales_copilot.core.thinking_policy import ThinkingPolicy
from sales_copilot.modules.detector.suggestions import (
    SUGGESTIONS_MAX_CONTEXT_CHARS,
    SUGGESTIONS_MAX_CONTEXT_LINES,
    SuggestionLLMClient,
    SuggestionResponse,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAGMENT = REPO_ROOT / "tests" / "operator_scripts" / "scripted_sales_call_nl.md"
DEFAULT_RUNS = 30
CALL_TIMEOUT_MS = 30_000
# Stop a (candidate, mode) early after this many failures in a row: a bad key or an exhausted
# quota would otherwise burn 30 x 30 s on calls that cannot succeed.
MAX_CONSECUTIVE_FAILURES = 5
MODES = ("streaming", "non-streaming")
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,}|Bearer\s+\S+)")


@dataclass(frozen=True)
class Candidate:
    key: str
    provider: str
    model: str
    thinking: ThinkingPolicy | None = None
    provider_pin: tuple[str, ...] | None = None
    note: str = ""


CANDIDATES: dict[str, Candidate] = {
    "haiku": Candidate("haiku", "openrouter", "anthropic/claude-haiku-4.5"),
    "qwen": Candidate(
        "qwen",
        "openrouter",
        "qwen/qwen3-30b-a3b",
        thinking=ThinkingPolicy(name="no-think", thinking_on=False),
        provider_pin=("DeepInfra",),
        note="provider pinned to DeepInfra (fp8), allow_fallbacks=false, enable_thinking=false",
    ),
    "gemini": Candidate(
        "gemini",
        "vertex",
        "gemini-2.5-flash",
        note="genai has no token streaming through LLMClient.astream: streaming falls back to acreate",
    ),
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without network)
# ---------------------------------------------------------------------------


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least ``pct`` % of samples at or below it."""
    if not values:
        return None
    if not 0 < pct <= 100:
        raise ValueError(f"pct must be in (0, 100], got {pct}")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def error_rate(errors: int, attempts: int) -> float:
    return errors / attempts if attempts else 0.0


def scrub(message: str, limit: int = 200) -> str:
    """Truncate an error message and mask anything that looks like a credential."""
    return _SECRET_RE.sub("<redacted>", message)[:limit]


@dataclass
class RunResult:
    latency_ms: float | None = None
    ttft_ms: float | None = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass
class ModeStats:
    candidate: str
    mode: str
    attempts: int = 0
    skipped: int = 0
    results: list[RunResult] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        ok = [r for r in self.results if r.error_type is None]
        failed = [r for r in self.results if r.error_type is not None]
        latencies = [r.latency_ms for r in ok if r.latency_ms is not None]
        ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
        error_types: dict[str, int] = {}
        for r in failed:
            error_types[r.error_type or "unknown"] = error_types.get(r.error_type or "unknown", 0) + 1
        return {
            "candidate": self.candidate,
            "mode": self.mode,
            "attempts": self.attempts,
            "ok": len(ok),
            "errors": len(failed),
            "skipped_after_abort": self.skipped,
            "error_rate": error_rate(len(failed), self.attempts),
            "error_types": error_types,
            "first_error_message": failed[0].error_message if failed else None,
            "median_ms": median(latencies),
            "p95_ms": percentile(latencies, 95),
            "min_ms": min(latencies) if latencies else None,
            "max_ms": max(latencies) if latencies else None,
            "ttft_median_ms": median(ttfts) if self.mode == "streaming" else None,
            "ttft_p95_ms": percentile(ttfts, 95) if self.mode == "streaming" else None,
            "measured": bool(latencies),
        }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure suggestion-path latency per LLM candidate.")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="runs per candidate and mode (default 30)")
    parser.add_argument(
        "--candidates",
        default=",".join(CANDIDATES),
        help=f"comma-separated subset of: {', '.join(CANDIDATES)} (default: all)",
    )
    parser.add_argument(
        "--fragment",
        type=Path,
        default=DEFAULT_FRAGMENT,
        help="dialogue script with P: prospect lines (default: scripted_sales_call_nl.md)",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the full JSON result to this path")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    names = [c.strip() for c in args.candidates.split(",") if c.strip()]
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown or not names:
        parser.error(f"unknown candidate(s) {unknown or args.candidates!r}; choose from {', '.join(CANDIDATES)}")
    args.candidates = list(dict.fromkeys(names))
    return args


def load_fragment(path: Path) -> list[str]:
    """Return the prospect lines of a dialogue script, capped like ``SuggestionEngine`` does.

    Takes the first ``SUGGESTIONS_MAX_CONTEXT_LINES`` ``P:`` lines and drops the oldest ones
    until the ``SUGGESTIONS_MAX_CONTEXT_CHARS`` budget holds, mirroring
    ``SuggestionEngine._build_transcript_context``.
    """
    prospect = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("P:") and line[2:].strip():
            prospect.append(" ".join(line[2:].split()))
    if not prospect:
        raise ValueError(f"no 'P:' prospect lines found in {path}")
    window = prospect[:SUGGESTIONS_MAX_CONTEXT_LINES]
    context: list[str] = []
    total = 0
    for line in reversed(window):
        total += len(line)
        if total > SUGGESTIONS_MAX_CONTEXT_CHARS:
            break
        context.append(line)
    return list(reversed(context))


def format_table(summaries: Sequence[dict[str, Any]]) -> str:
    header = (
        f"{'candidate':<8} {'mode':<14} {'n':>3} {'ok':>3} {'err%':>6} "
        f"{'median':>8} {'p95':>8} {'ttft50':>8} {'ttft95':>8}  errors"
    )

    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}"

    lines = [header, "-" * len(header)]
    for s in summaries:
        errors = ", ".join(f"{k}x{v}" for k, v in s["error_types"].items()) or "-"
        if not s["measured"]:
            errors = f"NIET GEMETEN: {errors}"
        lines.append(
            f"{s['candidate']:<8} {s['mode']:<14} {s['attempts']:>3} {s['ok']:>3} "
            f"{s['error_rate'] * 100:>5.1f}% {ms(s['median_ms']):>8} {ms(s['p95_ms']):>8} "
            f"{ms(s['ttft_median_ms']):>8} {ms(s['ttft_p95_ms']):>8}  {errors}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _pin_provider(create: Callable[..., Any], order: Sequence[str]) -> Callable[..., Any]:
    """Wrap an instructor create callable so OpenRouter routing is pinned.

    Merges ``provider`` into the ``extra_body`` the seam already built (thinking policy) instead
    of replacing it.
    """

    def pinned(**kwargs: Any) -> Any:
        extra = dict(kwargs.get("extra_body") or {})
        extra["provider"] = {"order": list(order), "allow_fallbacks": False}
        kwargs["extra_body"] = extra
        return create(**kwargs)

    return pinned


def build_suggestion_client(candidate: Candidate) -> SuggestionLLMClient:
    config = DetectorConfig(
        llm_provider=candidate.provider,
        llm_model=candidate.model,
        llm_timeout_ms=CALL_TIMEOUT_MS,
    )
    client = SuggestionLLMClient(config)
    if candidate.provider_pin and client._llm._create is not None:
        llm = client._llm
        llm._create = _pin_provider(llm._create, candidate.provider_pin)
        partial = build_create_partial(llm._client, llm.provider)
        llm._create_partial = _pin_provider(partial, candidate.provider_pin) if partial else None
    return client


def _call_kwargs(client: SuggestionLLMClient, candidate: Candidate, user_text: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": client.config.llm_model,
        "system_prompt": client.system_prompt,
        "user_text": user_text,
        "response_model": SuggestionResponse,
        "temperature": client.config.llm_temperature,
        "allow_local": True,
        "max_tokens": client.resolved.output_limit(candidate.thinking),
    }
    if candidate.thinking is not None:
        kwargs["thinking"] = candidate.thinking
    return kwargs


async def _one_run(client: SuggestionLLMClient, candidate: Candidate, user_text: str, mode: str) -> RunResult:
    kwargs = _call_kwargs(client, candidate, user_text)
    start = time.perf_counter()
    try:
        if mode == "streaming":
            last = None
            async for partial in client._llm.astream(**kwargs):
                last = partial
            elapsed = (time.perf_counter() - start) * 1000
            ttft = client._llm.last_ttft_ms
        else:
            last = await client._llm.acreate(**kwargs)
            elapsed = (time.perf_counter() - start) * 1000
            ttft = None
    except Exception as exc:  # vnx-silent-except: recorded in the result, never hidden
        return RunResult(error_type=type(exc).__name__, error_message=scrub(str(exc)))
    if last is None or not [q for q in (last.questions or []) if isinstance(q, str) and q.strip()]:
        return RunResult(error_type="EmptyResult", error_message="call returned no usable questions")
    return RunResult(latency_ms=elapsed, ttft_ms=ttft)


async def measure_candidate(
    candidate: Candidate, user_text_builder: Callable[[SuggestionLLMClient], str], runs: int
) -> list[ModeStats]:
    try:
        client = build_suggestion_client(candidate)
    except Exception as exc:  # vnx-silent-except: candidate reported as not measured
        message = scrub(str(exc))
        out = []
        for mode in MODES:
            stats = ModeStats(candidate.key, mode, attempts=1)
            stats.results.append(RunResult(error_type=type(exc).__name__, error_message=message))
            out.append(stats)
        return out
    user_text = user_text_builder(client)
    all_stats: list[ModeStats] = []
    for mode in MODES:
        stats = ModeStats(candidate.key, mode)
        consecutive = 0
        for i in range(runs):
            result = await _one_run(client, candidate, user_text, mode)
            stats.attempts += 1
            stats.results.append(result)
            consecutive = consecutive + 1 if result.error_type else 0
            marker = "ok " if result.error_type is None else f"ERR {result.error_type}"
            took = "" if result.latency_ms is None else f" {result.latency_ms:.0f}ms"
            print(f"  [{candidate.key}/{mode}] {i + 1}/{runs} {marker}{took}", file=sys.stderr, flush=True)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                stats.skipped = runs - stats.attempts
                print(f"  [{candidate.key}/{mode}] aborted after {consecutive} consecutive failures", file=sys.stderr)
                break
        all_stats.append(stats)
    return all_stats


async def run_all(args: argparse.Namespace, lines: list[str]) -> dict[str, Any]:
    started = datetime.now(UTC)
    summaries: list[dict[str, Any]] = []
    for key in args.candidates:
        candidate = CANDIDATES[key]
        print(f"candidate {key}: {candidate.provider} {candidate.model}", file=sys.stderr, flush=True)
        for stats in await measure_candidate(candidate, lambda c: c._user_prompt(lines), args.runs):
            summaries.append(stats.summary())
    return {
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "runs_per_mode": args.runs,
        "call_timeout_ms": CALL_TIMEOUT_MS,
        "max_output_tokens": task_max_output_tokens("suggestions"),
        "fragment": {
            "path": str(args.fragment),
            "sha256": hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16],
            "lines": len(lines),
            "chars": sum(len(line) for line in lines),
        },
        "candidates": {
            k: {
                "provider": CANDIDATES[k].provider,
                "model": CANDIDATES[k].model,
                "provider_pin": list(CANDIDATES[k].provider_pin) if CANDIDATES[k].provider_pin else None,
                "note": CANDIDATES[k].note,
            }
            for k in args.candidates
        },
        "results": summaries,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lines = load_fragment(args.fragment)
    report = asyncio.run(run_all(args, lines))
    print()
    frag = report["fragment"]
    print(f"fragment: {frag['path']} ({frag['lines']} lines, sha256 {frag['sha256']})")
    print(
        f"run: {report['started_utc']} .. {report['finished_utc']} UTC, "
        f"{args.runs} runs per candidate and mode, timeout {CALL_TIMEOUT_MS} ms"
    )
    print(format_table(report["results"]))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.out.with_suffix(args.out.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(args.out)  # vnx-atomic-write: tmp + replace
        print(f"json: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
