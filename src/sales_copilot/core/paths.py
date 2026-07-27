"""Application path resolution helpers for dev and frozen macOS .app builds.

In a repository checkout the project root (where ``pyproject.toml`` lives) is used
as the base so existing dev behaviour and tests keep working. Inside a py2app
frozen ``.app`` the base switches to ``~/Library/Application Support/SalesCopilot``
so the app works regardless of the current working directory.

Read-only bundled resources (dashboard, sample audio, binaries) are resolved
against the ``Contents/Resources`` directory of the frozen bundle.
"""

from __future__ import annotations

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


def _repo_root() -> Path:
    """Locate the repository root by walking up from this source file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    # Defensive fallback: src/sales_copilot/core/paths.py -> src/sales_copilot -> src -> repo
    return here.parents[2]


def resolve_app_support() -> Path:
    """Return the base directory for writable application data.

    - Frozen ``.app``: ``~/Library/Application Support/SalesCopilot``.
    - Dev/repo checkout: the repository root.

    The returned path is *not* created automatically; callers that need the
    directory should call ``ensure_app_support_tree()`` or create it themselves.
    """
    if is_frozen_app():
        return Path.home() / "Library" / "Application Support" / _APP_SUPPORT_DIR_NAME
    return _repo_root()


def resolve_app_path(relative: str | Path) -> Path:
    """Resolve ``relative`` against the writable app-support base.

    In dev this preserves the existing cwd-relative layout (e.g.
    ``config/pain_points.yaml`` resolves to ``<repo>/config/pain_points.yaml``);
    in a frozen app it resolves to
    ``~/Library/Application Support/SalesCopilot/config/pain_points.yaml``.
    """
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"relative path required, got {relative!r}")
    return resolve_app_support() / rel


def resolve_app_resource(relative: str | Path) -> Path:
    """Resolve a bundled read-only resource path.

    In a frozen ``.app`` this points to ``Contents/Resources/<relative>`` so
    static assets ship inside the signed bundle. In dev it falls back to the
    repository root.
    """
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"relative path required, got {relative!r}")
    if is_frozen_app():
        return _bundle_resources_dir() / rel
    return _repo_root() / rel


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
    This is a no-op in dev because config/data already live in the repo.
    """
    if not is_frozen_app():
        return
    resources = _bundle_resources_dir()
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
