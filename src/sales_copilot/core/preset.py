"""Vakgebied-preset loader.

Reads PRESET env var (default: 'sales'), loads
config/presets/<preset>.yaml, merges into runtime config.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from sales_copilot.core.paths import resolve_app_path

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_BASE_PRESET = "sales"

# Vertical vakgebied-starter-packs are Pro content and are absent from the OSS
# export (scripts/export_public.sh excludes them). Requesting one there must
# degrade fail-soft to the free base preset instead of crashing at call-start;
# the Pro distribution ships the files and they load normally when present.
_PRO_PRESET_NAMES = frozenset({"coach", "recruitment", "acquisitie"})

_REQUIRED_FIELDS = frozenset(
    {"pain_points", "objections", "buying_signals", "doubts", "ui_labels", "system_prompt_addendum"}
)


@dataclass(frozen=True)
class Preset:
    name: str
    pain_points: dict
    objections: dict
    buying_signals: dict
    doubts: dict
    ui_labels: dict
    system_prompt_addendum: str
    apply_pii_filter: bool = False

    def redact(self, text: str) -> tuple[str, int]:
        """Redact PII from text if apply_pii_filter is enabled.

        Returns (clean_text, hit_count). When disabled returns (text, 0).
        """
        if not self.apply_pii_filter:
            return text, 0
        from sales_copilot.core.pii_filter import redact_pii  # local import avoids circular dep

        return redact_pii(text)


def load_preset(name: str | None = None) -> Preset:
    """Load a vakgebied-preset by name.

    Falls back to PRESET env var, then to 'sales'.
    Raises FileNotFoundError if the preset YAML does not exist — except for the
    vertical Pro packs (coach/recruitment/acquisitie), which fail-soft to the
    free 'sales' base preset when absent (OSS export).
    Raises ValueError if required fields are missing.
    """
    resolved = (name or os.environ.get("PRESET") or "sales").strip().lower()
    path = resolve_app_path(f"config/presets/{resolved}.yaml")
    if not path.exists() and resolved in _PRO_PRESET_NAMES:
        # Fail-soft: the Pro pack is absent (OSS export). Run on the free base
        # preset; detection and UI keep working with the sales profile.
        logger.warning(
            "Preset '%s' not found (%s) — vertical preset packs are Pro content; "
            "falling back to '%s'.",
            resolved,
            path,
            _BASE_PRESET,
        )
        resolved = _BASE_PRESET
        path = resolve_app_path(f"config/presets/{resolved}.yaml")
    if not path.exists():
        raise FileNotFoundError(f"Preset '{resolved}' not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Preset file must be a YAML mapping: {path}")
    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(f"Preset '{resolved}' missing required fields: {sorted(missing)}")
    return Preset(
        name=resolved,
        pain_points=data["pain_points"],
        objections=data["objections"],
        buying_signals=data["buying_signals"],
        doubts=data["doubts"],
        ui_labels=data["ui_labels"],
        system_prompt_addendum=data.get("system_prompt_addendum", ""),
        apply_pii_filter=(resolved == "recruitment"),
    )
