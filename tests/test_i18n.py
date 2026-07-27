"""Tests for the i18n message-catalog loader (sales_copilot.core.i18n).

Loader/fallback/interpolation behaviour is exercised against small seeded
catalogs written into a tmp directory, so results are deterministic and never
depend on the wording of the shipped catalogs. A separate group verifies that
the ``LANGUAGE`` env var selects the real committed ``nl`` catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core import i18n

_NL_SEED = """\
greeting: "Hallo {name}"
only_nl: "alleen in het Nederlands"
coaching:
  empty_state: "NL empty"
"""

_EN_SEED = """\
greeting: "Hello {name}"
only_en: "only in English"
coaching:
  empty_state: "EN empty"
"""


@pytest.fixture(autouse=True)
def _reset_i18n() -> None:
    """Isolate global i18n state around every test."""
    i18n.clear_cache()
    yield
    i18n.set_catalog_dir(None)
    i18n.clear_cache()


@pytest.fixture()
def seeded_catalogs(tmp_path: Path) -> Path:
    (tmp_path / "nl.yaml").write_text(_NL_SEED, encoding="utf-8")
    (tmp_path / "en.yaml").write_text(_EN_SEED, encoding="utf-8")
    i18n.set_catalog_dir(tmp_path)
    return tmp_path


# --- resolution -------------------------------------------------------------


def test_i18n_resolves_dotted_key_for_language(seeded_catalogs: Path) -> None:
    assert i18n.t("coaching.empty_state", lang="en") == "EN empty"
    assert i18n.t("coaching.empty_state", lang="nl") == "NL empty"


def test_i18n_falls_back_to_default_when_language_catalog_missing(seeded_catalogs: Path) -> None:
    # "xx" has no catalog file -> resolves against the nl default.
    assert i18n.t("coaching.empty_state", lang="xx") == "NL empty"


def test_i18n_falls_back_to_default_when_key_missing_in_nondefault_language(
    seeded_catalogs: Path,
) -> None:
    # "only_nl" exists in nl (the default) but not in en -> requesting en
    # falls back to the nl default, proving the en toggle degrades gracefully.
    assert i18n.t("only_nl", lang="en") == "alleen in het Nederlands"


def test_i18n_missing_key_in_default_language_returns_raw_key(
    seeded_catalogs: Path,
) -> None:
    # "only_en" exists in en but not in nl (the default) -> requesting nl
    # returns the raw key since nl is the anchor and has no further fallback.
    assert i18n.t("only_en", lang="nl") == "only_en"


def test_i18n_missing_key_returns_key_itself(seeded_catalogs: Path) -> None:
    assert i18n.t("does.not.exist", lang="en") == "does.not.exist"


# --- interpolation ----------------------------------------------------------


def test_i18n_interpolates_keyword_arguments(seeded_catalogs: Path) -> None:
    assert i18n.t("greeting", lang="en", name="Vincent") == "Hello Vincent"
    assert i18n.t("greeting", lang="nl", name="Steffie") == "Hallo Steffie"


def test_i18n_leaves_placeholder_intact_without_kwargs(seeded_catalogs: Path) -> None:
    assert i18n.t("greeting", lang="en") == "Hello {name}"


def test_i18n_degrades_to_raw_string_on_bad_interpolation(seeded_catalogs: Path) -> None:
    # Missing placeholder value must not raise -- the raw template is returned.
    assert i18n.t("greeting", lang="en", wrong="x") == "Hello {name}"


# --- caching ----------------------------------------------------------------


def test_i18n_caches_loaded_catalog(seeded_catalogs: Path) -> None:
    first = i18n.load_catalog("nl")
    assert i18n.load_catalog("nl") is first

    (seeded_catalogs / "nl.yaml").write_text('greeting: "gewijzigd"\n', encoding="utf-8")
    # Still the cached copy until the cache is cleared.
    assert i18n.t("only_nl", lang="nl") == "alleen in het Nederlands"

    i18n.clear_cache()
    assert i18n.t("greeting", lang="nl") == "gewijzigd"


def test_i18n_missing_catalog_caches_empty_mapping(seeded_catalogs: Path) -> None:
    assert i18n.load_catalog("xx") == {}
    assert i18n.load_catalog("xx") is i18n.load_catalog("xx")


# --- configured language ----------------------------------------------------


def test_configured_language_defaults_to_nl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(i18n.LANGUAGE_ENV_VAR, raising=False)
    assert i18n.configured_language() == "nl"


def test_configured_language_reads_and_normalizes_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "  EN ")
    assert i18n.configured_language() == "en"


def test_language_env_selects_language_catalog(
    monkeypatch: pytest.MonkeyPatch, seeded_catalogs: Path
) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "en")
    assert i18n.t("coaching.empty_state") == "EN empty"

    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "nl")
    assert i18n.t("coaching.empty_state") == "NL empty"


# --- real committed catalogs ------------------------------------------------


def test_language_en_selects_real_en_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the shipped catalogs, LANGUAGE=en resolves the real en strings."""
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "en")
    assert i18n.t("coaching.suggestion_empty_state") == "Waiting for conversation context..."


def test_language_default_selects_real_nl_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(i18n.LANGUAGE_ENV_VAR, raising=False)
    assert i18n.t("coaching.suggestion_empty_state") == "Wacht op gesprekscontext..."


def test_language_nl_selects_real_nl_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """nl stays a first-class toggle: LANGUAGE=nl must still render full Dutch."""
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "nl")
    assert i18n.t("coaching.suggestion_empty_state") == "Wacht op gesprekscontext..."
