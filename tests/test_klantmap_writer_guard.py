"""Grep-guard: only the known set of modules may write into a client folder.

klantmap-als-eenheid D3 plan: "een grep-guard dat geen nieuwe schrijver buiten
deze paden transcripten in een klantmap zet." A client folder under
``UPLOAD_ROOT`` mixes uploaded prep docs, the dossier, and (D3) the
``gesprekken/`` call archive -- a future addition that writes into it from
somewhere else, outside this reviewed set, would be easy to miss in review.
This test fails loudly the moment a new call site appears, forcing that
addition through this allowlist instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "sales_copilot"

# Every module allowed to create a directory under a client folder today:
# - core/context_docs.py: resolve_client_dir's own implementation, and
#   write_dossier_transcript's dossier/ subfolder.
# - websocket/hub_upload.py: the /upload endpoint's per-client upload dir.
# - modules/reports/__main__.py: the gesprekken/ call archive (D3).
_ALLOWED = {
    "core/context_docs.py",
    "websocket/hub_upload.py",
    "modules/reports/__main__.py",
}

_RESOLVE_CREATE_PATTERN = re.compile(r"resolve_client_dir\([^)]*create=True")
_UPLOAD_ROOT_MKDIR_PATTERN = re.compile(r"UPLOAD_ROOT\b")


def _writes_into_client_dir(text: str) -> bool:
    if _RESOLVE_CREATE_PATTERN.search(text):
        return True
    return bool(_UPLOAD_ROOT_MKDIR_PATTERN.search(text) and ".mkdir(" in text)


def test_only_known_modules_write_into_a_client_folder() -> None:
    offenders = []
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if _writes_into_client_dir(text) and rel not in _ALLOWED:
            offenders.append(rel)
    assert offenders == [], (
        f"Unreviewed client-folder writer(s) found: {offenders}. "
        "Add to _ALLOWED in this test only after confirming the write path "
        "(dossier/, gesprekken/, or the upload root) is intentional."
    )
