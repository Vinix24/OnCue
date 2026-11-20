#!/usr/bin/env python3
"""Replay a recorded dialogue through the real live path and count hub events per channel.

Starts ``python -m sales_copilot`` as a child (AUDIO_CAPTURE_METHOD=replay, LLM_PROVIDER=none),
subscribes to every hub channel, POSTs /api/start-call, records events for ``--wait`` seconds,
POSTs /api/end-call and stops only the child it started. No cloud call, no API key.

    python scripts/measure_live_detection.py --fixture-dir DIR --lang en --tier free --out-dir OUT

The fixture directory holds self.wav and prospect.wav (scripts/build_replay_fixture.sh).
Output: OUT/events.jsonl, server.log, result.json and a table on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PRODUCT_PORT = 8760
DEFAULT_PORT = 8771
TIERS = ("free", "pro", "license")
CHANNELS = (
    "transcript", "pain-points", "objections", "buying-signals", "talk-time", "coaching",
    "suggestions", "summary", "phase", "script-tracking", "insights", "slide-control",
    "system", "detector-status", "config", "wizard",
)  # fmt: skip
_STRIPPED_ENV_PREFIXES = ("SALES_COPILOT_", "LLM_", "WHISPER_", "GROQ", "OPENAI", "GEMINI", "ANTHROPIC")
# Provider keys set to an explicit empty value in the child: load_dotenv(override=False) keeps a
# variable that is already set, so a repo .env cannot re-inject a key into a measurement.
_EMPTIED_PROVIDER_KEYS = (
    "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_API_KEY",
)  # fmt: skip


def validate_port(port: int) -> int:
    """Return ``port`` if it is usable for a scratch hub; refuse the product port."""
    if port == PRODUCT_PORT:
        raise ValueError(f"port {PRODUCT_PORT} is the product hub port; pick another")
    if not 1024 <= port <= 65535:
        raise ValueError(f"port {port} is outside 1024-65535")
    return port


def make_token() -> str:
    """A fresh shutdown token for one run; never reused and never the user's .env token."""
    return "measure-" + secrets.token_urlsafe(16)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fixture-dir", type=Path, required=True, help="dir with self.wav and prospect.wav")
    p.add_argument("--lang", choices=("nl", "en"), required=True)
    p.add_argument("--tier", choices=TIERS, default="free")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--wait", type=float, default=100.0, help="seconds to record after start-call")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--python", default=sys.executable, help="interpreter that runs the child")
    p.add_argument("--whisper-binary", help="whisper-cli path")
    p.add_argument("--whisper-model", help="ggml model path")
    p.add_argument("--whisper-server", help="whisper-server path")
    args = p.parse_args(argv)
    try:
        validate_port(args.port)
    except ValueError as exc:
        p.error(str(exc))
    if args.wait <= 0 or args.speed <= 0:
        p.error("--wait and --speed must be positive")
    return args


def _event_type(payload: Any) -> str:
    return str(payload.get("type", "?")) if isinstance(payload, dict) else "?"


