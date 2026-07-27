#!/usr/bin/env python3
"""Standalone call recorder with a live level meter — no hub, dashboard or LLM.

Records two clean tracks of a call and shows, in the terminal, that the
recording is actually running: a dB meter per stream plus a REC indicator
with elapsed time. A flat meter means that side is not being captured, so you
see a dead mic or missing audio permission immediately instead of finding out
afterwards.

  • prospect = all system audio via AudioTee (the far side)
  • self     = your microphone (pick it with --mic)

Both are written as 16 kHz mono WAV to data/sessions/<timestamp>/ (prospect.wav,
self.wav). Ctrl-C stops and finalizes the files. For a browser UI with a REC
button, use scripts/record_server.py instead — both share the same engine.

Usage:
    .venv/bin/python scripts/record_call.py --list-devices
    .venv/bin/python scripts/record_call.py --mic "QuadCast"
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sales_copilot.audio.capture import (  # noqa: E402
    find_input_device,
    get_default_input_device,
    list_audio_devices,
)
from sales_copilot.audio.levels import render_meter_bar  # noqa: E402
from sales_copilot.audio.recorder_engine import EngineSnapshot, RecorderEngine  # noqa: E402

AUDIOTEE_PATH = str(REPO_ROOT / "bin" / "audiotee")
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"


def _print_devices() -> None:
    print("Input-devices (mic):")
    for dev in list_audio_devices():
        if int(dev.get("max_input_channels", 0) or 0) <= 0:
            continue
        print(f"  [{dev['index']:>2}] {dev.get('name', '?')}  ({dev.get('max_input_channels')} in)")


def _resolve_mic(spec: str | None) -> tuple[int | str | None, str]:
    if spec is None:
        dev = get_default_input_device()
        return None, (dev.get("name", "default") if dev else "default")
    if spec.strip().isdigit():
        index = int(spec.strip())
        for dev in list_audio_devices():
            if dev["index"] == index:
                return index, str(dev.get("name", index))
        return index, str(index)
    found = find_input_device(spec)
    if found is None:
        raise SystemExit(
            f"Geen input-device gevonden dat matcht op '{spec}'. Draai --list-devices voor de namen."
        )
    return int(found["index"]), str(found.get("name", spec))


def _color(text: str, code: str, *, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mic", help="Mic device name (substring) or index. Default: system default input.")
    parser.add_argument(
        "--prospect-process",
        help="Tap only this process for the far-side audio. "
        "Default: capture all system audio.",
    )
    parser.add_argument("--out-dir", default="data/sessions")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-size", type=int, default=1024)
    parser.add_argument("--silence-db", type=float, default=-50.0)
    parser.add_argument("--silence-seconds", type=float, default=4.0)
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        _print_devices()
        return 0

    color = sys.stdout.isatty()
    mic_device, mic_label = _resolve_mic(args.mic)
    engine = RecorderEngine(
        out_dir=args.out_dir,
        mic_device=mic_device,
        prospect_process=args.prospect_process,
        sample_rate=args.sample_rate,
        chunk_size=args.chunk_size,
        silence_db=args.silence_db,
        silence_seconds=args.silence_seconds,
        audiotee_path=AUDIOTEE_PATH,
    )
    directory = engine.start()

    src = {
        "prospect": "alle system-audio" if not args.prospect_process else args.prospect_process,
        "self": mic_label,
    }
    print(f"Opname: {directory}")
    snap = engine.snapshot()
    if not any(s.started for s in snap.streams):
        print(_color("[X] Geen enkele audio-stream gestart. Niets om op te nemen.", RED, enabled=color))
        engine.stop()
        return 1
    for s in snap.streams:
        flag = "" if s.started else _color(" [niet gestart]", YELLOW, enabled=color)
        print(f"  {s.label}: {src.get(s.label, '?')}{flag}")
    print(_color("Ctrl-C om te stoppen en de WAV-bestanden af te ronden.", DIM, enabled=color))
    print()

    stop = {"flag": False}

    def _on_sigint(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_sigint)
    warned: set[str] = set()
    try:
        while not stop["flag"]:
            snap = engine.snapshot()
            _draw(snap, color)
            _maybe_warn_silence(snap, warned, color)
            time.sleep(0.1)
    finally:
        sys.stdout.write("\n")
        path = engine.stop()
        _print_summary(path, engine.snapshot(), color)
    return 0


def _draw(snap: EngineSnapshot, color: bool) -> None:
    blink = int(snap.elapsed_s * 1.5) % 2 == 0
    dot = _color("●", RED, enabled=color) if blink else " "
    cells = [f"{dot} {_color('REC', RED, enabled=color)} {_fmt_elapsed(snap.elapsed_s)}"]
    for s in snap.streams:
        bar = render_meter_bar(s.display_db)
        bar_c = _color(bar, YELLOW if s.silent else GREEN, enabled=color)
        db_txt = "--" if s.display_db <= -120.0 else f"{s.display_db:5.0f}dB"
        warn = _color(" ⚠ stil", YELLOW, enabled=color) if s.silent else ""
        cells.append(f"{s.label.upper():<8} ▕{bar_c}▏ {db_txt}{warn}")
    sys.stdout.write("\r\033[K" + "   ".join(cells))
    sys.stdout.flush()


def _maybe_warn_silence(snap: EngineSnapshot, warned: set[str], color: bool) -> None:
    if snap.elapsed_s < 5.0:
        return
    for s in snap.streams:
        if s.label in warned or not s.silent:
            continue
        warned.add(s.label)
        if s.label == "prospect":
            msg = ("\n[!] Geen system-audio. Geef de terminal audio-opname-permissie "
                   "(Systeeminstellingen → Privacy & beveiliging) en zorg dat het gesprek geluid speelt.\n")
        else:
            msg = ("\n[!] Geen mic-audio. Check of de juiste mic gekozen is (--mic / --list-devices) "
                   "en niet via Bluetooth-HFP loopt.\n")
        sys.stdout.write(_color(msg, YELLOW, enabled=color))
        sys.stdout.flush()


def _print_summary(path: Path | None, snap: EngineSnapshot, color: bool) -> None:
    if path is None:
        print(_color("Geen opname om af te ronden.", YELLOW, enabled=color))
        return
    print(_color(f"Opname afgerond: {path}", GREEN, enabled=color))
    for s in snap.streams:
        wav = path / f"{s.label}.wav"
        size = wav.stat().st_size if wav.exists() else 0
        peak = "--" if s.peak_db <= -120.0 else f"{s.peak_db:.0f} dB"
        flag = "" if s.peak_db > -50.0 else _color("  ⚠ bleef stil — controleer dit spoor", YELLOW, enabled=color)
        print(f"  {s.label:<9} {wav.name:<14} {size / 1024:8.0f} KB   piek {peak}{flag}")


def _fmt_elapsed(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


if __name__ == "__main__":
    raise SystemExit(main())
