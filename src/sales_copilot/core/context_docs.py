"""Sandboxed storage and loading for uploaded context documents."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from sales_copilot.core.outbound_policy import sanitize_for_outbound
from sales_copilot.core.paths import resolve_app_path

UPLOAD_ROOT = resolve_app_path("data/clients")
MAX_CONTEXT_CHARS = 4_000


def resolve_context_doc_ids(
    document_ids: list[str],
    *,
    upload_root: Path | None = None,
) -> list[str]:
    """Resolve relative upload IDs while preventing traversal outside the upload root."""

    root = (upload_root or UPLOAD_ROOT).resolve()
    resolved: list[str] = []
    for document_id in document_ids:
        if not isinstance(document_id, str) or not document_id.strip():
            continue
        candidate = (root / document_id).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            resolved.append(str(candidate))
    return resolved


def load_context_documents(
    paths: list[str],
    *,
    upload_root: Path | None = None,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Load only validated uploaded text documents."""

    root = (upload_root or UPLOAD_ROOT).resolve()
    chunks: list[str] = []
    for resolved_path in resolve_context_doc_ids(_relative_ids(paths, root), upload_root=root):
        path = Path(resolved_path)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if content.strip():
            chunks.append(f"[{path.name}]\n{sanitize_for_outbound(content.strip(), allow_local=True)}")
    return "\n\n".join(chunks)[:max_chars]


def write_session_context_manifest(
    session_id: str,
    paths: list[str],
    *,
    sessions_root: Path = resolve_app_path("data/sessions"),
) -> Path:
    """Record uploaded documents used by a session so an erasure can remove them."""

    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_dir.chmod(stat.S_IRWXU)
    manifest = session_dir / "context_docs.json"
    manifest.write_text(json.dumps({"paths": paths}, indent=2), encoding="utf-8")
    manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return manifest


def _relative_ids(paths: list[str], root: Path) -> list[str]:
    relative: list[str] = []
    for raw_path in paths:
        if not isinstance(raw_path, str):
            continue
        try:
            relative.append(str(Path(raw_path).resolve().relative_to(root)))
        except ValueError:
            continue
    return relative
