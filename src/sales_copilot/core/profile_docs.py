"""Sandboxed storage and loading for the persistent seller profile.

One markdown doc ("dit weet ik al, dit verkoop ik, mijn methode") that every
deep-insight run reuses so the engine skips basic explanations of what the
seller already knows and goes straight to the sharp follow-up question. Lives
at ``data/profile/profile.md``, a sibling of ``data/clients/`` (the
context-docs/client-dossier upload root), and follows the same
``resolve_app_path``-based sandbox pattern as ``core/context_docs.py``: the
target path is resolved and verified to stay under its root before any read
or write, even though the filename itself is fixed and never user-supplied.
"""

from __future__ import annotations

import stat
from pathlib import Path

from sales_copilot.core.outbound_policy import apply_outbound_pii, sanitize_for_outbound
from sales_copilot.core.paths import resolve_app_path

PROFILE_ROOT = resolve_app_path("data/profile")
PROFILE_FILENAME = "profile.md"
MAX_PROFILE_CHARS = 4_000


def _profile_path(root: Path | None = None) -> Path:
    root = (root or PROFILE_ROOT).resolve()
    candidate = (root / PROFILE_FILENAME).resolve()
    # Defense-in-depth, matching context_docs' traversal-proofing -- the
    # filename is a fixed constant, never user-supplied, but this keeps the
    # same "verify containment before touching disk" shape everywhere the
    # sandbox pattern is used.
    candidate.relative_to(root)
    return candidate


def profile_exists(*, root: Path | None = None) -> bool:
    return _profile_path(root).is_file()


def read_profile_raw(*, root: Path | None = None) -> str:
    """Return the profile doc's raw, unredacted content for editing in the wizard.

    Never sent anywhere -- this is the operator reading/editing their own
    doc locally. Use ``load_profile_context`` for anything that ends up in an
    LLM prompt.
    """
    path = _profile_path(root)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def load_profile_context(
    *, root: Path | None = None, max_chars: int = MAX_PROFILE_CHARS, provider: str | None = None
) -> str:
    """Load the seller profile, PII-sanitized and capped, for use in an LLM prompt.

    Returns an empty string when no profile doc exists yet -- callers treat
    that as "no profile section", not an error.

    ``provider`` should be the destination LLM client's own provider (e.g.
    ``INSIGHT_PROVIDER`` for the deep-insight lane), not the global
    ``LLM_PROVIDER`` -- the two can differ, and the PII policy must be
    evaluated against the provider that will actually receive this text. When
    ``provider`` is None, this falls back to the env-configured
    ``LLM_PROVIDER`` via ``sanitize_for_outbound``.
    """
    content = read_profile_raw(root=root).strip()
    if not content:
        return ""
    if provider is not None:
        return apply_outbound_pii(content, provider=provider, allow_local=True)[:max_chars]
    return sanitize_for_outbound(content, provider=None, allow_local=True)[:max_chars]


def write_profile_document(content: str, *, root: Path | None = None) -> Path:
    """Create or overwrite the seller profile doc. Owner-only file permissions."""
    root = (root or PROFILE_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(stat.S_IRWXU)
    path = _profile_path(root)
    path.write_text(content, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
