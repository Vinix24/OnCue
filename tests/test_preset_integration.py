"""Integration tests: vakgebied-presets wired with DetectorConfig and WindowClassifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.preset import load_preset

# The vertical Pro preset packs (coach/recruitment/acquisitie) are Pro content
# and are absent from the OSS export; tests that assert on their content only
# run where the pack exists.
_PRESETS_DIR = Path(__file__).resolve().parent.parent / "config" / "presets"

_requires_coach_pack = pytest.mark.skipif(
    not (_PRESETS_DIR / "coach.yaml").exists(),
    reason="coach vertical preset is Pro content, absent from the OSS export",
)
_requires_recruitment_pack = pytest.mark.skipif(
    not (_PRESETS_DIR / "recruitment.yaml").exists(),
    reason="recruitment vertical preset is Pro content, absent from the OSS export",
)

try:
    from sales_copilot.modules.detector.window_classifier import WindowClassifier

    _HAS_WINDOW_CLASSIFIER = True
except ImportError:
    _HAS_WINDOW_CLASSIFIER = False
    WindowClassifier = None  # type: ignore[assignment, misc]

_skip_no_wc = pytest.mark.skipif(
    not _HAS_WINDOW_CLASSIFIER,
    reason="instructor package not installed — WindowClassifier tests skipped",
)


class TestDetectorConfigPresetIntegration:
    def test_default_preset_name_is_sales(self) -> None:
        config = DetectorConfig()
        assert config.preset_name == "sales"

    def test_from_env_reads_preset_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRESET", "recruitment")
        config = DetectorConfig.from_env()
        assert config.preset_name == "recruitment"

    def test_from_env_defaults_to_sales_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PRESET", raising=False)
        config = DetectorConfig.from_env()
        assert config.preset_name == "sales"

    def test_from_env_coach_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRESET", "coach")
        config = DetectorConfig.from_env()
        assert config.preset_name == "coach"

    def test_preset_name_is_lowercase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRESET", "RECRUITMENT")
        config = DetectorConfig.from_env()
        assert config.preset_name == "recruitment"


class TestPresetUiLabels:
    def test_sales_prospect_label(self) -> None:
        preset = load_preset("sales")
        assert preset.ui_labels.get("prospect") == "prospect"

    @_requires_recruitment_pack
    def test_recruitment_prospect_label_is_kandidaat(self) -> None:
        preset = load_preset("recruitment")
        assert preset.ui_labels.get("prospect") == "kandidaat"

    @_requires_recruitment_pack
    def test_recruitment_self_label_is_consultant(self) -> None:
        preset = load_preset("recruitment")
        assert preset.ui_labels.get("self") == "consultant"

    @_requires_coach_pack
    def test_coach_prospect_label_is_klant(self) -> None:
        preset = load_preset("coach")
        assert preset.ui_labels.get("prospect") == "klant"

    @_requires_recruitment_pack
    def test_ui_labels_mapping_default(self) -> None:
        preset = load_preset("recruitment")
        mapped = preset.ui_labels.get("prospect", "prospect")
        assert mapped == "kandidaat"

    @_requires_recruitment_pack
    def test_unknown_speaker_key_returns_default(self) -> None:
        preset = load_preset("recruitment")
        mapped = preset.ui_labels.get("unknown_speaker", "unknown_speaker")
        assert mapped == "unknown_speaker"


@_skip_no_wc
class TestWindowClassifierSalesPreset:
    def _make(self, monkeypatch: pytest.MonkeyPatch, preset_name: str = "sales") -> WindowClassifier:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = DetectorConfig(
            llm_provider="openai",
            llm_model="test-model",
            preset_name=preset_name,
        )
        preset = load_preset(preset_name)
        return WindowClassifier(config, "openai", preset=preset)  # type: ignore[operator]

    def test_sales_pain_categories_in_system_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch, "sales")
        assert "offerteproces" in clf.system_prompt
        assert "capaciteit" in clf.system_prompt

    def test_sales_ui_label_prospect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch, "sales")
        assert clf.preset.ui_labels["prospect"] == "prospect"

    def test_sales_no_addendum_in_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch, "sales")
        assert clf.preset.system_prompt_addendum == ""


@_skip_no_wc
@_requires_recruitment_pack
class TestWindowClassifierRecruitmentPreset:
    def _make(self, monkeypatch: pytest.MonkeyPatch) -> WindowClassifier:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = DetectorConfig(
            llm_provider="openai",
            llm_model="test-model",
            preset_name="recruitment",
        )
        preset = load_preset("recruitment")
        return WindowClassifier(config, "openai", preset=preset)  # type: ignore[operator]

    def test_recruitment_pain_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "functie_fit" in clf._pain_categories
        assert "salarispens" in clf._pain_categories
        assert "doorstroom" in clf._pain_categories

    def test_recruitment_ui_label_is_kandidaat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert clf.preset.ui_labels["prospect"] == "kandidaat"

    def test_system_prompt_contains_recruitment_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "functie_fit" in clf.system_prompt
        assert "salarispens" in clf.system_prompt

    def test_system_prompt_contains_addendum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "kandidaat" in clf.system_prompt

    def test_system_prompt_excludes_sales_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "offerteproces" not in clf.system_prompt
        assert "capaciteit" not in clf.system_prompt

    def test_buying_signals_from_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "interesse-in-startdatum" in clf.system_prompt

    def test_doubt_subcategories_from_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "twijfel-over-werkgever" in clf.system_prompt


@_skip_no_wc
@_requires_coach_pack
class TestWindowClassifierCoachPreset:
    def _make(self, monkeypatch: pytest.MonkeyPatch) -> WindowClassifier:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = DetectorConfig(
            llm_provider="openai",
            llm_model="test-model",
            preset_name="coach",
        )
        preset = load_preset("coach")
        return WindowClassifier(config, "openai", preset=preset)  # type: ignore[operator]

    def test_coach_pain_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "planning" in clf._pain_categories
        assert "motivatie" in clf._pain_categories
        assert "accountability" in clf._pain_categories

    def test_coach_ui_label_is_klant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert clf.preset.ui_labels["prospect"] == "klant"

    def test_system_prompt_contains_addendum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "coach" in clf.system_prompt.lower()

    def test_coach_system_prompt_addendum_core_principle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "CORE PRINCIPE" in clf.system_prompt

    def test_coach_new_pain_categories_in_system_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "financiele-druk" in clf.system_prompt
        assert "relationele-stress" in clf.system_prompt
        assert "gezondheid-balans" in clf.system_prompt

    def test_coach_partner_bezwaar_in_objection_categories(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "partner-bezwaar" in clf._objection_categories
        assert "partner-bezwaar" in clf.system_prompt

    def test_coach_buying_signal_routes_in_system_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clf = self._make(monkeypatch)
        assert "vraag-naar-implementatie" in clf.system_prompt
        assert "vraag-naar-tarieven" in clf.system_prompt


@_skip_no_wc
class TestWindowClassifierBackwardCompat:
    def test_default_preset_is_sales(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PRESET", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = DetectorConfig(llm_provider="openai", llm_model="test-model")
        clf = WindowClassifier(config, "openai")  # type: ignore[operator]
        assert clf.preset.name == "sales"

    @_requires_recruitment_pack
    def test_explicit_pain_categories_override_preset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = DetectorConfig(
            llm_provider="openai",
            llm_model="test-model",
            preset_name="recruitment",
        )
        preset = load_preset("recruitment")
        clf = WindowClassifier(config, "openai", pain_categories=["custom_cat"], preset=preset)  # type: ignore[operator]
        assert clf._pain_categories == ["custom_cat"]
        assert "custom_cat" in clf.system_prompt
