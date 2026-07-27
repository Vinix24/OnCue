"""Tests for the vakgebied-preset loader (sales_copilot.core.preset)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sales_copilot.core.preset import Preset, load_preset

# The vertical Pro preset packs (coach/recruitment/acquisitie) are Pro content
# and are absent from the OSS export; tests that assert on their content only
# run where the pack exists. Loader LOGIC is tested below against test-only
# fixture presets (tests/fixtures/presets/), independent of the real packs.
_PRESETS_DIR = Path(__file__).resolve().parent.parent / "config" / "presets"
_FIXTURE_PRESETS_DIR = Path(__file__).resolve().parent / "fixtures" / "presets"

_requires_coach_pack = pytest.mark.skipif(
    not (_PRESETS_DIR / "coach.yaml").exists(),
    reason="coach vertical preset is Pro content, absent from the OSS export",
)
_requires_recruitment_pack = pytest.mark.skipif(
    not (_PRESETS_DIR / "recruitment.yaml").exists(),
    reason="recruitment vertical preset is Pro content, absent from the OSS export",
)


def _available_presets() -> list[str]:
    """Presets whose YAML is present in this tree (sales always; Pro packs only in the Pro distribution)."""
    return [name for name in ("sales", "coach", "recruitment") if (_PRESETS_DIR / f"{name}.yaml").exists()]


def test_load_sales_preset_default() -> None:
    """load_preset() without arguments returns the sales preset."""
    preset = load_preset()
    assert preset.name == "sales"


def test_load_sales_preset_explicit() -> None:
    preset = load_preset("sales")
    assert isinstance(preset, Preset)
    assert preset.name == "sales"


@_requires_coach_pack
def test_load_coach_preset() -> None:
    preset = load_preset("coach")
    assert preset.name == "coach"
    assert isinstance(preset.pain_points, dict)
    assert isinstance(preset.ui_labels, dict)


@_requires_recruitment_pack
def test_load_recruitment_preset() -> None:
    preset = load_preset("recruitment")
    assert preset.name == "recruitment"
    assert isinstance(preset.pain_points, dict)
    assert isinstance(preset.ui_labels, dict)


def test_all_presets_have_required_fields() -> None:
    for name in _available_presets():
        preset = load_preset(name)
        assert preset.pain_points, f"{name}: pain_points must not be empty"
        assert preset.objections, f"{name}: objections must not be empty"
        assert preset.buying_signals, f"{name}: buying_signals must not be empty"
        assert preset.doubts, f"{name}: doubts must not be empty"
        assert preset.ui_labels, f"{name}: ui_labels must not be empty"
        assert isinstance(preset.system_prompt_addendum, str), f"{name}: system_prompt_addendum must be str"


def test_all_presets_have_pain_point_routes() -> None:
    for name in _available_presets():
        preset = load_preset(name)
        routes = preset.pain_points.get("routes", [])
        assert len(routes) >= 3, f"{name}: at least 3 pain_point routes expected"
        for route in routes:
            assert "name" in route, f"{name}: route missing 'name'"
            assert "utterances" in route, f"{name}: route missing 'utterances'"
            assert len(route["utterances"]) >= 3, f"{name}: route '{route['name']}' needs >= 3 utterances"


def test_all_presets_have_ui_labels_for_prospect_and_self() -> None:
    for name in _available_presets():
        preset = load_preset(name)
        assert "prospect" in preset.ui_labels, f"{name}: ui_labels must contain 'prospect' key"
        assert "self" in preset.ui_labels, f"{name}: ui_labels must contain 'self' key"


def test_sales_preset_ui_labels() -> None:
    preset = load_preset("sales")
    assert preset.ui_labels["prospect"] == "prospect"
    assert preset.ui_labels["self"] == "verkoper"


@_requires_coach_pack
def test_coach_preset_ui_labels() -> None:
    preset = load_preset("coach")
    assert preset.ui_labels["prospect"] == "klant"
    assert preset.ui_labels["self"] == "coach"


@_requires_recruitment_pack
def test_recruitment_preset_ui_labels() -> None:
    preset = load_preset("recruitment")
    assert preset.ui_labels["prospect"] == "kandidaat"
    assert preset.ui_labels["self"] == "consultant"


@_requires_recruitment_pack
def test_recruitment_preset_has_system_prompt_addendum() -> None:
    preset = load_preset("recruitment")
    assert preset.system_prompt_addendum.strip(), "recruitment preset must have a non-empty system_prompt_addendum"
    assert "kandidaat" in preset.system_prompt_addendum


@_requires_recruitment_pack
def test_fallback_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """PRESET env var is used when no explicit name is given."""
    monkeypatch.setenv("PRESET", "recruitment")
    preset = load_preset()
    assert preset.name == "recruitment"


@_requires_recruitment_pack
def test_env_var_overridden_by_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRESET", "recruitment")
    preset = load_preset("coach")
    assert preset.name == "coach"


def test_missing_preset_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        load_preset("nonexistent")


def test_preset_is_frozen() -> None:
    preset = load_preset("sales")
    with pytest.raises(Exception):
        preset.name = "other"  # type: ignore[misc]


def test_preset_name_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without PRESET env var, load_preset() defaults to 'sales'."""
    monkeypatch.delenv("PRESET", raising=False)
    preset = load_preset()
    assert preset.name == "sales"


@_requires_recruitment_pack
def test_recruitment_preset_apply_pii_filter_true() -> None:
    preset = load_preset("recruitment")
    assert preset.apply_pii_filter is True


def test_sales_preset_apply_pii_filter_false() -> None:
    preset = load_preset("sales")
    assert preset.apply_pii_filter is False


