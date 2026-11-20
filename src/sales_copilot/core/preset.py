"""Vakgebied-preset loader.

Reads PRESET env var (default: 'sales'), loads
config/presets/<preset>.yaml, merges into runtime config.

Presets resolve per call language. For language ``xx`` the loader reads
``config/presets/<name>_xx.yaml`` when it exists, else the base file
``config/presets/<name>.yaml``. The base files (sales, acquisitie, coach,
recruitment) are written in **Dutch**: their inline routes (``pain_points``,
``objections``) and ``ui_labels`` are Dutch text. When a base file is used for a
language other than Dutch, those inline routes and labels are not applied; the
language route files (``config/pain_points_<xx>.yaml``,
``config/objections_<xx>.yaml``) supply the routes and ``ui_labels`` stays empty
so the i18n catalog / neutral speaker labels apply. Settings that are not text
in a language (``default_consent_tier``, ``system_prompt_addendum``, the
``buying_signals`` / ``doubts`` category identifiers) are kept from the preset.
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

# The language the base preset files (config/presets/<name>.yaml) are written in.
BASE_PRESET_LANGUAGE = "nl"

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


def _language_route_mapping(config_name: str, language: str) -> dict | None:
    """Routes from ``config/<config_name>_<language>.yaml``, or ``None`` when that file is absent.

    A file that exists but is unreadable or not a mapping raises: it is a broken install, not a
    missing translation.
    """
    path = resolve_app_path(f"config/{config_name}_{language}.yaml")
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Route file must be a YAML mapping: {path}")
    return data


def load_preset(name: str | None = None, language: str | None = None) -> Preset:
    """Load a vakgebied-preset by name for a call language.

    Falls back to PRESET env var, then to 'sales'. ``language`` defaults to the configured
    call language (``CALL_LANGUAGE``).
    Resolution: ``<name>_<language>.yaml`` when it exists, else the Dutch base file. A base file
    used for a non-Dutch language does not apply its inline routes and labels (see module
    docstring); the language route files supply the routes instead.
    Raises FileNotFoundError if the preset YAML does not exist — except for the
    vertical Pro packs (coach/recruitment/acquisitie), which fail-soft to the
    free 'sales' base preset when absent (OSS export).
    Raises ValueError if required fields are missing.
    """
    if language is None:
        from sales_copilot.core.config import configured_call_language  # local import avoids circular dep

        language = configured_call_language()
    lang = language.strip().lower() or BASE_PRESET_LANGUAGE
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
    localized = resolve_app_path(f"config/presets/{resolved}_{lang}.yaml")
    use_localized = lang != BASE_PRESET_LANGUAGE and localized.exists()
    if use_localized:
        path = localized
    if not path.exists():
        raise FileNotFoundError(f"Preset '{resolved}' not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Preset file must be a YAML mapping: {path}")
    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(f"Preset '{resolved}' missing required fields: {sorted(missing)}")
    pain_points = data["pain_points"]
    objections = data["objections"]
    ui_labels = data["ui_labels"]
    if lang != BASE_PRESET_LANGUAGE and not use_localized:
        # The base file is Dutch: do not apply its Dutch routes and labels on this call.
        localized_pain = _language_route_mapping("pain_points", lang)
        localized_obj = _language_route_mapping("objections", lang)
        if localized_pain is None or localized_obj is None:
            logger.warning(
                "No '%s' route files for preset '%s'; the Dutch base routes stay in use.",
                lang,
                resolved,
            )
        else:
            pain_points = localized_pain
            objections = localized_obj
            ui_labels = {}
    return Preset(
        name=resolved,
        pain_points=pain_points,
        objections=objections,
        buying_signals=data["buying_signals"],
        doubts=data["doubts"],
        ui_labels=ui_labels,
        system_prompt_addendum=data.get("system_prompt_addendum", ""),
        apply_pii_filter=(resolved == "recruitment"),
    )
