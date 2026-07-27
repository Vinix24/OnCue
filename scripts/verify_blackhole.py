"""Verify BlackHole 2ch receives system audio.

Run with audio playing (e.g. YouTube in Chrome) and Multi-Output Device
set as system output. Reports pass/fail with diagnostics.

Usage:
    python scripts/verify_blackhole.py
"""

import sys
import time

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    print("ERROR: sounddevice not installed. Run: pip install sounddevice")
    sys.exit(1)


DEVICE_NAME = "BlackHole 2ch"
TEST_DURATION = 3  # seconds
SILENCE_THRESHOLD = 0.0005


def find_blackhole() -> dict | None:
    """Find BlackHole device and return its info."""
    for i, d in enumerate(sd.query_devices()):
        if DEVICE_NAME in d["name"] and d["max_input_channels"] > 0:
            return {"index": i, **d}
    return None


def check_sample_rates() -> None:
    """Print sample rates of all relevant devices for mismatch detection."""
    print("\n--- Device Sample Rates ---")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 or d["max_output_channels"] > 0:
            direction = []
            if d["max_input_channels"] > 0:
                direction.append("IN")
            if d["max_output_channels"] > 0:
                direction.append("OUT")
            print(
                f"  [{i}] {d['name']:30s} {'/'.join(direction):6s} "
                f"sr={d['default_samplerate']:.0f} Hz"
            )

    # Check for rate mismatches in devices that might be in Multi-Output
    rates = {}
    for i, d in enumerate(sd.query_devices()):
        rates[d["name"]] = d["default_samplerate"]

    bh_rate = rates.get("BlackHole 2ch")
    mo_rate = rates.get("Multi-Output Device")
    if bh_rate and mo_rate and bh_rate != mo_rate:
        print(
            f"\n  WARNING: Sample rate mismatch! "
            f"BlackHole={bh_rate:.0f}Hz vs Multi-Output={mo_rate:.0f}Hz"
        )


def test_loopback(device_idx: int) -> bool:
    """Test BlackHole by playing a tone into its output and reading from input."""
    print("\n--- Loopback Test (self-test) ---")
    fs = 48000
    t = np.linspace(0, 0.5, int(fs * 0.5), dtype="float32")
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32")
    tone_stereo = np.column_stack([tone, tone])

    try:
        recording = sd.playrec(
            tone_stereo, samplerate=fs, channels=2, device=device_idx, dtype="float32"
        )
        sd.wait()
        rms = np.sqrt(np.mean(recording**2))
        print(f"  Loopback RMS: {rms:.6f}")
        if rms > 0.01:
            print("  PASS: BlackHole device is functional")
            return True
        print("  FAIL: BlackHole loopback returned silence")
        return False
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


def test_system_audio(device_idx: int) -> bool:
    """Record from BlackHole to check if system audio routes through."""
    print(f"\n--- System Audio Test ({TEST_DURATION}s) ---")
    print("  Make sure audio is playing (YouTube, Spotify, etc.)")
    print("  Make sure system output is set to Multi-Output Device")
    print(f"  Recording {TEST_DURATION}s from {DEVICE_NAME}...")

    fs = 48000
    recording = sd.rec(
        int(TEST_DURATION * fs),
        samplerate=fs,
        channels=2,
        device=device_idx,
        dtype="float32",
    )
    sd.wait()

    rms = np.sqrt(np.mean(recording**2))
    peak = np.max(np.abs(recording))

    print(f"  RMS:  {rms:.6f}")
    print(f"  Peak: {peak:.6f}")

    # Per-second breakdown
    for sec in range(TEST_DURATION):
        start = sec * fs
        end = (sec + 1) * fs
        sec_rms = np.sqrt(np.mean(recording[start:end] ** 2))
        print(f"    Second {sec + 1}: RMS={sec_rms:.6f}")

    if rms < SILENCE_THRESHOLD:
        print("  FAIL: No system audio detected on BlackHole")
        print()
        print("  Possible causes:")
        print("    1. System output is not set to Multi-Output Device")
        print("       -> Check System Settings > Sound > Output")
        print("    2. Bluetooth reconnection reset the output device")
        print("       -> Re-select Multi-Output Device as output")
        print("    3. Multi-Output Device does not include BlackHole 2ch")
        print("       -> Open Audio MIDI Setup and verify sub-devices")
        print("    4. No audio is playing on the system")
        print("       -> Play something in Chrome/Safari/Spotify")
        return False

    print("  PASS: System audio detected on BlackHole")

    # Also test at 16kHz mono (copilot config)
    print("\n--- 16kHz Mono Test (copilot capture rate) ---")
    recording_16k = sd.rec(
        int(2 * 16000),
        samplerate=16000,
        channels=1,
        device=device_idx,
        dtype="float32",
    )
    sd.wait()
    rms_16k = np.sqrt(np.mean(recording_16k**2))
    print(f"  16kHz mono RMS: {rms_16k:.6f}")
    if rms_16k < SILENCE_THRESHOLD:
        print("  FAIL: Audio lost during resampling to 16kHz")
        return False
    print("  PASS: 16kHz mono capture works")
    return True


def main() -> int:
    print("=== BlackHole 2ch Verification ===")
    print(f"  Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    check_sample_rates()

    device = find_blackhole()
    if device is None:
        print(f"\nERROR: {DEVICE_NAME} not found. Is it installed?")
        print("  Install: brew install blackhole-2ch")
        return 1

    print(f"\n  Found {DEVICE_NAME} at index {device['index']}")
    print(f"  Input channels: {device['max_input_channels']}")
    print(f"  Output channels: {device['max_output_channels']}")
    print(f"  Sample rate: {device['default_samplerate']:.0f} Hz")

    loopback_ok = test_loopback(device["index"])
    if not loopback_ok:
        print("\nBlackHole device itself is broken. Try reinstalling:")
        print("  brew reinstall blackhole-2ch")
        print("  Then restart your Mac.")
        return 1

    system_ok = test_system_audio(device["index"])

    print("\n--- Summary ---")
    print(f"  BlackHole device:  {'PASS' if loopback_ok else 'FAIL'}")
    print(f"  System audio:      {'PASS' if system_ok else 'FAIL'}")

    if loopback_ok and system_ok:
        print("\n  BlackHole is working. The copilot can capture system audio.")
        return 0

    if loopback_ok and not system_ok:
        print("\n  BlackHole works but receives no system audio.")
        print("  Fix: Set system output to Multi-Output Device that includes BlackHole.")
        return 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
