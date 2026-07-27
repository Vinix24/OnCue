#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys

DEVICE_NAME_DEFAULT = "OnCue Output"
BLACKHOLE_NAME_DEFAULT = "BlackHole 2ch"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/verify a Multi-Output Device for OnCue.",
    )
    parser.add_argument(
        "--device-name",
        default=DEVICE_NAME_DEFAULT,
        help="Expected Multi-Output device name.",
    )
    parser.add_argument(
        "--blackhole-name",
        default=BLACKHOLE_NAME_DEFAULT,
        help="Expected BlackHole device name.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without making changes.",
    )
    return parser.parse_args()


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _switchaudio_devices() -> list[str]:
    try:
        result = _run(["SwitchAudioSource", "-a"], check=True)
    except Exception:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _has_device(name: str) -> bool:
    return any(name.lower() == line.lower() for line in _switchaudio_devices())


def _try_coreaudio_path(device_name: str, blackhole_name: str, dry_run: bool) -> tuple[bool, str]:
    # Path A placeholder: pyobjc + CoreAudio aggregate device creation.
    if dry_run:
        return True, "[dry-run] Path A would attempt CoreAudio aggregate-device creation via pyobjc."

    try:
        __import__("CoreAudio")
    except Exception as exc:
        return False, f"Path A unavailable (CoreAudio/pyobjc import failed): {exc}"

    if _has_device(device_name):
        return True, f"Device already exists: {device_name}"

    return False, (
        "Path A not completed: CoreAudio aggregate device creation API is unavailable in this environment. "
        f"Expected target='{device_name}', input='{blackhole_name}'."
    )


def _try_applescript_path(device_name: str, dry_run: bool) -> tuple[bool, str]:
    # Path B: UI scripting fallback; locale/UI-state sensitive.
    script = '''
    tell application "Audio MIDI Setup" to activate
    delay 1
    tell application "System Events"
        tell process "Audio MIDI Setup"
            click menu item "Create Multi-Output Device" of menu "Audio Devices" of menu bar item "+" of menu bar 1
        end tell
    end tell
    '''

    if dry_run:
        return True, "[dry-run] Path B would attempt AppleScript UI automation in Audio MIDI Setup."

    try:
        _run(["osascript", "-e", script], check=True)
    except Exception as exc:
        return False, f"Path B failed (AppleScript/UI automation): {exc}"

    if _has_device(device_name):
        return True, f"Device created via Path B: {device_name}"
    return False, "Path B executed but target device was not detected afterwards."


def _manual_fallback(device_name: str, blackhole_name: str, dry_run: bool) -> None:
    print("\nManual fallback required.")
    print("1) Open Audio MIDI Setup (Audio-MIDI-configuratie).")
    print("2) Click '+' and choose 'Create Multi-Output Device'.")
    print(f"3) Rename it to: {device_name}")
    print(f"4) Enable at least your speakers + {blackhole_name} in that Multi-Output device.")
    print("5) In macOS Sound settings, set output to that Multi-Output device.")

    if not dry_run:
        try:
            _run(["open", "-a", "Audio MIDI Setup"], check=False)
        except Exception:
            pass


def main() -> int:
    args = parse_args()

    print("OnCue audio setup helper")
    print(f"Target Multi-Output name: {args.device_name}")

    if _has_device(args.device_name):
        print(f"OK: device already exists: {args.device_name}")
        return 0

    ok_a, msg_a = _try_coreaudio_path(args.device_name, args.blackhole_name, args.dry_run)
    print(msg_a)
    if ok_a and (args.dry_run or _has_device(args.device_name)):
        return 0

    ok_b, msg_b = _try_applescript_path(args.device_name, args.dry_run)
    print(msg_b)
    if ok_b and (args.dry_run or _has_device(args.device_name)):
        return 0

    _manual_fallback(args.device_name, args.blackhole_name, args.dry_run)
    if args.dry_run:
        return 0

    if _has_device(args.device_name):
        print(f"OK: detected {args.device_name} after fallback.")
        return 0

    print("ERROR: Multi-Output device not found yet.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
