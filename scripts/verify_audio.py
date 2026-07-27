#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

if __package__ in {None, ""}:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    if src_path.exists() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))

from sales_copilot.audio.capture import (
    check_device_health,
    find_input_device,
    get_default_input_device,
    get_default_output_device,
    list_audio_devices,
)

DEFAULT_OUTPUT_NAME = "OnCue Output"
DEFAULT_BLACKHOLE_NAME = "BlackHole 2ch"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    level: float
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify mic + BlackHole + output routing for OnCue.")
    parser.add_argument(
        "--output-device",
        default=DEFAULT_OUTPUT_NAME,
        help="Expected output device name used for test-tone playback.",
    )
    parser.add_argument(
        "--blackhole-device",
        default=DEFAULT_BLACKHOLE_NAME,
        help="BlackHole input device name to validate capture.",
    )
    parser.add_argument(
        "--tone-frequency",
        type=float,
        default=440.0,
        help="Frequency for output tone test in Hz.",
    )
    parser.add_argument(
        "--tone-seconds",
        type=float,
        default=1.0,
        help="Duration of output tone playback in seconds.",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="Sample rate for tone and loopback test.",
    )
    parser.add_argument(
        "--skip-tone",
        action="store_true",
        help="Skip tone/loopback check and only run passive mic + BlackHole health probes.",
    )
    return parser.parse_args()


def _status(ok: bool, warning: bool = False) -> str:
    if ok:
        return "OK"
    if warning:
        return "WARNING"
    return "FAIL"


def _print_device_line(index: int, name: str, status: str, detail: str) -> None:
    print(f"  [{index}] {name:<36} {status} ({detail})")


def _find_output_device(name: str) -> dict[str, object] | None:
    target = name.strip().lower()
    if not target:
        return None
    for device in list_audio_devices():
        if int(device.get("max_output_channels", 0) or 0) <= 0:
            continue
        dev_name = str(device.get("name", "")).lower()
        if target == dev_name or target in dev_name:
            return device
    return None


def _tone_loopback_check(
    *,
    output_device_index: int,
    blackhole_input_index: int,
    sample_rate: int,
    tone_frequency: float,
    tone_seconds: float,
) -> HealthResult:
    import sounddevice as sd

    frames = max(1, int(sample_rate * tone_seconds))
    time_axis = np.arange(frames, dtype=np.float32) / float(sample_rate)
    tone = (0.2 * np.sin(2.0 * math.pi * tone_frequency * time_axis)).astype(np.float32)
    playback = tone.reshape(-1, 1)

    try:
        recording = sd.playrec(
            playback,
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            device=(blackhole_input_index, output_device_index),
        )
        sd.wait()
    except Exception as exc:
        return HealthResult(ok=False, level=0.0, detail=f"playrec failed: {exc}")

    level = float(np.mean(np.abs(recording)))
    return HealthResult(ok=level > 0.001, level=level, detail="tone loopback")


def main() -> int:
    args = parse_args()

    print("Audio Device Check")
    print("==================")

    output_device = get_default_output_device()
    output_name = str(output_device["name"]) if output_device else "Unknown"
    expected_output = _find_output_device(args.output_device)
    output_ok = expected_output is not None and output_name.lower() == str(expected_output["name"]).lower()
    output_status = "OK" if output_ok else f"WARNING: Expected output '{args.output_device}'"
    print(f"Output device: {output_name:<27} {output_status}")

    devices = list_audio_devices()
    output_devices = [device for device in devices if int(device.get("max_output_channels", 0) or 0) > 0]
    input_devices = [device for device in devices if int(device.get("max_input_channels", 0) or 0) > 0]
    default_mic = get_default_input_device()
    blackhole = find_input_device(args.blackhole_device)

    print("Output devices:")
    if not output_devices:
        print("  [-] No output devices found")
    for device in output_devices:
        name = str(device["name"])
        index = int(device["index"])
        status = "DEFAULT" if output_device is not None and index == int(output_device["index"]) else "AVAILABLE"
        detail = f"channels: {int(device.get('max_output_channels', 0) or 0)}"
        _print_device_line(index, name, status, detail)

    print("Input devices:")
    mic_ok = False
    mic_level = 0.0
    blackhole_ok = False
    blackhole_level = 0.0
    if default_mic is not None:
        mic_ok, mic_level = check_device_health("default")
    if blackhole is not None:
        blackhole_ok, blackhole_level = check_device_health(args.blackhole_device)

    if not input_devices:
        print("  [-] No input devices found")
    for device in input_devices:
        name = str(device["name"])
        index = int(device["index"])
        if default_mic is not None and index == int(default_mic["index"]):
            _print_device_line(index, name, _status(mic_ok), f"level: {mic_level:.3f}")
            continue
        if blackhole is not None and index == int(blackhole["index"]):
            _print_device_line(index, name, _status(blackhole_ok), f"level: {blackhole_level:.3f}")
            continue
        _print_device_line(index, name, "AVAILABLE", "not checked")

    tone_result = HealthResult(ok=False, level=0.0, detail="skipped")
    if args.skip_tone:
        print("\nTone loopback: SKIPPED (--skip-tone)")
    elif expected_output is None or blackhole is None:
        print("\nTone loopback: FAIL (required devices not found)")
    else:
        tone_result = _tone_loopback_check(
            output_device_index=int(expected_output["index"]),
            blackhole_input_index=int(blackhole["index"]),
            sample_rate=args.sample_rate,
            tone_frequency=args.tone_frequency,
            tone_seconds=args.tone_seconds,
        )
        print(
            f"\nTone loopback: {_status(tone_result.ok)} "
            f"(level: {tone_result.level:.3f}, detail: {tone_result.detail})"
        )

    print("")
    print(f"BlackHole passive test: {'RECEIVING AUDIO' if blackhole_ok else 'SILENT'}")
    print(f"Mic test: {'RECEIVING AUDIO' if mic_ok else 'SILENT'}")

    all_ok = mic_ok and blackhole_ok and output_ok and (args.skip_tone or tone_result.ok)
    if all_ok:
        print("\nPASS: Audio pipeline looks ready.")
        return 0

    print("\nFAIL: Audio pipeline not fully ready.")
    if not output_ok:
        print(f"- Select output device '{args.output_device}' in macOS Sound settings.")
    if not blackhole_ok:
        print(f"- Verify '{args.blackhole_device}' receives system audio.")
    if not mic_ok:
        print("- Verify microphone permission and selected input device.")
    if not args.skip_tone and not tone_result.ok:
        print("- Tone loopback failed. Recheck Multi-Output routing and BlackHole channel selection.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