def summarize(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per channel: count, counts by type, first and median arrival after the transcript event.

    ``events`` is a list of {"t_ms", "channel", "event"}. Delay = arrival of the event minus
    arrival of the latest final ``transcript`` event that arrived at or before it. Channels
    with no such transcript event get None. Subscribe markers, errors, ``_http`` records and
    the sticky empty message are not counted.
    """
    transcripts = sorted(
        e["t_ms"] for e in events if e["channel"] == "transcript" and _event_type(e["event"]) == "transcript"
    )
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        ch, payload = e["channel"], e["event"]
        # {} is the empty message each channel sends on subscribe
        if ch.startswith("_") or payload == {}:
            continue
        if isinstance(payload, dict) and any(k.startswith("_") for k in payload):
            continue
        row = out.setdefault(ch, {"count": 0, "types": {}, "delays_ms": []})
        row["count"] += 1
        typ = _event_type(payload)
        row["types"][typ] = row["types"].get(typ, 0) + 1
        before = [t for t in transcripts if t <= e["t_ms"]]
        if before and ch != "transcript":
            row["delays_ms"].append(e["t_ms"] - before[-1])
    for row in out.values():
        d = row.pop("delays_ms")
        row["first_delay_ms"] = round(d[0], 1) if d else None
        row["median_delay_ms"] = round(statistics.median(d), 1) if d else None
    return out


def format_table(summary: dict[str, dict[str, Any]]) -> str:
    lines = ["channel | events | types | first delay ms | median delay ms", "---|---|---|---|---"]
    for ch in CHANNELS:
        row = summary.get(ch, {"count": 0, "types": {}, "first_delay_ms": None, "median_delay_ms": None})
        types = ", ".join(f"{k} {v}" for k, v in sorted(row["types"].items())) or "-"
        first = "-" if row["first_delay_ms"] is None else row["first_delay_ms"]
        med = "-" if row["median_delay_ms"] is None else row["median_delay_ms"]
        lines.append(f"{ch} | {row['count']} | {types} | {first} | {med}")
    return "\n".join(lines)


def build_env(args: argparse.Namespace, token: str, base_env: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base_env.items() if not k.startswith(_STRIPPED_ENV_PREFIXES)}
    stripped_keys = {k for k in base_env if k.startswith(_STRIPPED_ENV_PREFIXES) and k.endswith(("_KEY", "_TOKEN"))}
    env.update(dict.fromkeys((*_EMPTIED_PROVIDER_KEYS, *sorted(stripped_keys)), ""))
    src = Path(__file__).resolve().parents[1] / "src"
    env.update(
        {
            "PYTHONPATH": str(src),
            "AUDIO_CAPTURE_METHOD": "replay",
            "REPLAY_SESSION_DIR": str(args.fixture_dir),
            "REPLAY_SPEED": str(args.speed),
            "WS_HUB_PORT": str(args.port),
            "SHUTDOWN_TOKEN": token,
            "SALES_COPILOT_NO_BROWSER": "1",
            "SALES_COPILOT_DEV_TIER": args.tier,
            "LOG_LEVEL": "DEBUG",
            "WHISPER_BACKEND": "whisper.cpp",
            "WHISPER_LANGUAGE": args.lang,
            # Pinned so the operator's .env (loaded by the child, never overriding set vars)
            # cannot change the UI or call language of the run.
            "LANGUAGE": args.lang,
            "CALL_LANGUAGE": args.lang,
            "RECORD_AUDIO": "false",
            "LLM_PROVIDER": "none",
        }
    )
    for key, val in (
        ("WHISPER_CPP_BINARY", args.whisper_binary),
        ("WHISPER_CPP_MODEL_PATH", args.whisper_model),
        ("WHISPER_CPP_SERVER_BINARY", args.whisper_server),
    ):
        if val:
            env[key] = val
    return env


def start_call_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "config": {
            "screen_mode": "single",
            "language": args.lang,
            "transcript": {"language": args.lang},
            "modules": {
                "talk_time": True,
                "transcript": True,
                "pain_points": True,
                "presentation": True,
                "post_call_report": False,
            },
        }
    }


def _post(port: int, token: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST",
        data=json.dumps(body or {}).encode(),
        headers={"X-Sales-Copilot-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


async def _subscribe(ch: str, args: argparse.Namespace, auth: dict[str, str], events: list, clock, stop):
    import websockets

    try:
        async with websockets.connect(f"ws://127.0.0.1:{args.port}/ws/{ch}", additional_headers=auth) as ws:
            events.append({"t_ms": clock(), "channel": ch, "event": {"_subscribed": True}})
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), 0.3)
                except TimeoutError:
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"_raw": str(raw)[:200]}
                events.append({"t_ms": clock(), "channel": ch, "event": payload})
    except Exception as exc:  # noqa: BLE001 - recorded as an event so the failure shows in the result
        events.append({"t_ms": clock(), "channel": ch, "event": {"_subscribe_error": repr(exc)}})


async def _run(args: argparse.Namespace, token: str, proc: subprocess.Popen, events: list) -> str | None:
    t0 = time.monotonic()

    def clock() -> float:
        return round((time.monotonic() - t0) * 1000, 1)

    for _ in range(240):
        try:
            socket.create_connection(("127.0.0.1", args.port), 0.5).close()
            break
        except OSError:
            if proc.poll() is not None:
                return f"server exited early with code {proc.returncode}"
            await asyncio.sleep(0.5)
    else:
        return "server did not open its port within 120 s"
    auth = {"Authorization": "Basic " + base64.b64encode(f"token:{token}".encode()).decode()}
    stop = asyncio.Event()
    tasks = [asyncio.create_task(_subscribe(c, args, auth, events, clock, stop)) for c in CHANNELS]
    await asyncio.sleep(1.5)
    status = _post(args.port, token, "/api/start-call", start_call_body(args))
    events.append({"t_ms": clock(), "channel": "_http", "event": {"start_call": status}})
    print("start-call", status, flush=True)
    error = None if status[0] == 200 else f"start-call returned {status[0]}: {status[1]}"
    await asyncio.sleep(args.wait if error is None else 1)
    events.append(
        {"t_ms": clock(), "channel": "_http", "event": {"end_call": _post(args.port, token, "/api/end-call")}}
    )
    await asyncio.sleep(4)
    stop.set()
    await asyncio.gather(*tasks)
    failed = failed_channels(events)
    if failed:
        sub = "subscription failed for channel(s): " + ", ".join(failed)
        error = sub if error is None else f"{error}; {sub}"
    return error


def failed_channels(events: list[dict[str, Any]]) -> list[str]:
    """Channels (in CHANNELS order) whose subscription failed or dropped during the run."""
    bad = {e["channel"] for e in events if isinstance(e["event"], dict) and "_subscribe_error" in e["event"]}
    return [c for c in CHANNELS if c in bad]


def _failure(stage: str, exc: BaseException) -> dict[str, str]:
    return {"stage": stage, "error_type": type(exc).__name__, "message": str(exc)[:500]}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for name in ("self.wav", "prospect.wav"):
        if not (args.fixture_dir / name).is_file():
            print(f"fixture missing: {args.fixture_dir / name}", file=sys.stderr)
            return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    token = make_token()
    events: list[dict[str, Any]] = []
    error: str | None = None
    failure: dict[str, str] | None = None
    with open(args.out_dir / "server.log", "wb") as logf:
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                [args.python, "-m", "sales_copilot"],
                cwd=Path(__file__).resolve().parents[1],
                env=build_env(args, token, dict(os.environ)),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001 - becomes a structured failure in result.json
            failure = _failure("start_server", exc)
        if proc is not None:
            try:
                error = asyncio.run(_run(args, token, proc, events))
            except Exception as exc:  # noqa: BLE001 - keep the collected events, report the failure
                failure = _failure("run", exc)
            finally:
                proc.terminate()  # only the child started above
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
    if failure is not None:
        error = f"{failure['stage']} failed: {failure['error_type']}: {failure['message']}"
    with open(args.out_dir / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    summary = summarize(events)
    result = {
        "lang": args.lang, "tier": args.tier, "speed": args.speed, "port": args.port,
        "wait_s": args.wait, "error": error, "failure": failure,
        "failed_channels": failed_channels(events), "channels": summary,
    }  # fmt: skip
    (args.out_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(format_table(summary))
    if error:
        print("FAILED:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