@_requires_coach_pack
def test_coach_preset_apply_pii_filter_false() -> None:
    preset = load_preset("coach")
    assert preset.apply_pii_filter is False


@_requires_recruitment_pack
def test_recruitment_preset_redact_bsn() -> None:
    """Preset.redact() replaces BSN when apply_pii_filter is True."""
    preset = load_preset("recruitment")
    clean, hits = preset.redact("BSN 123456789")
    assert clean == "BSN [BSN]"
    assert hits == 1


def test_sales_preset_redact_is_passthrough() -> None:
    """Preset.redact() returns original text unchanged when apply_pii_filter is False."""
    preset = load_preset("sales")
    text = "BSN 123456789"
    clean, hits = preset.redact(text)
    assert clean == text
    assert hits == 0


@_requires_recruitment_pack
def test_recruitment_preset_has_buying_signal_routes() -> None:
    preset = load_preset("recruitment")
    routes = preset.buying_signals.get("routes", [])
    assert len(routes) >= 2
    for route in routes:
        assert "name" in route
        assert "utterances" in route


@_requires_coach_pack
def test_coach_preset_has_8_pain_point_routes() -> None:
    preset = load_preset("coach")
    routes = preset.pain_points.get("routes", [])
    assert len(routes) == 8, f"coach preset expected 8 pain_point routes, got {len(routes)}"


@_requires_coach_pack
def test_coach_preset_has_new_pain_points() -> None:
    preset = load_preset("coach")
    route_names = [r["name"] for r in preset.pain_points.get("routes", [])]
    assert "financiele-druk" in route_names
    assert "relationele-stress" in route_names
    assert "gezondheid-balans" in route_names


@_requires_coach_pack
def test_coach_preset_pain_points_have_6_utterances() -> None:
    preset = load_preset("coach")
    for route in preset.pain_points.get("routes", []):
        assert len(route["utterances"]) == 6, (
            f"coach route '{route['name']}' expected 6 utterances, got {len(route['utterances'])}"
        )


@_requires_coach_pack
def test_coach_preset_has_buying_signal_routes() -> None:
    preset = load_preset("coach")
    routes = preset.buying_signals.get("routes", [])
    assert len(routes) == 5, f"coach preset expected 5 buying_signal routes, got {len(routes)}"
    route_names = [r["name"] for r in routes]
    assert "vraag-naar-implementatie" in route_names
    assert "vraag-naar-traject" in route_names
    assert "vraag-naar-tarieven" in route_names
    assert "vraag-naar-referenties" in route_names
    assert "vraag-naar-aanpak" in route_names


@_requires_coach_pack
def test_coach_preset_has_partner_bezwaar_objection() -> None:
    preset = load_preset("coach")
    route_names = [r["name"] for r in preset.objections.get("routes", [])]
    assert "partner-bezwaar" in route_names


@_requires_coach_pack
def test_coach_preset_system_prompt_addendum_contains_core_principle() -> None:
    preset = load_preset("coach")
    addendum = preset.system_prompt_addendum
    assert "CORE PRINCIPE" in addendum
    assert "coaching-gesprek" in addendum


# ---------------------------------------------------------------------------
# Loader LOGIC against test-only fixture presets (tests/fixtures/presets/).
# These run everywhere — also in the OSS export, where the vertical Pro packs
# are absent — because they never touch the real coach/recruitment/acquisitie
# content.
# ---------------------------------------------------------------------------


@pytest.fixture
def _fixture_preset_tree(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the loader at the test-only fixture presets (sales + recruitment)."""
    monkeypatch.setattr(
        "sales_copilot.core.preset.resolve_app_path",
        lambda rel: _FIXTURE_PRESETS_DIR / Path(rel).name,
    )
    return _FIXTURE_PRESETS_DIR


@pytest.mark.parametrize("pro_name", ["coach", "recruitment", "acquisitie"])
def test_absent_pro_pack_falls_back_to_sales(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pro_name: str
) -> None:
    """A requested Pro pack that is absent (OSS export) fail-softs to the free base preset."""
    # Tree with ONLY the base preset: every Pro pack is absent by construction.
    (tmp_path / "sales.yaml").write_text(
        (_FIXTURE_PRESETS_DIR / "sales.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(
        "sales_copilot.core.preset.resolve_app_path",
        lambda rel: tmp_path / Path(rel).name,
    )
    with caplog.at_level(logging.WARNING):
        preset = load_preset(pro_name)
    assert preset.name == "sales"
    assert preset.ui_labels["prospect"] == "fixture-prospect"
    assert f"Preset '{pro_name}' not found" in caplog.text
    assert "falling back to 'sales'" in caplog.text


def test_fixture_tree_loads_fixture_sales(_fixture_preset_tree: Path) -> None:
    preset = load_preset("sales")
    assert preset.name == "sales"
    assert preset.ui_labels["self"] == "fixture-self"
    assert [r["name"] for r in preset.pain_points["routes"]] == ["fixture-pijn"]


def test_unknown_preset_still_raises_with_fixture_tree(_fixture_preset_tree: Path) -> None:
    """Fail-soft applies only to the known Pro packs; typos still fail fast."""
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        load_preset("nonexistent")


def test_preset_missing_required_fields_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "broken.yaml").write_text("pain_points: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "sales_copilot.core.preset.resolve_app_path",
        lambda rel: tmp_path / Path(rel).name,
    )
    with pytest.raises(ValueError, match="missing required fields"):
        load_preset("broken")


def test_preset_non_mapping_yaml_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "alist.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    monkeypatch.setattr(
        "sales_copilot.core.preset.resolve_app_path",
        lambda rel: tmp_path / Path(rel).name,
    )
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_preset("alist")
