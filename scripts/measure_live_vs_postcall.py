#!/usr/bin/env python3
"""Live path against post-call transcription on the reviewed entity set.

Answers: does the live path (audio in short pieces through the VAD buffer, as during a
call) yield as many correct entities as a post-call transcription of the same fragment
(``scripts/transcribe_file.py`` ``_transcribe``, 30 s windows)? Same model, same
vocabulary, same normalization layer. Only the path differs.

Live path, per fragment (end-to-end through the real server):

1. Build a replay directory: the fragment as ``prospect.wav`` plus a silent ``self.wav``
   of the same length (16 kHz mono).
2. Start ``python -m sales_copilot`` with ``AUDIO_CAPTURE_METHOD=replay`` at speed 1.0 on a
   free loopback port, with every LLM task set to ``none``.
3. Start the call over HTTP like the dashboard does (``/api/start-call``), wait for the
   audio to play out in real time, end it (``/api/end-call``).
4. Read ``full_transcript`` from the written report. That text has been through
   ``InferenceWorker`` and so through ``normalize()`` already: the raw text before the
   layer is not available on this path, only the "after" score is.
5. Write the result per fragment immediately, so an aborted run costs nothing.

Scoring reuses ``scripts/measure_entities_per_model.score_variant`` (same cases, same
windows). The post-call arm is read from the cached transcripts of that harness
(``--postcall-dir``) and scored through the same function, restricted to the fragments
the live path has finished.

The difference between the paths is the WER of the live transcript against the post-call
transcript (both after the layer). It says how far the paths diverge, not which one is
right.

Run from the repository root as a module (``PYTHONPATH=src python -m
scripts.measure_live_vs_postcall ...``) so ``scripts`` and ``sales_copilot`` import.

Raw transcripts and per-case outcomes contain customer names. They go to ``--out-dir``
(expected to be gitignored). Stdout carries aggregated counts only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sales_copilot.modules.transcriber.normalize import (
    load_normalization_lists,
    normalize,
)
from scripts import measure_entities_per_model as m
from scripts import measure_transcript_normalization as base
from scripts.benchmark_transcription import compute_wer, normalize_text

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "live-vs-postcall-measurement"
SAMPLE_RATE = 16000
LLM_TASKS = (
    "DETECTOR_CONFIRM",
    "WINDOW_CLASSIFIER",
    "PHASE",
    "SUGGESTIONS",
    "SUMMARY",
    "SCRIPT_TRACKING",
    "SLIDES",
    "REPORT_TERMS",
    "REPORT",
)
OPERATOR_WHISPER_ENV = {
    "WHISPER_BACKEND": "whisper.cpp",
    "WHISPER_MODEL": "large-v3-turbo",
    "WHISPER_CPP_THREADS": "8",
    "WHISPER_LANGUAGE": "nl",
    "WHISPER_VOCABULARY_ENABLED": "true",
    "WHISPER_MAX_BUFFER_SECONDS": "12.0",
    "WHISPER_MAX_BUFFER_HARD_SECONDS": "20.0",
    "WHISPER_SILENCE_GAP_SECONDS": "2.0",
    "WHISPER_MIN_SEGMENT_SECONDS": "3.0",
    "WHISPER_LONG_SILENCE_ESCAPE_SECONDS": "4.0",
}
_LOOPBACK = ("127.0.0.1", "[::1]", "localhost", "*")

LiveFn = Callable[[Path, str], "LiveResult"]


@dataclass
class LiveResult:
    entries: list[dict[str, Any]]
    seconds: float
    outbound: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Replay directory
# ---------------------------------------------------------------------------


def build_replay_dir(fragment_wav: Path, dst: Path) -> Path:
    """``prospect.wav`` = the fragment, ``self.wav`` = silence of the same length."""
    dst.mkdir(parents=True, exist_ok=True)
    with wave.open(str(fragment_wav), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())
    if params.nchannels != 1 or params.framerate != SAMPLE_RATE or params.sampwidth != 2:
        raise ValueError(
            f"fragment must be 16 kHz mono 16-bit PCM, got {params.nchannels} ch, "
            f"{params.framerate} Hz, {params.sampwidth * 8}-bit: {fragment_wav.name}"
        )
    with wave.open(str(dst / "prospect.wav"), "wb") as out:
        out.setparams(params)
        out.writeframes(frames)
    with wave.open(str(dst / "self.wav"), "wb") as out:
        out.setparams(params)
        out.writeframes(b"\x00" * len(frames))
    return dst


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as src:
        return src.getnframes() / src.getframerate()


# ---------------------------------------------------------------------------
# Environment of the live run
# ---------------------------------------------------------------------------


def build_live_env(
    base_env: dict[str, str],
    *,
    replay_dir: Path,
    port: int,
    model_path: str,
    whisper_binary: str,
    whisper_server_binary: str | None,
) -> dict[str, str]:
    """Explicit environment for the measured server. Never reads a ``.env``.

    Starts from ``base_env`` minus every variable that could carry a key or point at a
    cloud lane, then sets the operator's live settings and switches every LLM task off.
    """
    env = {
        k: v
        for k, v in base_env.items()
        if not re.search(r"(API_KEY|TOKEN|SECRET|CREDENTIALS)", k) and not k.startswith("WHISPER_")
    }
    env.update(OPERATOR_WHISPER_ENV)
    env["WHISPER_CPP_MODEL_PATH"] = model_path
    env["WHISPER_CPP_BINARY"] = whisper_binary
    if whisper_server_binary:
        env["WHISPER_CPP_SERVER_BINARY"] = whisper_server_binary
    for task in LLM_TASKS:
        env[f"{task}_LLM_PROVIDER"] = "none"
    env.update(
        {
            "AUDIO_CAPTURE_METHOD": "replay",
            "REPLAY_SESSION_DIR": str(replay_dir),
            "REPLAY_SPEED": "1.0",
            "WS_HUB_HOST": "127.0.0.1",
            "WS_HUB_PORT": str(port),
            "SHUTDOWN_TOKEN": TOKEN,
            "SALES_COPILOT_NO_BROWSER": "1",
            "CONSENT_TIER": "off",
            "REPORT_REDACT_PII": "false",
            "RECORD_AUDIO": "false",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    return env


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


# ---------------------------------------------------------------------------
# Server driving
# ---------------------------------------------------------------------------


def _http(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> int:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Sales-Copilot-Token", TOKEN)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def start_call_payload() -> dict[str, Any]:
    return {
        "config": {
            "screen_mode": "single",
            "modules": {
                "talk_time": False,
                "transcript": True,
                "pain_points": False,
                "presentation": False,
                "post_call_report": True,
            },
            "transcript": {"language": "nl"},
            "prospect": {"name": "Measurement", "company": "Measurement"},
        }
    }


def outbound_connections(pid: int) -> list[str]:
    """Non-loopback network endpoints of ``pid`` and its direct children (``lsof``)."""
    pids = [pid]
    children = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
    pids += [int(p) for p in children.stdout.split()]
    found: set[str] = set()
    for target in pids:
        out = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(target), "-i"], capture_output=True, text=True, check=False
        ).stdout
        for line in out.splitlines()[1:]:
            match = re.search(r"->(\S+)", line)
            if match and not match.group(1).startswith(_LOOPBACK):
                found.add(match.group(1))
    return sorted(found)


def newest_report(reports_dir: Path, since: float) -> Path | None:
    candidates = [
        p for p in reports_dir.glob("*_report.json") if p.stat().st_mtime >= since
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def read_report_entries(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("full_transcript")
    if not isinstance(entries, list):
        raise ValueError(f"report has no full_transcript list: {path.name}")
    return entries


def run_live_fragment(
    wav: Path,
    key: str,
    *,
    work_dir: Path,
    reports_dir: Path,
    model_path: str,
    whisper_binary: str,
    whisper_server_binary: str | None,
    startup_timeout_s: float = 120.0,
    tail_s: float = 25.0,
    report_timeout_s: float = 90.0,
) -> LiveResult:
    """Play one fragment through the real server in real time and return the report entries."""
    replay_dir = build_replay_dir(wav, work_dir / "replay" / key)
    duration = wav_seconds(wav)
    port = free_port()
    env = build_live_env(
        dict(os.environ),
        replay_dir=replay_dir,
        port=port,
        model_path=model_path,
        whisper_binary=whisper_binary,
        whisper_server_binary=whisper_server_binary,
    )
    log_path = work_dir / "logs" / f"{key}.server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    wall_start = time.time()
    outbound: set[str] = set()
    with log_path.open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "sales_copilot"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + startup_timeout_s
            while not port_in_use(port):
                if server.poll() is not None:
                    raise RuntimeError(f"server exited during startup, see {log_path}")
                if time.monotonic() > deadline:
                    raise RuntimeError(f"server not listening on port {port} after {startup_timeout_s:.0f}s")
                time.sleep(0.5)
            base_url = f"http://127.0.0.1:{port}"
            status = _http("POST", f"{base_url}/api/start-call", start_call_payload())
            if status != 200:
                raise RuntimeError(f"/api/start-call returned {status}")
            play_start = time.monotonic()
            half = play_start + duration / 2
            while time.monotonic() < half:
                time.sleep(1.0)
            outbound.update(outbound_connections(server.pid))
            while time.monotonic() < play_start + duration + tail_s:
                time.sleep(1.0)
            outbound.update(outbound_connections(server.pid))
            status = _http("POST", f"{base_url}/api/end-call")
            if status != 200:
                raise RuntimeError(f"/api/end-call returned {status}")
            report_deadline = time.monotonic() + report_timeout_s
            report = None
            while report is None and time.monotonic() < report_deadline:
                report = newest_report(reports_dir, wall_start)
                time.sleep(1.0)
            if report is None:
                raise RuntimeError(f"no report written within {report_timeout_s:.0f}s, see {log_path}")
            time.sleep(2.0)  # the post-call rewrite replaces the file atomically
            entries = read_report_entries(report)
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    return LiveResult(entries=entries, seconds=time.monotonic() - started, outbound=sorted(outbound))


# ---------------------------------------------------------------------------
# Conversion to the scorer's format, comparison
# ---------------------------------------------------------------------------


def entries_to_timestamped(entries: list[dict[str, Any]], speaker: str = "prospect") -> str:
    """``[MM:SS] text`` lines (start relative to the fragment) as the #283 scorer expects."""
    lines: list[str] = []
    for entry in sorted(entries, key=lambda e: int(e.get("start_ms") or 0)):
        if (entry.get("speaker") or "") != speaker:
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        seconds = max(0, int(entry.get("start_ms") or 0) // 1000)
        lines.append(f"[{seconds // 60:02d}:{seconds % 60:02d}] {text}")
    return "\n".join(lines)


def strip_timestamps(text: str) -> str:
    return " ".join(chunk for _, chunk in m.parse_timestamped_transcript(text))


def path_difference(live_text: str, postcall_text: str, normalize_fn: base.NormalizeFn) -> float | None:
    """WER of the live transcript against the post-call one, both after the layer.

    None when the post-call side is empty. Not an error rate: neither side is the truth.
    """
    reference = normalize_fn(strip_timestamps(postcall_text))[0]
    hypothesis = strip_timestamps(live_text)
    if not reference.strip():
        return None
    try:
        return compute_wer(reference, hypothesis)
    except ModuleNotFoundError:
        return word_edit_rate(normalize_text(reference), normalize_text(hypothesis))


def wer_engine() -> str:
    if importlib.util.find_spec("jiwer") is None:
        return "eigen woord-Levenshtein (jiwer niet geinstalleerd)"
    return "jiwer"


def word_edit_rate(reference: str, hypothesis: str) -> float:
    """Word-level edit distance over reference length: the same quantity as ``jiwer.wer``."""
    ref, hyp = reference.split(), hypothesis.split()
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1] / len(ref)


