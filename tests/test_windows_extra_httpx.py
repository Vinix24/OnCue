"""Prove the `windows` extra actually closes the httpx hole (2026-09-06 field report).

Not a manifest assertion: this builds a real, isolated venv with *only* the
`windows` extra installed (no `dev`, which already carries httpx and would
mask a regression here) and imports the whisper.cpp backend module in it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def windows_only_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv with `pip install ".[windows]"` and nothing else project-related."""
    venv_dir = tmp_path_factory.mktemp("windows-only-venv") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, timeout=120)
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():  # pragma: no cover - Windows layout
        venv_python = venv_dir / "Scripts" / "python.exe"

    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-q", f"{REPO_ROOT}[windows]"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert install.returncode == 0, f"pip install failed:\nstdout={install.stdout}\nstderr={install.stderr}"
    return venv_python


def test_windows_extra_alone_provides_httpx(windows_only_venv: Path) -> None:
    result = subprocess.run(
        [str(windows_only_venv), "-c", "import httpx; print('OK')"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"httpx not importable with only the windows extra:\n{result.stderr}"
    assert "OK" in result.stdout


def test_windows_extra_alone_lets_whisper_cpp_backend_import(windows_only_venv: Path) -> None:
    """The actual regression check: the backend module itself must import.

    Before this fix, `pip install ".[windows]"` alone left httpx missing, so
    this import raised ImportError and the backend factory silently degraded
    to the Apple-only mlx-whisper backend with no working transcription.
    """
    snippet = (
        "from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [str(windows_only_venv), "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"whisper_cpp_backend failed to import with only [windows]:\n{result.stderr}"
    assert "OK" in result.stdout
