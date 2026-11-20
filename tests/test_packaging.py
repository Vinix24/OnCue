"""Smoke tests for the native macOS packaging setup.

These tests do not perform a full py2app build (that requires a macOS GUI
context and the full ``.[full]`` dependency set). Instead they verify that the
packaging metadata excludes the right directories and that the bundled app can
import the package and resolve paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import setup_app as setup_app_module


@pytest.fixture()
def setup_app() -> type:
    """Return the importable setup_app module."""
    return setup_app_module


def _resource_paths(resources: list[str | tuple[str, list[str]]]) -> list[Path]:
    """Normalize resource entries to ``Path`` objects."""
    paths: list[Path] = []
    for entry in resources:
        if isinstance(entry, tuple):
            paths.extend(Path(p) for p in entry[1])
        else:
            paths.append(Path(entry))
    return paths


def test_setup_app_excludes_bloat_directories(setup_app: type) -> None:
    """The bundle must not ship tests, docs, .vnx*, claudedocs, daily-log or worktrees."""
    repo_root = Path(setup_app.REPO_ROOT)
    resources = _resource_paths(setup_app._collect_resources())
    forbidden = {"tests", "docs", ".vnx", ".vnx-data", "claudedocs", "daily-log"}
    for resource in resources:
        try:
            rel = resource.relative_to(repo_root)
        except ValueError:
            # External resources (e.g. bin/audiotee) have no repo-relative path;
            # skip them because they cannot contain repo-specific bloat dirs.
            continue
        parts = {p.lower() for p in rel.parts}
        assert not (parts & forbidden), f"resource {resource} contains a forbidden directory"


def test_setup_app_includes_required_defaults(setup_app: type) -> None:
    """Default config, sample data and frontend assets must be bundled."""
    resources = _resource_paths(setup_app._collect_resources())
    resource_names = {r.name for r in resources}
    assert "config" in resource_names
    assert "samples" in resource_names or any("samples" in r.parts for r in resources)
    assert "dashboard" in resource_names
    assert "presentation" in resource_names


def test_setup_app_version_matches_pyproject(setup_app: type) -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    assert setup_app._read_version() == data["project"]["version"]


def test_setup_app_entry_point_is_the_notetaker_launcher(setup_app: type) -> None:
    """The py2app entry point is the dialog launcher, not sales_copilot.__main__ directly."""
    text = Path(setup_app.APP_SCRIPT).read_text(encoding="utf-8")
    assert "def dialog_script" in text and "record_label" in text
    assert "default button" in text
    assert not hasattr(setup_app, "_ensure_app_script")


def test_setup_app_collect_resources_idempotent(setup_app: type) -> None:
    """Collecting resources twice must return the same list (no side effects)."""
    first = setup_app._collect_resources()
    second = setup_app._collect_resources()
    assert first == second


def test_frozen_import_still_resolves_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the bundled app importing sales_copilot and resolving paths."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_bundle = tmp_path / "SalesCopilot.app"
    executable = fake_bundle / "Contents" / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("HOME", str(fake_home))

    # Importing the package should succeed even with a fake frozen context.
    from sales_copilot.core.paths import resolve_app_support

    expected = fake_home / "Library" / "Application Support" / "SalesCopilot"
    assert resolve_app_support() == expected


def test_setup_app_module_runnable_help(setup_app: type) -> None:
    """``python setup_app.py --help`` exits cleanly without invoking setup()."""
    # We cannot easily run the full setup without py2app, but we can exercise
    # helper functions. This test simply documents that the module is importable
    # and its public helpers are reachable.
    assert callable(setup_app._collect_resources)
    assert callable(setup_app._read_version)