@dataclass
class DirectionCounts:
    live_only_good: int
    postcall_only_good: int
    both_good: int
    both_wrong: int


def compare_outcomes(live: m.VariantScore, postcall: m.VariantScore) -> DirectionCounts:
    pairs = [
        (a, b)
        for a, b in zip(live.outcomes, postcall.outcomes, strict=True)
        if a.scored and b.scored
    ]
    return DirectionCounts(
        live_only_good=sum(a.present_after and not b.present_after for a, b in pairs),
        postcall_only_good=sum(b.present_after and not a.present_after for a, b in pairs),
        both_good=sum(a.present_after and b.present_after for a, b in pairs),
        both_wrong=sum(not a.present_after and not b.present_after for a, b in pairs),
    )


def render_table(
    live: m.VariantScore,
    postcall: m.VariantScore,
    differences: dict[str, float | None],
    seconds_by_key: dict[str, float],
) -> str:
    lines = [
        "| fragment | verschil tussen de paden (WER) | live-rekentijd (s) |",
        "|---|---|---|",
    ]
    for key in sorted(differences):
        diff = differences[key]
        shown = f"{diff:.3f}" if diff is not None else "n.v.t."
        lines.append(f"| {key} | {shown} | {seconds_by_key.get(key, 0.0):.0f} |")
    values = [d for d in differences.values() if d is not None]
    mean = sum(values) / len(values) if values else 0.0
    lines += [
        "",
        f"Gemiddeld verschil tussen de paden: {mean:.3f} over {len(values)} fragmenten.",
        "",
        "| pad | gescoord | goed voor de laag | goed na de laag |",
        "|---|---|---|---|",
        f"| live | {live.cases_scored}/{live.cases_total} | n.v.t. (ruwe tekst niet beschikbaar) | "
        f"{live.after_hits}/{live.cases_scored} = {live.score_after:.3f} |",
        f"| achteraf | {postcall.cases_scored}/{postcall.cases_total} | "
        f"{postcall.before_hits}/{postcall.cases_scored} = {postcall.score_before:.3f} | "
        f"{postcall.after_hits}/{postcall.cases_scored} = {postcall.score_after:.3f} |",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_fragments(
    keys: list[str],
    audio_by_key: dict[str, Path],
    out_dir: Path,
    live_fn: LiveFn,
) -> dict[str, float]:
    """Live-run every fragment that is not cached yet. Each result is written at once."""
    live_dir = out_dir / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    seconds_by_key: dict[str, float] = {}
    for key in keys:
        text_path = live_dir / f"{key}.txt"
        meta_path = live_dir / f"{key}.meta.json"
        if text_path.exists() and meta_path.exists():
            seconds_by_key[key] = float(json.loads(meta_path.read_text(encoding="utf-8")).get("seconds", 0.0))
            continue
        result = live_fn(audio_by_key[key], key)
        (live_dir / f"{key}.entries.json").write_text(
            json.dumps(result.entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        text_path.write_text(entries_to_timestamped(result.entries), encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {"seconds": result.seconds, "segments": len(result.entries), "outbound": result.outbound}
            ),
            encoding="utf-8",
        )
        seconds_by_key[key] = result.seconds
        print(f"  [live] {key}: {len(result.entries)} segments, {result.seconds:.0f}s", flush=True)
    return seconds_by_key


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--tuning-set", required=True, type=Path)
    parser.add_argument("--listening-list", required=True, type=Path)
    parser.add_argument("--fragments-dir", required=True, type=Path, help="Directory with <key>.wav fragments")
    parser.add_argument("--postcall-dir", required=True, type=Path, help="Cached post-call <key>.txt transcripts")
    parser.add_argument("--out-dir", required=True, type=Path, help="Gitignored directory for transcripts and outcomes")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--whisper-cpp-binary", default="/opt/homebrew/bin/whisper-cli")
    parser.add_argument("--whisper-cpp-server-binary", default=None)
    parser.add_argument("--reports-dir", type=Path, default=REPO_ROOT / "data" / "reports")
    parser.add_argument("--only", nargs="*", default=None, help="Fragment keys to run (default: all with audio)")
    parser.add_argument("--score-only", action="store_true", help="Skip live runs, score what is cached")
    parser.add_argument(
        "--normalization-config", type=Path, default=REPO_ROOT / "config" / "transcript_normalization.yaml"
    )
    parser.add_argument("--window-seconds", type=int, default=m.DEFAULT_WINDOW_SECONDS)
    return parser


def main(argv: list[str] | None = None, live_fn: LiveFn | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    lists = load_normalization_lists(args.normalization_config)

    def normalize_fn(text: str) -> tuple[str, list[tuple[str, str]]]:
        new_text, replacements = normalize(text, lists)
        return new_text, [(src, dst) for src, dst, _rule in replacements]

    def identity_fn(text: str) -> tuple[str, list[tuple[str, str]]]:
        return text, []

    cases = base.parse_tuning_set(args.tuning_set)
    fragments = {m._key(f.name): f for f in m.parse_listening_list(args.listening_list)}
    audio_by_key = {
        key: args.fragments_dir / f"{key}.wav"
        for key in fragments
        if (args.fragments_dir / f"{key}.wav").is_file()
    }
    missing = sorted(set(fragments) - set(audio_by_key))
    keys = [k for k in audio_by_key if args.only is None or k in args.only]

    if live_fn is None:
        for port in (8760, 8761, 8762):
            if port_in_use(port):
                print(f"note: port {port} is in use (operator server); the live run takes a free port.")
        reports_dir = args.reports_dir

        def live_fn(wav: Path, key: str) -> LiveResult:
            return run_live_fragment(
                wav,
                key,
                work_dir=args.out_dir,
                reports_dir=reports_dir,
                model_path=args.model_path,
                whisper_binary=args.whisper_cpp_binary,
                whisper_server_binary=args.whisper_cpp_server_binary,
            )

    if args.score_only:
        seconds_by_key = {}
        for key in keys:
            meta = args.out_dir / "live" / f"{key}.meta.json"
            if meta.exists():
                seconds_by_key[key] = float(json.loads(meta.read_text(encoding="utf-8")).get("seconds", 0.0))
    else:
        seconds_by_key = run_fragments(keys, audio_by_key, args.out_dir, live_fn)

    live_texts: dict[str, str] = {}
    postcall_texts: dict[str, str] = {}
    for key in keys:
        live_path = args.out_dir / "live" / f"{key}.txt"
        post_path = args.postcall_dir / f"{key}.txt"
        if live_path.exists() and post_path.exists():
            live_texts[key] = live_path.read_text(encoding="utf-8")
            postcall_texts[key] = post_path.read_text(encoding="utf-8")

    live_variant = m.AsrVariant("live", "large-v3-turbo", True)
    post_variant = m.AsrVariant("achteraf", "large-v3-turbo", True)
    # The live text has been through the layer inside InferenceWorker: scoring it with the
    # identity function measures what the product delivered, without applying the layer twice.
    live_score = m.score_variant(
        live_variant, cases, fragments, live_texts, identity_fn,
        seconds=sum(seconds_by_key.get(k, 0.0) for k in live_texts), window_s=args.window_seconds,
    )
    post_score = m.score_variant(
        post_variant, cases, fragments, postcall_texts, normalize_fn, window_s=args.window_seconds
    )
    differences = {
        key: path_difference(live_texts[key], postcall_texts[key], normalize_fn) for key in live_texts
    }
    directions = compare_outcomes(live_score, post_score)

    (args.out_dir / "live-outcomes.json").write_text(
        json.dumps([asdict(o) for o in live_score.outcomes], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "postcall-outcomes.json").write_text(
        json.dumps([asdict(o) for o in post_score.outcomes], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.out_dir / "summary.json").write_text(
        json.dumps(
            {
                "differences": differences,
                "directions": asdict(directions),
                "fragments_live": sorted(live_texts),
                "fragments_missing_audio": missing,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(render_table(live_score, post_score, differences, seconds_by_key))
    print(
        f"\nLive goed en achteraf fout: {directions.live_only_good}; "
        f"achteraf goed en live fout: {directions.postcall_only_good}; "
        f"beide goed: {directions.both_good}; beide fout: {directions.both_wrong}"
    )
    print(
        f"Verschil tussen de paden berekend met: {wer_engine()}\n"
        f"Fragmenten in luisterlijst: {len(fragments)}; met audio: {len(audio_by_key)}; "
        f"zonder audio: {len(missing)}; live klaar: {len(live_texts)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
