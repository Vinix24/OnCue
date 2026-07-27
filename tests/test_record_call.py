"""Tests for the standalone record_call CLI."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "record_call.py"


def _run_help() -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_help_text_is_neutral() -> None:
    help_text = _run_help()
    lowered = help_text.lower()

    assert "avconferenced" not in lowered
    assert "phone call" not in lowered
    assert "phone-call" not in lowered
    assert "sales pro" not in lowered
    assert "pro license" not in lowered
    assert re.search(r"\bpro\b", lowered) is None
    assert "far-side" in lowered or "target process" in lowered
