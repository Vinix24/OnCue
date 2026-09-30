"""Sandboxed storage and loading for uploaded context documents and the client dossier.

Two related but distinct uses of the same ``data/clients/<slug>/`` root:

- **Context docs** (this session's prep docs): explicitly chosen by upload id
  and loaded via ``load_context_documents`` -- unchanged, pre-dates the
  dossier.
- **Client dossier** (PR-D4): everything else in a client's folder --
  transcripts and notes saved from *earlier* sessions with the same
  customer. Opt-in per session via an explicit client-slug link; loaded via
  ``load_client_dossier``, most-recent-first so the oldest material is what
  gets dropped when the cap is hit, not the newest.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from sales_copilot.core.outbound_policy import apply_outbound_pii, sanitize_for_outbound
from sales_copilot.core.paths import resolve_app_path

# "klantmap-als-eenheid" plan: KLANTEN_ROOT lets an operator point the client
# folder at an existing location outside the app-support tree (e.g. a folder
# already used for client work elsewhere). Default is unchanged -- the
# existing data/clients app-support path.
_KLANTEN_ROOT_ENV_VAR = "KLANTEN_ROOT"


def _resolve_upload_root() -> Path:
    override = os.environ.get(_KLANTEN_ROOT_ENV_VAR, "").strip()
    return Path(override).expanduser() if override else resolve_app_path("data/clients")


UPLOAD_ROOT = _resolve_upload_root()
MAX_CONTEXT_CHARS = 4_000
MAX_DOSSIER_CHARS = 8_000
DOSSIER_ALLOWED_EXTENSIONS = frozenset({".txt", ".json", ".md"})


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


def _sanitize_for_destination(content: str, *, provider: str | None) -> str:
    """Apply the outbound PII policy for the destination that will actually receive this text.

    ``provider`` is the LLM client that consumes the loaded content (e.g. an
    ``InsightEngine`` on its own ``INSIGHT_PROVIDER``). When it is None, the
    caller doesn't know its destination yet, so this falls back to the
    env-configured ``LLM_PROVIDER`` (``sanitize_for_outbound``'s existing
    behaviour) -- never a silent skip, just a less specific destination.
    """
    if provider is not None:
        return apply_outbound_pii(content, provider=provider, allow_local=True)
    return sanitize_for_outbound(content, allow_local=True)


def load_context_documents(
    paths: list[str],
    *,
    upload_root: Path | None = None,
    max_chars: int = MAX_CONTEXT_CHARS,
    provider: str | None = None,
) -> str:
    """Load only validated uploaded text documents.

    ``provider`` should be the destination LLM client's own provider (e.g.
    ``INSIGHT_PROVIDER`` for the deep-insight lane), not the global
    ``LLM_PROVIDER`` -- the two can differ, and the PII policy must be
    evaluated against the provider that will actually receive this text.
    """

    root = (upload_root or UPLOAD_ROOT).resolve()
    chunks: list[str] = []
    for resolved_path in resolve_context_doc_ids(_relative_ids(paths, root), upload_root=root):
        path = Path(resolved_path)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if content.strip():
            chunks.append(f"[{path.name}]\n{_sanitize_for_destination(content.strip(), provider=provider)}")
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


# --- Client dossier (PR-D4) --------------------------------------------------


def slugify_client_name(value: str) -> str:
    """Normalize a free-typed client/company name into a filesystem-safe slug.

    Shared by the upload endpoint (``hub_upload.py``) and the client-dossier
    link on a session's start-call config, so "choose or create a client
    folder" always resolves to the same folder for the same typed name.
    """
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in str(value or "").strip())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug or "default"


def resolve_client_dir(
    slug: str,
    *,
    upload_root: Path | None = None,
    create: bool = False,
) -> Path | None:
    """Resolve a client slug to its sandboxed directory under the upload root.

    The slug is always re-normalized through ``slugify_client_name`` first, so
    this can never escape ``upload_root`` regardless of what the caller passes
    in. Returns ``None`` when the resolved directory does not exist and
    ``create`` is False.
    """
    root = (upload_root or UPLOAD_ROOT).resolve()
    normalized = slugify_client_name(slug)
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
        candidate.chmod(stat.S_IRWXU)
        return candidate
    return candidate if candidate.is_dir() else None


def parse_client_slug_from_payload(payload: dict[str, object]) -> str | None:
    """Read the optional client-dossier link from a start-call payload.

    Accepts a top-level ``client_slug`` or a nested ``{"dossier": {"client_slug": ...}}``
    (the shape the dashboard's client picker sends). Always re-normalized through
    ``slugify_client_name`` -- the raw payload value is never trusted as-is. Missing/empty
    means no client link. Shared by ``sales_copilot.__main__._parse_call_config`` (builds
    ``CallConfig.client_slug``) and ``hub_core.extract_start_call_config`` (klantmap-als-
    eenheid D2: loads ``klant.yaml`` for this slug before the call starts) so both agree on
    exactly the same slug for the same payload.
    """
    raw = payload.get("client_slug")
    if not isinstance(raw, str) or not raw.strip():
        dossier_payload = payload.get("dossier")
        if isinstance(dossier_payload, dict):
            raw = dossier_payload.get("client_slug")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return slugify_client_name(raw)


def list_client_slugs(*, upload_root: Path | None = None) -> list[str]:
    """Return the slugs of every existing client folder, alphabetically."""
    root = (upload_root or UPLOAD_ROOT).resolve()
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def load_client_dossier(
    slug: str,
    *,
    upload_root: Path | None = None,
    max_chars: int = MAX_DOSSIER_CHARS,
    exclude: set[str] | None = None,
    provider: str | None = None,
) -> str:
    """Load prior material from a client's folder, most-recent-first, capped.

    ``exclude`` is the set of absolute paths already loaded as this session's
    context docs (prep docs uploaded into the same folder) -- skipped here so
    the same file's content is never injected into the prompt twice. Files
    are read newest-mtime-first and accumulated until ``max_chars`` would be
    exceeded, so the OLDEST material is what gets dropped, matching
    ``InsightEngine._build_transcript_text``'s "oldest lines drop first"
    behaviour for the live transcript. The kept files are then joined in
    chronological (oldest-of-kept-first) order for a readable narrative.

    ``provider`` should be the destination LLM client's own provider (e.g.
    ``INSIGHT_PROVIDER``), not the global ``LLM_PROVIDER`` -- see
    ``_sanitize_for_destination``.
    """
    directory = resolve_client_dir(slug, upload_root=upload_root, create=False)
    if directory is None:
        return ""
    excluded_paths = {str(Path(p).resolve()) for p in (exclude or set())}
    # klantmap-als-eenheid D3: new dossier saves land in `dossier/` (see
    # write_dossier_transcript below); a folder from before that convention
    # keeps its dossier material loose in the folder root. Both are read here
    # -- `gesprekken/` (the call archive, also D3) never is, since it is not
    # in either scan (`directory.iterdir()` does not descend into it, and it
    # is not added as a second scan root the way `dossier/` is below).
    scan_dirs = [directory]
    dossier_dir = directory / "dossier"
    if dossier_dir.is_dir():
        scan_dirs.append(dossier_dir)
    candidates = [
        entry
        for scan_dir in scan_dirs
        for entry in scan_dir.iterdir()
        if entry.is_file()
        and entry.suffix.lower() in DOSSIER_ALLOWED_EXTENSIONS
        and str(entry.resolve()) not in excluded_paths
    ]
    candidates.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)

    kept: list[str] = []
    total_chars = 0
    for entry in candidates:
        try:
            content = entry.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not content:
            continue
        chunk = f"[{entry.name}]\n{_sanitize_for_destination(content, provider=provider)}"
        total_chars += len(chunk) + 2
        if total_chars > max_chars:
            break
        kept.append(chunk)
    return "\n\n".join(reversed(kept))


def write_dossier_transcript(
    slug: str,
    filename_stub: str,
    content: str,
    *,
    upload_root: Path | None = None,
) -> Path:
    """Save a session transcript into a client's dossier folder (post-call, opt-in).

    Always writes into the ``dossier/`` subfolder (klantmap-als-eenheid D3), never
    the client folder's root -- that root also receives this session's own
    upload-endpoint context docs (``hub_upload.py``) and, for a client with a
    call archived, its ``gesprekken/`` folder, so a fresh dossier save needs its
    own place to avoid mixing with either. ``load_client_dossier`` reads both
    ``dossier/`` and (for a pre-D3 folder) the root, so an existing install's
    material is still found.

    ``filename_stub`` should already be unique and sortable (e.g. an ISO
    timestamp + session id) -- this is a plain write, not an append, so a
    collision would silently overwrite a prior save.
    """
    directory = resolve_client_dir(slug, upload_root=upload_root, create=True)
    if directory is None:
        raise ValueError(f"Invalid client slug: {slug!r}")
    dossier_dir = directory / "dossier"
    dossier_dir.mkdir(parents=True, exist_ok=True)
    dossier_dir.chmod(stat.S_IRWXU)
    path = dossier_dir / f"{filename_stub}.md"
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
