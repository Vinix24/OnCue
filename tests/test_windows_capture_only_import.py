"""Regression coverage for the Windows capture-only install (INSTALL.md).

INSTALL.md documented a four-step Windows install because importing
``sales_copilot.__main__`` unconditionally pulled in detector/reports deps
(litellm, instructor, semantic-router, sentence-transformers, aiosqlite).
PR #149/#152 already made ``detector_main``/``reports_main`` local imports
inside ``_run_call()`` (covered by ``test_lazy_imports.py``), but two more
edges kept the same failure alive for a bare ``pip install ".[windows]"``:

- ``sales_copilot.sample_aha.player`` imported ``httpx`` at module scope.
  It sits on the unconditional import chain
  ``__main__`` -> ``modules.talk_time.__main__`` -> ``websocket.hub`` ->
  ``websocket.hub_api`` -> ``sample_aha`` -> ``sample_aha.player``, so it ran
  for every boot regardless of which modules a call enables.
- ``modules.transcriber.backends`` imported ``GroqBackend`` (which imports
  ``httpx``) at module scope, unguarded — unlike ``WhisperCppBackend``, which
  already degrades to ``None`` behind a try/except.

``httpx`` is not a base or ``windows`` dependency (only detector-adjacent
extras such as ``groq``, ``openai``, ``google-genai``, ``litellm`` pull it in
transitively), so a Windows capture-only install never has it.

These tests fake the missing dependency by setting ``sys.modules["httpx"] =
None`` (the standard trick: any subsequent ``import httpx`` raises
``ImportError`` immediately) in a **subprocess**, so the sabotage never leaks
into the rest of this test session's already-imported modules.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_BLOCK_HTTPX = "import sys; sys.modules['httpx'] = None\n"

_BLOCK_DETECTOR_CHAIN = (
    "import sys\n"
    "for _name in ('litellm', 'instructor', 'semantic_router', "
    "'sentence_transformers', 'aiosqlite', 'httpx'):\n"
    "    sys.modules[_name] = None\n"
)


def _run(snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_orchestrator_boots_without_detector_reports_or_httpx_deps() -> None:
    """``import sales_copilot.__main__`` must not require the detector/reports chain.

    This is the behavioural counterpart to ``test_lazy_imports.py``'s
    structural checks: instead of grepping source, it actually blocks the
    heavy packages and imports the real module graph, so it also catches a
    regression introduced anywhere else in the import chain (not just a
    reintroduced top-level ``detector_main``/``reports_main`` import).
    """
    result = _run(_BLOCK_DETECTOR_CHAIN + "import sales_copilot.__main__\nprint('OK')\n")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_sample_aha_player_importable_without_httpx() -> None:
    """The zero-dependency demo player must not require httpx to import."""
    result = _run(_BLOCK_HTTPX + "import sales_copilot.sample_aha.player\nprint('OK')\n")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_transcriber_backends_importable_without_httpx() -> None:
    """The backends package (needed for the default whisper.cpp backend too) must import."""
    result = _run(
        _BLOCK_HTTPX
        + "from sales_copilot.modules.transcriber.backends import create_backend\n"
        + "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_groq_transcription_backend_without_httpx_fails_loudly_not_bare_import_error() -> None:
    """Selecting WHISPER_BACKEND=groq without httpx must raise an actionable error.

    Not a bare ``ImportError`` half-way into a call: ``create_backend()`` must
    name the missing dependency and the exact ``pip install`` line that fixes
    it, the same contract ``WhisperCppBackend``'s missing-binary path already
    honors.
    """
    snippet = (
        _BLOCK_HTTPX
        + "from sales_copilot.modules.transcriber.backends import create_backend\n"
        + "try:\n"
        + "    create_backend({'backend': 'groq'})\n"
        + "except RuntimeError as exc:\n"
        + "    assert 'httpx' in str(exc)\n"
        + "    assert 'pip install' in str(exc)\n"
        + "    print('OK')\n"
        + "except ImportError as exc:\n"
        + "    print('BARE_IMPORT_ERROR:', exc)\n"
    )
    result = _run(snippet)
    assert result.returncode == 0, result.stderr
    assert "BARE_IMPORT_ERROR" not in result.stdout, result.stdout
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    "blocked_module",
    ["litellm", "instructor", "semantic_router", "sentence_transformers", "aiosqlite"],
)
def test_orchestrator_boots_without_each_detector_dep_individually(blocked_module: str) -> None:
    """Isolate which single dependency, if any, would break a capture-only boot.

    A parametrized companion to the combined-block test above: if this ever
    regresses, the failing parameter names the exact dependency that leaked
    back onto the unconditional import path instead of just "something did".
    """
    result = _run(
        f"import sys; sys.modules[{blocked_module!r}] = None\n"
        "import sales_copilot.__main__\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_default_whisper_cpp_backend_refuses_on_windows_without_httpx() -> None:
    """The fallback to mlx-whisper must refuse, not silently substitute, on Windows.

    Reproduces the field-reported defect (2026-09-06, Windows 11 + RTX 2050):
    a plain `pip install ".[windows]"` (before this fix's httpx addition) has
    no httpx, so WhisperCppBackend fails to import; the old behaviour degraded
    to MlxWhisperBackend regardless, which cannot run on Windows at all (Apple
    Silicon only) -- silently leaving no working transcription and no error.
    create_backend() must now raise a RuntimeError naming both problems.
    """
    # sys.platform is flipped only *after* create_backend (and everything it
    # imports) has finished importing: pydantic's zoneinfo/sysconfig init runs
    # at import time and reads the real platform, so faking it any earlier
    # breaks unrelated stdlib lookups (_sysconfigdata__linux_darwin) rather
    # than the transcriber-backend selection this test targets.
    snippet = (
        _BLOCK_HTTPX
        + "from sales_copilot.modules.transcriber.backends import create_backend\n"
        + "import sys; sys.platform = 'win32'\n"
        + "try:\n"
        + "    backend = create_backend()\n"
        + "    print('NO_RAISE:', type(backend).__name__)\n"
        + "except RuntimeError as exc:\n"
        + "    assert 'httpx' in str(exc)\n"
        + "    assert 'mlx-whisper' in str(exc)\n"
        + "    print('OK')\n"
    )
    result = _run(snippet)
    assert result.returncode == 0, result.stderr
    assert "NO_RAISE" not in result.stdout, result.stdout
    assert "OK" in result.stdout


def test_default_whisper_cpp_backend_refuses_on_linux_without_httpx() -> None:
    """Same refusal on Linux, where mlx-whisper is equally impossible."""
    snippet = (
        _BLOCK_HTTPX
        + "from sales_copilot.modules.transcriber.backends import create_backend\n"
        + "import sys; sys.platform = 'linux'\n"
        + "try:\n"
        + "    backend = create_backend()\n"
        + "    print('NO_RAISE:', type(backend).__name__)\n"
        + "except RuntimeError as exc:\n"
        + "    assert 'mlx-whisper' in str(exc)\n"
        + "    print('OK')\n"
    )
    result = _run(snippet)
    assert result.returncode == 0, result.stderr
    assert "NO_RAISE" not in result.stdout, result.stdout
    assert "OK" in result.stdout
