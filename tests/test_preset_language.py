"""Presets resolve per call language: <name>_<lang>.yaml, else the Dutch base without its Dutch text."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sales_copilot.core import consent
from sales_copilot.core import preset as preset_module
from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.preset import load_preset
from sales_copilot.modules.detector.objection_detector import ObjectionRouter

_ROOT = Path(__file__).resolve().parent.parent
_DUTCH_PRICE = "het is te duur voor ons"


def _route_names(section: dict) -> list[str]:
    return [route["name"] for route in section["routes"]]


def _all_utterances(section: dict) -> list[str]:
    return [u for route in section["routes"] for u in route["utterances"]]


@pytest.fixture
def plain_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config tree with a Dutch-only preset `plain` and the real language route files."""
    config = tmp_path / "config"
    (config / "presets").mkdir(parents=True)
    for name in ("pain_points_en.yaml", "objections_en.yaml"):
        shutil.copy(_ROOT / "config" / name, config / name)
    base = yaml.safe_load((_ROOT / "config" / "presets" / "sales.yaml").read_text(encoding="utf-8"))
    base["default_consent_tier"] = "soft"
    (config / "presets" / "plain.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    monkeypatch.setattr(preset_module, "resolve_app_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(consent, "resolve_app_path", lambda rel: tmp_path / rel)
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    return tmp_path


def test_english_call_uses_english_sales_preset() -> None:
    preset = load_preset("sales", "en")
    assert "price" in _route_names(preset.objections)
    assert "prijs" not in _route_names(preset.objections)
    assert preset.ui_labels["self"] == "salesperson"
    assert _DUTCH_PRICE not in _all_utterances(preset.objections)


def test_english_price_objection_gets_english_label() -> None:
    preset = load_preset("sales", "en")
    router = ObjectionRouter(DetectorConfig.from_env(), "en", objections=preset.objections)
    names = [route.name for route in router.routes]
    assert "price" in names
    assert "prijs" not in names


def test_dutch_call_keeps_dutch_sales_preset() -> None:
    preset = load_preset("sales", "nl")
    assert "prijs" in _route_names(preset.objections)
    assert preset.ui_labels["self"] == "verkoper"
    assert _DUTCH_PRICE in _all_utterances(preset.objections)


def test_english_and_dutch_sales_presets_have_the_same_shape() -> None:
    en = load_preset("sales", "en")
    nl = load_preset("sales", "nl")
    assert len(en.pain_points["routes"]) == len(nl.pain_points["routes"])
    assert len(en.objections["routes"]) == len(nl.objections["routes"])
    assert en.buying_signals == nl.buying_signals
    assert en.doubts == nl.doubts


def test_english_call_without_en_preset_uses_language_route_files(plain_config: Path) -> None:
    preset = load_preset("plain", "en")
    en_objections = yaml.safe_load((_ROOT / "config" / "objections_en.yaml").read_text(encoding="utf-8"))
    en_pain = yaml.safe_load((_ROOT / "config" / "pain_points_en.yaml").read_text(encoding="utf-8"))
    assert preset.objections == en_objections
    assert preset.pain_points == en_pain
    assert preset.ui_labels == {}
    assert _DUTCH_PRICE not in _all_utterances(preset.objections)
    router = ObjectionRouter(DetectorConfig.from_env(), "en", objections=preset.objections)
    assert "prijs" not in [route.name for route in router.routes]


def test_dutch_call_without_en_preset_keeps_dutch_base(plain_config: Path) -> None:
    preset = load_preset("plain", "nl")
    assert "prijs" in _route_names(preset.objections)
    assert preset.ui_labels["self"] == "verkoper"


def test_language_without_route_files_keeps_dutch_base_and_warns(
    plain_config: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        preset = load_preset("plain", "fr")
    assert "prijs" in _route_names(preset.objections)
    assert "No 'fr' route files" in caplog.text


def test_broken_language_route_file_raises(plain_config: Path) -> None:
    (plain_config / "config" / "objections_en.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_preset("plain", "en")


def test_language_defaults_to_call_language_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "en")
    assert "price" in _route_names(load_preset("sales").objections)
    monkeypatch.setenv("CALL_LANGUAGE", "nl")
    assert "prijs" in _route_names(load_preset("sales").objections)


def test_default_consent_tier_is_the_same_for_both_languages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    en = yaml.safe_load((_ROOT / "config" / "presets" / "sales_en.yaml").read_text(encoding="utf-8"))
    nl = yaml.safe_load((_ROOT / "config" / "presets" / "sales.yaml").read_text(encoding="utf-8"))
    assert en["default_consent_tier"] == nl["default_consent_tier"]
    assert consent.consent_tier("sales") == nl["default_consent_tier"]


def test_non_language_settings_survive_the_fallback(plain_config: Path) -> None:
    nl = load_preset("plain", "nl")
    en = load_preset("plain", "en")
    assert en.system_prompt_addendum == nl.system_prompt_addendum
    assert en.buying_signals == nl.buying_signals
    assert en.doubts == nl.doubts
    assert consent.consent_tier("plain") == "soft"
