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


def _load_record_call():
    import importlib.util

    spec = importlib.util.spec_from_file_location("record_call_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_autostop_settings_come_from_dotenv(tmp_path, monkeypatch) -> None:
    for name in (
        "RECORDER_AUTOSTOP_SILENCE_SECONDS",
        "RECORDER_AUTOSTOP_PROSPECT_DB",
        "RECORDER_AUTOSTOP_SELF_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "RECORDER_AUTOSTOP_SILENCE_SECONDS=137\nRECORDER_AUTOSTOP_PROSPECT_DB=-61\n"
    )
    monkeypatch.chdir(tmp_path)
    module = _load_record_call()

    config = module._auto_stop_config()

    assert config.silence_seconds == 137
    assert config.prospect_db == -61


def test_autostop_stays_off_without_dotenv(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RECORDER_AUTOSTOP_SILENCE_SECONDS", raising=False)
    monkeypatch.chdir(tmp_path)
    module = _load_record_call()

    assert module._auto_stop_config().silence_seconds == 0
