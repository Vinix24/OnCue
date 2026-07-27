#!/usr/bin/env python3
"""Replay a recorded session through the live pipeline into the dashboard.

One-command runner for watching an already-recorded call (real transcription +
detection) live in the dashboard, e.g. to compare Free vs Pro on the same
conversation by toggling SALES_COPILOT_DEV_TIER in the environment:

    SALES_COPILOT_DEV_TIER=free .venv/bin/python scripts/replay_session.py data/sessions/<id>
    SALES_COPILOT_DEV_TIER=pro  .venv/bin/python scripts/replay_session.py data/sessions/<id> --speed 2

Then open http://localhost:8760/dashboard and start the call as usual.

The script sets AUDIO_CAPTURE_METHOD=replay + REPLAY_SESSION_DIR and execs the
normal server (python -m sales_copilot) — the tier is NOT set here; it is
inherited from the environment (SALES_COPILOT_DEV_TIER / license key) exactly
like a normal run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DASHBOARD_URL = "http://localhost:8760/dashboard"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a recorded session (self.wav + prospect.wav) through the live pipeline.",
    )
    parser.add_argument(
        "session_dir",
        type=Path,
        help="Path to a recorded session directory (e.g. data/sessions/<id>/) "
        "containing self.wav and prospect.wav (16 kHz mono).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        metavar="N",
        help="Playback speed multiplier (1.0 = real-time, 2.0 = double speed). Default: 1.0.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    session_dir = args.session_dir.expanduser().resolve()
    self_wav = session_dir / "self.wav"
    prospect_wav = session_dir / "prospect.wav"
    missing = [str(p) for p in (self_wav, prospect_wav) if not p.is_file()]
    if not session_dir.is_dir() or missing:
        print(
            f"error: replay session not found or incomplete: {session_dir}\n"
            "Expected a recorded session directory containing self.wav and prospect.wav "
            f"(16 kHz mono). Missing: {', '.join(missing) if missing else session_dir}.\n"
            "Record a call first (RECORD_AUDIO=true) or point at an existing "
            "data/sessions/<id>/ directory.",
            file=sys.stderr,
        )
        return 2
    if args.speed <= 0:
        print(f"error: --speed must be positive, got {args.speed}", file=sys.stderr)
        return 2

    os.environ["AUDIO_CAPTURE_METHOD"] = "replay"
    os.environ["REPLAY_SESSION_DIR"] = str(session_dir)
    os.environ["REPLAY_SPEED"] = str(args.speed)

    tier = os.environ.get("SALES_COPILOT_DEV_TIER", "").strip() or "(license/default)"
    print(f"Replaying {session_dir} at {args.speed}x speed — tier: {tier}")
    print(f"Open the dashboard at {DASHBOARD_URL} and start the call.")

    # Boot the normal server in-process (replaces this process).
    os.execvp(sys.executable, [sys.executable, "-m", "sales_copilot"])
    return 0  # unreachable: execvp replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
