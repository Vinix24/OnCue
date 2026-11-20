"""oncue-launch.sh messages come from the catalog in the language of the install (English unless nl)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCH = REPO_ROOT / "scripts" / "launcher" / "oncue-launch.sh"


def _launch(install: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(LAUNCH), *args], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": str(install), "ONCUE_REPO": str(install)},
    )


def _install(tmp_path: Path, env_text: str | None) -> Path:
    catalogs = tmp_path / "config" / "i18n"
    catalogs.mkdir(parents=True)
    for lang in ("en", "nl"):
        (catalogs / f"{lang}.yaml").write_bytes((REPO_ROOT / "config" / "i18n" / f"{lang}.yaml").read_bytes())
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    return tmp_path


DUTCH = "Onbekende modus: 'bogus'. Gebruik 'dashboard' of 'record'."
ENGLISH = "Unknown mode: 'bogus'. Use 'dashboard' or 'record'."


@pytest.mark.parametrize(
    ("env_text", "expected"),
    [(None, ENGLISH), ("LANGUAGE=nl\n", DUTCH), ("LANGUAGE = nl\n", DUTCH), ("LANGUAGE=en\n", ENGLISH)],
)
def test_launcher_error_follows_the_language_of_the_install(tmp_path: Path, env_text, expected) -> None:
    result = _launch(_install(tmp_path, env_text), ["bogus"])
    assert result.returncode == 1
    assert result.stderr.strip() == expected


def test_launcher_error_names_the_key_when_the_message_is_missing(tmp_path: Path) -> None:
    install = _install(tmp_path, None)
    for lang in ("en", "nl"):
        (install / "config" / "i18n" / f"{lang}.yaml").write_text('launcher:\n  other: "x"\n', encoding="utf-8")
    result = _launch(install, ["bogus"])
    assert result.returncode == 1
    assert result.stderr.strip() != ""
