"""Lightweight i18n message-catalog resolution.

User-facing strings live in per-language YAML catalogs under ``config/i18n``
(``nl.yaml``, ``en.yaml``, ...). ``t()`` resolves a dotted message key against
the configured language and falls back to the default language (``nl``) when a
language catalog or an individual key is missing, so a partial translation can
never crash a call. ``en`` remains a fully-supported first-class toggle via
the ``LANGUAGE`` env var -- only the default/fallback direction is Dutch.

The configured language is read from the ``LANGUAGE`` environment variable
(see ``I18nConfig`` in ``sales_copilot.core.config`` for the documented config
surface). This module is deliberately self-contained -- it depends only on
``paths`` and ``yaml``, never on ``config``, so it can be imported from anywhere
in the config/routing graph without an import cycle.

Loaded catalogs are cached per language. Tests can point the loader at a
throwaway directory with ``set_catalog_dir()`` and reset shared state with
``clear_cache()``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

#: Environment variable holding the configured product/UI language.
LANGUAGE_ENV_VAR = "LANGUAGE"
#: Default and fallback language. Every other catalog resolves against this one.
DEFAULT_LANGUAGE = "nl"
#: Catalog directory, relative to the app-support base (repo root in dev).
CATALOG_DIRNAME = "config/i18n"

_CACHE: dict[str, dict[str, object]] = {}
_CATALOG_DIR_OVERRIDE: Path | None = None


def set_catalog_dir(path: str | Path | None) -> None:
    """Override the catalog directory and clear the cache.

    Primarily for tests: point the loader at a directory of seeded catalog
    files. Pass ``None`` to restore the default ``config/i18n`` location.
    """
    global _CATALOG_DIR_OVERRIDE
    _CATALOG_DIR_OVERRIDE = Path(path) if path is not None else None
    clear_cache()


def clear_cache() -> None:
    """Drop all cached catalogs. Call after mutating catalog files in tests."""
    _CACHE.clear()


def _catalog_dir() -> Path:
    if _CATALOG_DIR_OVERRIDE is not None:
        return _CATALOG_DIR_OVERRIDE
    return resolve_app_path(CATALOG_DIRNAME)


def _normalize(language: str | None) -> str:
    if not language:
        return DEFAULT_LANGUAGE
    normalized = language.strip().lower()
    return normalized or DEFAULT_LANGUAGE


def configured_language() -> str:
    """Return the configured language from the ``LANGUAGE`` env var (default ``en``)."""
    return _normalize(os.getenv(LANGUAGE_ENV_VAR, DEFAULT_LANGUAGE))


def load_catalog(language: str) -> dict[str, object]:
    """Load and cache the catalog for ``language``.

    A missing or malformed catalog resolves to an empty mapping (cached) so the
    caller degrades to the fallback language instead of raising.
    """
    lang = _normalize(language)
    cached = _CACHE.get(lang)
    if cached is not None:
        return cached

    path = _catalog_dir() / f"{lang}.yaml"
    data: dict[str, object] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("i18n: could not load catalog %s (%s); using fallback", path, exc)
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
        elif loaded is not None:
            logger.warning("i18n: catalog %s is not a mapping; using fallback", path)

    _CACHE[lang] = data
    return data


def _resolve(key: str, language: str) -> str | None:
    node: object = load_catalog(language)
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def t(key: str, *, lang: str | None = None, **kwargs: object) -> str:
    """Resolve ``key`` to a string for the configured (or given) language.

    Resolution order: the requested language, then the default language
    (``en``), then the key itself as a last-resort visible fallback. When
    keyword arguments are supplied they are applied with ``str.format`` for
    simple ``{name}`` interpolation; a malformed template degrades to the raw
    string rather than raising.
    """
    language = _normalize(lang if lang is not None else configured_language())

    value = _resolve(key, language)
    if value is None and language != DEFAULT_LANGUAGE:
        value = _resolve(key, DEFAULT_LANGUAGE)
    if value is None:
        logger.warning("i18n: missing message key %r (lang=%s)", key, language)
        return key

    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("i18n: interpolation failed for key %r (%s)", key, exc)
    return value
