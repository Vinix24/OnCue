"""Print which processes use the microphone, once per interval.

Run from the repo root::

    PYTHONPATH=src .venv/bin/python scripts/probe_mic_users.py [--interval 1.0] [--seconds N]
"""

from __future__ import annotations

import argparse
import subprocess
import time

from sales_copilot.audio.mic_usage import (
    MicUsageOk,
    MicUsageReadError,
    MicUsageUnavailable,
    mic_usage_available,
    read_mic_users,
)


def _executable(pid: int) -> str:
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    return out.stdout.strip().rsplit("/", 1)[-1] or "?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--seconds", type=float, default=None)
    args = parser.parse_args()

    print(f"mic-usage reader beschikbaar: {'ja' if mic_usage_available() else 'nee'}")
    deadline = None if args.seconds is None else time.monotonic() + args.seconds
    try:
        while deadline is None or time.monotonic() < deadline:
            stamp = time.strftime("%H:%M:%S")
            result = read_mic_users()
            if isinstance(result, MicUsageUnavailable):
                print(f"{stamp} niet beschikbaar: {result.reason}")
            elif isinstance(result, MicUsageReadError):
                print(f"{stamp} leesfout: {result.reason}")
            elif isinstance(result, MicUsageOk) and not result.users:
                print(f"{stamp} geen")
            elif isinstance(result, MicUsageOk):
                for user in result.users:
                    print(f"{stamp} pid={user.pid} bundle={user.bundle_id or '-'} exe={_executable(user.pid)}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
