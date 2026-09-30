"""Cloud-sync detection for the klantmap root (klantmap-als-eenheid, D4).

Every check below points ``home`` at a fake directory tree under ``tmp_path``
so the four real macOS locations (iCloud Drive, Library/CloudStorage,
Dropbox, iCloud-synced Desktop/Documents) are exercised without touching the
actual filesystem or depending on this machine's real sync state.

``KLANTEN_ROOT`` itself is read in exactly one place,
``context_docs._resolve_upload_root`` -- this module has no resolver of its
own, so these tests drive it via ``context_docs.UPLOAD_ROOT`` (module
attribute, monkeypatched directly, or recomputed from the env var the same
way the app does at import time).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sales_copilot.core import cloud_sync_warning as csw
from sales_copilot.core import context_docs
from sales_copilot.core.cloud_sync_warning import CloudSyncKind


def test_icloud_drive_detected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.ICLOUD_DRIVE


def test_cloud_storage_provider_detected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / "Library" / "CloudStorage" / "Dropbox-Personal" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.CLOUD_STORAGE_PROVIDER


def test_dropbox_detected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / "Dropbox" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.DROPBOX


def test_icloud_desktop_documents_detected_when_marker_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    marker = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop"
    marker.mkdir(parents=True)
    client_root = home / "Desktop" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.ICLOUD_DESKTOP_DOCUMENTS


def test_icloud_documents_detected_when_marker_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    marker = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop"
    marker.mkdir(parents=True)
    client_root = home / "Documents" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.ICLOUD_DESKTOP_DOCUMENTS


def test_desktop_not_flagged_without_icloud_desktop_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / "Desktop" / "clients"
    client_root.mkdir(parents=True)
    # No com~apple~CloudDocs/Desktop marker created: Desktop-and-Documents
    # sync is off on this (fake) machine, so Desktop is an ordinary local folder.

    assert csw.detect_cloud_sync(client_root, home=home) is None


def test_plain_local_path_not_flagged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / "Projects" / "sales-copilot" / "data" / "clients"
    client_root.mkdir(parents=True)

    assert csw.detect_cloud_sync(client_root, home=home) is None


def test_icloud_drive_wins_over_desktop_documents_kind(tmp_path: Path) -> None:
    """A path already inside iCloud Drive is ICLOUD_DRIVE, not the Desktop/Documents kind."""
    home = tmp_path / "home"
    client_root = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop" / "clients"
    client_root.mkdir(parents=True)

    warning = csw.detect_cloud_sync(client_root, home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.ICLOUD_DRIVE


def test_klanten_root_cloud_sync_warning_uses_context_docs_upload_root(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    client_root = home / "Dropbox" / "clients"
    client_root.mkdir(parents=True)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", client_root)

    warning = csw.klanten_root_cloud_sync_warning(home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.DROPBOX


def test_klanten_root_cloud_sync_warning_none_for_local_root(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    client_root = tmp_path / "local" / "data" / "clients"
    client_root.mkdir(parents=True)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", client_root)

    assert csw.klanten_root_cloud_sync_warning(home=home) is None


def test_klanten_root_with_tilde_under_mocked_home_is_flagged(tmp_path: Path, monkeypatch) -> None:
    """Regression: a literal ``~`` in ``KLANTEN_ROOT`` must expand before detection.

    ``context_docs._resolve_upload_root`` is the one place that reads the env
    var, and it calls ``.expanduser()``. Before the fix,
    ``cloud_sync_warning.resolve_klanten_root`` read ``KLANTEN_ROOT`` a second
    time with a plain ``Path(raw)`` and no ``expanduser()``, so a value like
    ``~/Desktop/BUSINESS/clients`` stayed a literal ``~/...`` path, never
    matched anything under the (mocked) home directory, and silently skipped
    the warning -- exactly when an iCloud-synced Desktop most needed it.
    """
    home = tmp_path / "home"
    marker = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Desktop"
    marker.mkdir(parents=True)
    client_root = home / "Desktop" / "BUSINESS" / "clients"
    client_root.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KLANTEN_ROOT", "~/Desktop/BUSINESS/clients")
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", context_docs._resolve_upload_root())

    warning = csw.klanten_root_cloud_sync_warning(home=home)

    assert warning is not None
    assert warning.kind is CloudSyncKind.ICLOUD_DESKTOP_DOCUMENTS


def test_klanten_root_env_var_read_only_in_context_docs() -> None:
    """Grep-guard: ``KLANTEN_ROOT`` must be read from the environment in exactly one place.

    A second independent reader (like the old
    ``cloud_sync_warning.resolve_klanten_root``) can silently diverge from
    ``context_docs._resolve_upload_root`` -- as it did before this fix, by
    skipping ``.expanduser()``. This walks every ``.py`` file under ``src/``
    and fails if any module other than ``core/context_docs.py`` both names the
    literal string ``"KLANTEN_ROOT"`` (as an ``os.environ``/``os.getenv`` key,
    directly or via an intermediate constant) and performs any environment
    read at all. Checking same-line co-occurrence is not enough: the old
    ``cloud_sync_warning.py`` stored the name in ``_KLANTEN_ROOT_ENV`` on one
    line and called ``os.environ.get(_KLANTEN_ROOT_ENV, ...)`` on another --
    the guard must catch that split just as reliably as an inline literal.
    """
    src_root = Path(__file__).resolve().parents[2] / "src"
    allowed_file = src_root / "sales_copilot" / "core" / "context_docs.py"
    env_read_pattern = re.compile(r"os\.(environ|getenv)")

    violations: list[str] = []
    for path in src_root.rglob("*.py"):
        if path == allowed_file:
            continue
        text = path.read_text(encoding="utf-8")
        if "KLANTEN_ROOT" not in text:
            continue
        has_literal = any(
            isinstance(node, ast.Constant) and node.value == "KLANTEN_ROOT"
            for node in ast.walk(ast.parse(text, filename=str(path)))
        )
        if has_literal and env_read_pattern.search(text):
            violations.append(str(path))

    assert violations == [], (
        "KLANTEN_ROOT must be read from the environment only in "
        f"core/context_docs.py, found readers in: {violations}"
    )
