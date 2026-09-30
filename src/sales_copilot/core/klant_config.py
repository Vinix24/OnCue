"""Schema, loader and path safety for the per-client ``klant.yaml`` file.

Part of the "klantmap-als-eenheid" plan (D1 of 4). A client folder under
``context_docs.UPLOAD_ROOT`` (env override: ``KLANTEN_ROOT``) may optionally
contain a ``klant.yaml`` that couples "Prospect bedrijf", "Industry" and the
dossier folder that today have to be re-typed by hand even after picking a
client. This module only covers the schema/loader/validation/path-safety
slice; deriving ``prospect_company``/``prospect_industry``/``call_terms`` in
``/api/start-call`` (D2), the ``client_slug`` session column and
``gesprekken/`` archive (D3), and the cloud-sync-folder warning (D4) are
separate dispatches.

Backward compatibility is load-bearing: a folder without ``klant.yaml`` (every
existing client folder today) must keep working exactly as before --
``load_klant_config`` returns ``None`` for it, and callers fall back to the
folder name as the company name with loose files as the dossier, unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sales_copilot.core import context_docs

KLANT_YAML_FILENAME = "klant.yaml"

_MAX_CONTACTPERSONEN = 20
_MAX_TERMEN = 200
_MAX_TERM_LENGTH = 64

# Only "lokaal" ships in this version -- HubSpot and webhook delivery are
# explicitly out of scope for klantmap-als-eenheid (plan Approach section).
_SUPPORTED_AFLEVERING = "lokaal"


class KlantConfigError(ValueError):
    """Raised when ``klant.yaml`` fails schema validation, or a slug is unsafe.

    The message always names the offending field and the reason, so a
    misconfigured client folder fails loudly with something actionable
    instead of a bare pydantic traceback.
    """


class KlantConfig(BaseModel):
    """Validated contents of a client folder's ``klant.yaml``.

    ``extra="forbid"`` so a typo'd or future-version field fails validation
    now rather than being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    bedrijf: str = Field(min_length=1)
    branche: str | None = None
    contactpersonen: list[str] = Field(default_factory=list, max_length=_MAX_CONTACTPERSONEN)
    termen: list[str] = Field(default_factory=list, max_length=_MAX_TERMEN)
    privacy: Literal["local", "tenant", "public"] | None = None
    # Semantics locked by D3 (klantmap-als-eenheid, retention sweep +
    # purge_session wiring): 0 means "voor altijd" -- the SAME meaning as the
    # *global* DATA_RETENTION_DAYS=0 sentinel (core/retention.py), so a client
    # is never auto-purged by leaving this at its default of 0/absent. None
    # (the field absent from klant.yaml) means "defer to the global
    # DATA_RETENTION_DAYS policy" for this client's sessions. Any value > 0
    # is this client's OWN retention window in days, and takes priority over
    # the global policy -- it fires even when DATA_RETENTION_DAYS=0 (global
    # disabled does not disable a client's own window), and a client's own
    # window is never additionally capped by a shorter global one.
    bewaren_dagen: int | None = Field(default=None, ge=0)
    aflevering: str | None = None

    @field_validator("termen")
    @classmethod
    def _termen_length(cls, value: list[str]) -> list[str]:
        for term in value:
            if len(term) > _MAX_TERM_LENGTH:
                raise ValueError(
                    f"term {term!r} is {len(term)} tekens lang, maximum is {_MAX_TERM_LENGTH}"
                )
        return value

    @field_validator("aflevering")
    @classmethod
    def _aflevering_supported(cls, value: str | None) -> str | None:
        if value is not None and value != _SUPPORTED_AFLEVERING:
            raise ValueError(f"{value!r} niet ondersteund in deze versie")
        return value


def _format_validation_error(slug: str, exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        veld = ".".join(str(loc) for loc in error["loc"]) or "(root)"
        details.append(f"veld '{veld}': {error['msg']}")
    return f"klant.yaml voor '{slug}' is ongeldig -- " + "; ".join(details)


def resolve_klant_dir(slug: str, *, root: Path | None = None) -> Path:
    """Resolve ``slug`` to its client directory, refusing any path escape.

    Unlike ``context_docs.resolve_client_dir`` (which re-normalizes any input
    through ``slugify_client_name`` so a free-typed company name always maps
    to *some* safe folder), this is the stricter check for a slug that is
    expected to already name an existing, valid client folder -- e.g. one
    coming back from an API caller. A slug that resolves outside the root
    (``..`` segments, an absolute path, a symlink escape) is a hard error
    here, not a silent fallback to a default folder.
    """
    if not isinstance(slug, str) or not slug.strip():
        raise KlantConfigError("Klant-slug is verplicht en mag niet leeg zijn")
    base = (root or context_docs.UPLOAD_ROOT).resolve()
    candidate = (base / slug).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise KlantConfigError(
            f"Klant-slug {slug!r} resolveert buiten KLANTEN_ROOT"
        ) from exc
    return candidate


def load_klant_config(slug: str, *, root: Path | None = None) -> KlantConfig | None:
    """Load and validate ``<root>/<slug>/klant.yaml``.

    Returns ``None`` when the client directory has no ``klant.yaml`` -- the
    backward-compatible case that covers every client folder that existed
    before this plan, and is not itself an error. Raises
    ``KlantConfigError`` when the slug is unsafe, or the file exists but
    fails schema validation.
    """
    directory = resolve_klant_dir(slug, root=root)
    yaml_path = directory / KLANT_YAML_FILENAME
    if not yaml_path.is_file():
        return None

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise KlantConfigError(
            f"klant.yaml voor '{slug}' moet een mapping zijn, kreeg {type(raw).__name__}"
        )
    try:
        return KlantConfig.model_validate(raw)
    except ValidationError as exc:
        raise KlantConfigError(_format_validation_error(slug, exc)) from exc
