"""An update of an existing install keeps it Dutch; scripts/install.sh calls the real migration script."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATE = REPO_ROOT / "scripts" / "migrate_env_language.sh"
INSTALL = REPO_ROOT / "scripts" / "install.sh"


def _migrate(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(MIGRATE), str(target)], capture_output=True, text=True, timeout=30)


def test_settings_without_the_keys_get_the_dutch_lines_once(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.write_text("PORT=8760", encoding="utf-8")  # no trailing newline

    first = _migrate(settings)

    assert first.returncode == 0
    assert settings.read_text(encoding="utf-8").splitlines() == ["PORT=8760", "LANGUAGE=nl", "CALL_LANGUAGE=nl"]
    assert len(first.stdout.strip().splitlines()) == 1 and "en" in first.stdout
    after_first = settings.read_text(encoding="utf-8")

    second = _migrate(settings)

    assert second.returncode == 0 and second.stdout == ""
    assert settings.read_text(encoding="utf-8") == after_first


@pytest.mark.parametrize(
    "content", ["LANGUAGE=en\n", "CALL_LANGUAGE=de\n", "export LANGUAGE=nl\n", "  CALL_LANGUAGE=en\n"]
)
def test_settings_with_either_key_are_left_untouched(tmp_path: Path, content: str) -> None:
    settings = tmp_path / "settings"
    settings.write_text(content, encoding="utf-8")

    result = _migrate(settings)

    assert result.returncode == 0 and result.stdout == ""
    assert settings.read_text(encoding="utf-8") == content


def test_keys_written_with_spaces_around_equals_count_as_set(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    content = "LANGUAGE = en\nexport CALL_LANGUAGE   =   en\n"
    settings.write_text(content, encoding="utf-8")

    result = _migrate(settings)

    assert result.returncode == 0 and result.stdout == ""
    assert settings.read_text(encoding="utf-8") == content


def test_commented_out_keys_do_not_count_as_set(tmp_path: Path) -> None:
    settings = tmp_path / "settings"
    settings.write_text("# LANGUAGE=en\n# CALL_LANGUAGE=en\n", encoding="utf-8")

    _migrate(settings)

    assert settings.read_text(encoding="utf-8").splitlines()[-2:] == ["LANGUAGE=nl", "CALL_LANGUAGE=nl"]


def test_missing_settings_file_is_a_failure(tmp_path: Path) -> None:
    result = _migrate(tmp_path / "does-not-exist")

    assert result.returncode == 1 and result.stdout == ""
    assert "no settings file" in result.stderr


def test_install_migrates_only_an_existing_settings_file() -> None:
    fresh, existing = (
        INSTALL.read_text(encoding="utf-8").split('if [[ ! -f "$ENV_FILE" ]]; then', 1)[1].split("else", 1)
    )

    assert 'cp "$ROOT_DIR/.env.example" "$ENV_FILE"' in fresh
    assert "migrate_env_language.sh" not in fresh
    assert "migrate_env_language.sh" in existing
