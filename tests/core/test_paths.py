"""Tests for application path resolution in dev and frozen .app mode."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sales_copilot.core import paths


def _repo_root() -> Path:
    """Canonical repo root for assertions.

    Mirrors ``sales_copilot.core.paths._repo_root()`` so tests work from any
    checkout depth (regular clone, nested worktree, etc.).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


def test_is_frozen_app_defaults_to_false_in_dev() -> None:
    assert paths.is_frozen_app() is False


def test_resolve_app_support_points_to_repo_root_in_dev() -> None:
    base = paths.resolve_app_support()
    assert base == _repo_root()
    assert (base / "pyproject.toml").exists()


def test_resolve_app_path_preserves_relative_layout() -> None:
    assert paths.resolve_app_path("config/pain_points.yaml") == _repo_root() / "config" / "pain_points.yaml"
    assert paths.resolve_app_path("data/sessions") == _repo_root() / "data" / "sessions"
    assert paths.resolve_app_path(".vnx-data/sessions.db") == _repo_root() / ".vnx-data" / "sessions.db"


def test_resolve_app_path_rejects_absolute() -> None:
    with pytest.raises(ValueError, match="relative path required"):
        paths.resolve_app_path("/tmp/absolute")


def test_resolve_app_resource_rejects_absolute() -> None:
    with pytest.raises(ValueError, match="relative path required"):
        paths.resolve_app_resource("/tmp/absolute")


def test_resolve_app_path_is_stable_across_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing cwd must not change resolved paths (the whole point of the helper)."""
    expected = _repo_root() / "config" / "pain_points.yaml"
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_app_path("config/pain_points.yaml") == expected


def test_simulated_frozen_mode_uses_app_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When sys.frozen is True paths resolve to ~/Library/Application Support."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_bundle = tmp_path / "SalesCopilot.app"
    executable = fake_bundle / "Contents" / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("HOME", str(fake_home))

    expected_base = fake_home / "Library" / "Application Support" / "SalesCopilot"
    assert paths.resolve_app_support() == expected_base
    assert paths.resolve_app_path("config/pain_points.yaml") == expected_base / "config" / "pain_points.yaml"
    expected_resource = fake_bundle / "Contents" / "Resources" / "dashboard" / "index.html"
    assert paths.resolve_app_resource("dashboard/index.html") == expected_resource


def test_ensure_app_support_tree_creates_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_bundle = tmp_path / "SalesCopilot.app"
    executable = fake_bundle / "Contents" / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("HOME", str(fake_home))

    paths.ensure_app_support_tree()
    base = paths.resolve_app_support()
    for subdir in (".vnx-data", ".vnx-data/audit", "config", "data/sessions", "data/logs"):
        assert (base / subdir).is_dir()


def test_seed_app_support_defaults_copies_bundled_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First-run seeding copies bundled defaults into app support."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_bundle = tmp_path / "SalesCopilot.app"
    resources = fake_bundle / "Contents" / "Resources"
    resources.mkdir(parents=True)
    executable = fake_bundle / "Contents" / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()

    # Create fake bundled defaults.
    (resources / "config").mkdir()
    (resources / "config" / "pain_points.yaml").write_text("test: yes\n", encoding="utf-8")
    (resources / "data" / "samples").mkdir(parents=True)
    (resources / "data" / "samples" / "sample-aha.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setenv("HOME", str(fake_home))

    paths.seed_app_support_defaults()
    base = paths.resolve_app_support()
    assert (base / "config" / "pain_points.yaml").exists()
    assert (base / "data" / "samples" / "sample-aha.json").exists()


def test_seed_app_support_defaults_is_no_op_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeding must not touch the repo in dev mode."""
    # is_frozen_app is False by default; the function should return immediately.
    paths.seed_app_support_defaults()
    assert paths.is_frozen_app() is False
