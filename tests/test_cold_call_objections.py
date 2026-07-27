from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core.config import _ALLOWED_PRESETS
from sales_copilot.core.preset import load_preset
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "objection_responses.yaml").exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)

# The acquisitie vertical preset is Pro content and is absent from the OSS
# export; tests that load it only run where the pack exists.
_requires_acquisitie_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "presets" / "acquisitie.yaml").exists(),
    reason="acquisitie vertical preset is Pro content, absent from the OSS export",
)

COLD_CALL_CATEGORIES = {
    "geen-interesse",
    "stuur-mailtje",
    "geen-tijd",
    "al-geregeld",
    "hoe-kom-je-aan-nummer",
    "geen-budget",
    "niet-de-juiste-persoon",
    "weer-een-verkoper",
}


@_requires_acquisitie_pack
def test_acquisitie_preset_registered_and_loads() -> None:
    assert "acquisitie" in _ALLOWED_PRESETS
    preset = load_preset("acquisitie")
    assert preset.name == "acquisitie"
    assert preset.ui_labels["self"] == "beller"
    names = {route["name"] for route in preset.objections["routes"]}
    assert names == COLD_CALL_CATEGORIES


@_requires_acquisitie_pack
def test_objection_router_builds_from_preset_routes() -> None:
    preset = load_preset("acquisitie")
    # Explicitly disable opportunities/negatives so this test isolates preset-route loading.
    router = ObjectionRouter(
        objections=preset.objections, include_opportunities=False, include_negatives=False
    )

    assert {route.name for route in router.routes} == COLD_CALL_CATEGORIES
    # every route needs example utterances for the embedding match to anchor on
    assert all(len(route.utterances) >= 3 for route in router.routes)


@_requires_curated_pack
def test_every_cold_call_objection_has_a_rebuttal() -> None:
    responses = ObjectionDetector._load_responses(  # noqa: SLF001
        "config/objection_responses.yaml", language="nl"
    )
    for category in COLD_CALL_CATEGORIES:
        assert responses.get(category), f"no rebuttal wired for cold-call objection '{category}'"
