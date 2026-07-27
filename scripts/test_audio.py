"""Test audio capture setup — verifies mic and system audio streams work."""

import queue
import subprocess
import sys
import time

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    print("ERROR: sounddevice not installed. Run: pip install sounddevice")
    sys.exit(1)


def test_microphone(duration: int = 3) -> bool:
    """Test microphone capture for N seconds."""
    print(f"\n--- Testing microphone ({duration}s) ---")
    print("Speak into your microphone...")

    q: queue.Queue[np.ndarray] = queue.Queue()
    frames_received = 0

    def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        nonlocal frames_received
        frames_received += 1
        q.put(indata.copy())

    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=callback, blocksize=1024):
            time.sleep(duration)
    except Exception as e:
        print(f"ERROR: {e}")
        return False

    # Check if we got audio with actual signal
    all_audio = []
    while not q.empty():
        all_audio.append(q.get())

    if not all_audio:
        print("ERROR: No audio frames received.")
        return False

    audio = np.concatenate(all_audio)
    rms = np.sqrt(np.mean(audio**2))
    peak = np.max(np.abs(audio))

    print(f"Frames received: {frames_received}")
    print(f"RMS level: {rms:.6f}")
    print(f"Peak level: {peak:.6f}")

    if rms < 0.0001:
        print("WARNING: Audio level very low — mic may be muted or not connected.")
        return False

    print("OK: Microphone capture working.")
    return True


def test_audiotee() -> bool:
    """Test AudioTee binary exists and can list processes."""
    print("\n--- Testing AudioTee ---")

    # Check if binary exists
    import os
    audiotee_path = os.environ.get("AUDIOTEE_BINARY_PATH", "./bin/audiotee")

    if not os.path.exists(audiotee_path):
        print(f"WARNING: AudioTee binary not found at {audiotee_path}")
        print("Run ./scripts/setup.sh to build it, or set AUDIOTEE_BINARY_PATH in .env")
        return False

    print(f"AudioTee binary found: {audiotee_path}")

    # Check if Teams/Zoom is running
    for process in ["Microsoft Teams", "zoom.us", "Google Chrome"]:
        try:
            pid = subprocess.check_output(["pgrep", "-x", process], stderr=subprocess.DEVNULL).decode().strip()
            if pid:
                print(f"Found {process} (PID: {pid})")
        except subprocess.CalledProcessError:
            pass

    print("OK: AudioTee binary exists. Start a video call to test system audio capture.")
    return True


def list_audio_devices() -> None:
    """List available audio devices."""
    print("\n--- Available Audio Devices ---")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        direction = ""
        if d["max_input_channels"] > 0:
            direction += "IN "
        if d["max_output_channels"] > 0:
            direction += "OUT"
        print(f"  [{i}] {d['name']} ({direction.strip()})")


def main() -> None:
    print("=== Audio Capture Test ===")

    list_audio_devices()
    mic_ok = test_microphone()
    audiotee_ok = test_audiotee()

    print("\n--- Summary ---")
    print(f"Microphone: {'OK' if mic_ok else 'FAILED'}")
    print(f"AudioTee:   {'OK' if audiotee_ok else 'NOT READY'}")

    if mic_ok and audiotee_ok:
        print("\nAll good. Ready to capture audio.")
    else:
        print("\nFix the issues above before running the copilot.")


if __name__ == "__main__":
    main()
