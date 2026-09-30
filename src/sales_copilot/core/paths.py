"""Application path resolution helpers for dev, installed, and frozen macOS .app builds.

Three modes, distinguished at runtime:

- **Dev/editable checkout**: a ``pyproject.toml`` is found by walking up from
  this source file (true for a repo clone and for ``pip install -e .``, since
  the editable install still points back at the real repo tree). The project
  root is used as the base, preserving the existing cwd-relative layout.
- **Installed (non-editable) package**: a plain ``pip install .`` — no
  ``pyproject.toml`` reachable from this file, because only the
  ``sales_copilot`` package itself was copied into ``site-packages``. Writable
  data goes to a per-OS user data directory (never ``site-packages`` itself —
  that directory is not guaranteed writable and gets wiped on upgrade/reinstall);
  read-only resources (dashboard, presentation, config defaults) are resolved
  against a ``_resources`` directory shipped inside the installed package via
  ``[tool.hatch.build.targets.wheel.force-include]`` in ``pyproject.toml``.
- **Frozen py2app ``.app`` bundle**: ``sys.frozen`` is set. Writable data goes
  to ``~/Library/Application Support/SalesCopilot``; read-only resources
  resolve against the bundle's ``Contents/Resources`` directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APP_SUPPORT_DIR_NAME = "SalesCopilot"


def is_frozen_app() -> bool:
    """Return ``True`` when running inside a py2app/frozen ``.app`` bundle."""
    return getattr(sys, "frozen", False) is True


def _bundle_resources_dir() -> Path:
    """Return the frozen ``.app`` ``Contents/Resources`` directory.

    In a py2app bundle ``sys.executable`` points to the executable inside
    ``Contents/MacOS``, so two parents up is ``Contents/Resources``.
    """
    return Path(sys.executable).resolve().parent.parent / "Resources"


def _find_repo_root() -> Path | None:
    """Locate the repository root by walking up from this source file.

    Returns ``None`` when no ``pyproject.toml`` is found -- the case for a
    normal (non-editable) ``pip install``, where only the ``sales_copilot``
    package is present on disk, with no attached repo checkout.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _installed_package_root() -> Path:
    """Return the installed ``sales_copilot`` package directory.

    ``paths.py`` lives at ``sales_copilot/core/paths.py``, so two parents up is
    the package root -- ``.../site-packages/sales_copilot`` for a normal
    install. Bundled read-only resources ship inside it at ``_resources/``
    (see ``[tool.hatch.build.targets.wheel.force-include]``).
    """
    return Path(__file__).resolve().parent.parent


def _installed_package_resources_dir() -> Path:
    """Read-only resources bundled inside an installed (non-editable) package."""
    return _installed_package_root() / "_resources"


def _installed_package_data_dir() -> Path:
    """Per-OS user data directory for a normal (non-editable, non-frozen) install.

    Never ``site-packages`` itself: that directory is not guaranteed writable
    (system Python, read-only volumes) and its contents are not preserved
    across an upgrade or reinstall.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / _APP_SUPPORT_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_DIR_NAME
    # Linux / other POSIX: XDG Base Directory spec.
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / _APP_SUPPORT_DIR_NAME


def resolve_app_support() -> Path:
    """Return the base directory for writable application data.

    - Frozen ``.app``: ``~/Library/Application Support/SalesCopilot``.
    - Dev/editable checkout: the repository root.
    - Installed (non-editable) package: a per-OS user data directory --
      never ``site-packages`` (see ``_installed_package_data_dir()``).

    The returned path is *not* created automatically; callers that need the
    directory should call ``ensure_app_support_tree()`` or create it themselves.
    """
    if is_frozen_app():
        return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_DIR_NAME
    repo_root = _find_repo_root()
    if repo_root is not None:
        return repo_root
    return _installed_package_data_dir()


def resolve_app_path(relative: str | Path) -> Path:
    """Resolve ``relative`` against the writable app-support base.

    In dev this preserves the existing cwd-relative layout (e.g.
    ``config/pain_points.yaml`` resolves to ``<repo>/config/pain_points.yaml``);
    in a frozen app, or a normal installed package, it resolves under the
    per-mode app-support base (see ``resolve_app_support()``).
    """
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"relative path required, got {relative!r}")
    return resolve_app_support() / rel


def resolve_app_resource(relative: str | Path) -> Path:
    """Resolve a bundled read-only resource path.

    - Frozen ``.app``: ``Contents/Resources/<relative>``.
    - Dev/editable checkout: the repository root.
    - Installed (non-editable) package: the ``_resources/<relative>`` directory
      shipped inside the installed package (see
      ``_installed_package_resources_dir()``).
    """
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"relative path required, got {relative!r}")
    if is_frozen_app():
        return _bundle_resources_dir() / rel
    repo_root = _find_repo_root()
    if repo_root is not None:
        return repo_root / rel
    return _installed_package_resources_dir() / rel


def ensure_app_support_tree() -> None:
    """Create the standard writable subdirectories under the app-support base."""
    base = resolve_app_support()
    for subdir in (
        # App-support scoped (under resolve_app_support()), not repo-root VNX governance.
        ".vnx-data",
        ".vnx-data/audit",
        "config",
        "config/presets",
        "data",
        "data/sessions",
        "data/reports",
        "data/clients",
        "data/profile",
        "data/logs",
        "data/samples",
        "data/feedback",
    ):
        (base / subdir).mkdir(parents=True, exist_ok=True)


def _seed_file(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` if ``dst`` does not already exist."""
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())


def seed_app_support_defaults() -> None:
    """Copy bundled default config/data into the writable app-support tree.

    Idempotent: existing files are preserved so user edits are not overwritten.
    This is a no-op in a dev/editable checkout because config/data already live
    in the repo; a frozen ``.app`` and a normal installed package both seed from
    their respective bundled ``_resources``/``Contents/Resources`` tree.
    """
    if is_frozen_app():
        resources = _bundle_resources_dir()
    elif _find_repo_root() is None:
        resources = _installed_package_resources_dir()
    else:
        return
    app_support = resolve_app_support()
    # Read-only defaults that the app needs to function even when the user has
    # not manually populated app support.
    rel_paths = (
        "config/i18n",
        "config/pain_points.yaml",
        "config/objections.yaml",
        "config/opportunities.yaml",
        "config/objection_responses.yaml",
        "config/opportunity_responses.yaml",
        "config/phases.yaml",
        "config/llm_models.yaml",
        "config/eval_model_prices.yaml",
        "config/presets",
        "data/samples",
    )
    for rel in rel_paths:
        src = resources / rel
        if not src.exists():
            continue
        dst = app_support / rel
        if src.is_dir():
            for child in src.rglob("*"):
                if child.is_file():
                    _seed_file(child, dst / child.relative_to(src))
        else:
            _seed_file(src, dst)
