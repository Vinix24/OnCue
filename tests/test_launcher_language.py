"""The OnCue.app dialog follows the install's LANGUAGE (English unless it says nl).

Runs the real scripts/launcher/oncue-language.sh against a temporary install and
checks that scripts/launcher/build_app.sh carries both label sets.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "launcher"
LANGUAGE_SCRIPT = LAUNCHER / "oncue-language.sh"
BUILD_APP = LAUNCHER / "build_app.sh"


def _language(repo: Path) -> str:
    result = subprocess.run(["bash", str(LANGUAGE_SCRIPT), str(repo)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("LANGUAGE=nl\n", "nl"),
        ("LANGUAGE=NL\n", "nl"),
        ('LANGUAGE="nl"\n', "nl"),
        ("LANGUAGE=nl # Dutch\n", "nl"),
        ("LANGUAGE=nl\r\n", "nl"),
        ("export LANGUAGE=nl\n", "nl"),
        ("LANGUAGE = nl\n", "nl"),  # the dotenv loader accepts this
        ("LANGUAGE =nl\n", "nl"),
        ("LANGUAGE= NL\n", "nl"),
        ("export LANGUAGE\t=\tnl\n", "nl"),
        ("LANGUAGE=en\n", "en"),
        ("LANGUAGE=de\n", "de"),
        ("LANGUAGE=nl\nLANGUAGE=en\n", "en"),  # last one wins
        ("CALL_LANGUAGE=nl\n", "en"),  # a different key does not count
        ("# LANGUAGE=nl\n", "en"),
        ("PORT=8760\n", "en"),
        ("LANGUAGE=\n", "en"),
        ("", "en"),
    ],
)
def test_language_is_read_from_the_install_settings(tmp_path: Path, content: str, expected: str) -> None:
    (tmp_path / ".env").write_bytes(content.encode("utf-8"))

    assert _language(tmp_path) == expected


def test_install_without_settings_is_english(tmp_path: Path) -> None:
    assert _language(tmp_path) == "en"


def test_default_repo_is_the_one_the_script_lives_in() -> None:
    result = subprocess.run(["bash", str(LANGUAGE_SCRIPT)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert result.stdout.strip() != ""


def test_dialog_has_english_and_dutch_labels_and_no_hardcoded_path() -> None:
    source = BUILD_APP.read_text(encoding="utf-8")

    for label in ("Cancel", "Live coach", "Record and transcribe", "Annuleer", "Opnemen en uitschrijven"):
        assert f'"{label}"' in source
    assert 'if uiLanguage is "nl" then' in source
    assert "oncue-language.sh" in source
    assert "/Users/" not in source
