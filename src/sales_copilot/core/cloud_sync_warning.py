"""Detect when the klantmap root lives inside a folder a cloud-sync client watches.

``klant.yaml`` and the client dossier/archive can hold contact names and
transcript excerpts (klantmap-als-eenheid, D1/D3). If ``KLANTEN_ROOT``
resolves to a folder under iCloud Drive, a File-Provider cloud-storage mount,
Dropbox, or an iCloud-synced Desktop/Documents, that content leaves the
machine even though nothing in this codebase uploaded it.

This module only detects and describes that condition -- it never blocks.
Per the plan: "Een waarschuwing, geen blokkade: de keuze is aan de gebruiker."
Callers (setup menu / start-call, D2) decide how to surface the warning.

``KLANTEN_ROOT`` is read from the environment in exactly one place --
``context_docs._resolve_upload_root`` -- and exposed as
``context_docs.UPLOAD_ROOT``. This module reads that module attribute at call
time (not via a top-level ``from ... import UPLOAD_ROOT``, which would bind
the value at import time and not see a test's monkeypatch) rather than
resolving the env var a second time, so the two never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sales_copilot.core import context_docs


class CloudSyncKind(StrEnum):
    """The four folder kinds called out by the plan, in detection priority order."""

    ICLOUD_DRIVE = "icloud_drive"
    CLOUD_STORAGE_PROVIDER = "cloud_storage_provider"
    DROPBOX = "dropbox"
    ICLOUD_DESKTOP_DOCUMENTS = "icloud_desktop_documents"


@dataclass(frozen=True)
class CloudSyncWarning:
    kind: CloudSyncKind
    path: Path
    message: str


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def detect_cloud_sync(path: Path, *, home: Path | None = None) -> CloudSyncWarning | None:
    """Return a warning when ``path`` lives under a known cloud-synced location.

    ``home`` overrides ``Path.home()`` -- every check below is relative to it,
    so tests can point it at a fake directory tree under ``tmp_path`` and
    exercise each of the four kinds without touching the real filesystem or
    depending on the actual sync state of this machine.

    Checked in order: iCloud Drive, then Library/CloudStorage, then Dropbox,
    then (only if the iCloud Desktop-and-Documents marker exists) Desktop or
    Documents. A path already under iCloud Drive is reported as
    ``ICLOUD_DRIVE`` even if it happens to sit under the Desktop-and-Documents
    subtree of it -- that is the more specific, correct explanation.
    """
    home_dir = (home or Path.home()).resolve()
    resolved = path.resolve()

    icloud_drive_root = home_dir / "Library" / "Mobile Documents"
    if _under(resolved, icloud_drive_root):
        return CloudSyncWarning(
            kind=CloudSyncKind.ICLOUD_DRIVE,
            path=resolved,
            message=(
                "Deze klantmap staat in iCloud Drive (Library/Mobile Documents). "
                "Transcripten en dossiers hierin worden naar iCloud gesynchroniseerd."
            ),
        )

    cloud_storage_root = home_dir / "Library" / "CloudStorage"
    if _under(resolved, cloud_storage_root):
        return CloudSyncWarning(
            kind=CloudSyncKind.CLOUD_STORAGE_PROVIDER,
            path=resolved,
            message=(
                "Deze klantmap staat onder Library/CloudStorage (Dropbox, OneDrive of "
                "Google Drive via de File Provider). Inhoud wordt naar die provider "
                "gesynchroniseerd."
            ),
        )

    dropbox_root = home_dir / "Dropbox"
    if _under(resolved, dropbox_root):
        return CloudSyncWarning(
            kind=CloudSyncKind.DROPBOX,
            path=resolved,
            message="Deze klantmap staat in Dropbox. Inhoud wordt naar Dropbox gesynchroniseerd.",
        )

    icloud_desktop_marker = icloud_drive_root / "com~apple~CloudDocs" / "Desktop"
    if icloud_desktop_marker.exists():
        for folder_name in ("Desktop", "Documents"):
            folder_root = home_dir / folder_name
            if _under(resolved, folder_root):
                return CloudSyncWarning(
                    kind=CloudSyncKind.ICLOUD_DESKTOP_DOCUMENTS,
                    path=resolved,
                    message=(
                        "iCloud Drive synchroniseert Bureaublad en Documenten op deze Mac. "
                        f"Deze klantmap staat onder {folder_name} en wordt daardoor naar "
                        "iCloud gesynchroniseerd."
                    ),
                )

    return None


def klanten_root_cloud_sync_warning(*, home: Path | None = None) -> CloudSyncWarning | None:
    """Warning for the currently configured ``KLANTEN_ROOT``, or ``None`` if it is local."""
    return detect_cloud_sync(context_docs.UPLOAD_ROOT, home=home)
