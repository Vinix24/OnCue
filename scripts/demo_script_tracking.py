#!/usr/bin/env python3
"""Demo: live script-dekkings-tracking op een sample-gesprek.

Laadt config/scripts/default.yaml, classificeert een aantal sample-utterances
met de lokale embedding-router, en toont welke scriptpunten geraakt zijn.
Geen externe LLM-aanroepen; dit draait lokaal.
"""

from __future__ import annotations

import asyncio

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.coaching import ScriptTracker

SAMPLE_UTTERANCES = [
    ("self", "Even kort voorstellen: ik ben de accountmanager voor dit team."),
    ("self", "Wat is het budget dat jullie hiervoor beschikbaar hebben?"),
    ("prospect", "We hebben een investeringsruimte van rond de twintigduizend euro."),
    ("self", "En wanneer moet dit opgeleverd zijn?"),
    ("prospect", "Graag voor het einde van het kwartaal."),
]


def _format_status(status: str) -> str:
    labels = {
        "missing": "❌ nog niet",
        "partial": "◐ gedeeltelijk",
        "tentative": "◇ mogelijk geraakt",
        "discussed": "◇ mogelijk geraakt",
        "confirmed": "✅ bevestigd",
    }
    return labels.get(status, status)


async def main() -> None:
    config = DetectorConfig(llm_provider="none")
    tracker = ScriptTracker(config)
    print(f"Loaded {len(tracker.points)} script points from {config.script_tracking_config}")
    print()

    timestamp_ms = 0
    for speaker, text in SAMPLE_UTTERANCES:
        timestamp_ms += 5000
        result = await tracker.process_utterance(text, speaker, timestamp_ms)
        marker = "✅" if result else "  "
        print(f"{marker} {speaker:8}: {text}")
        if result:
            print(f"   → script point: {result.title} ({_format_status(result.status)}, {result.confidence:.2f})")

    print()
    print("Coverage snapshot:")
    for item in tracker.build_full_snapshot():
        req = "verplicht" if item.required else "optioneel"
        print(f"  {_format_status(item.status)} [{req}] {item.title} ({item.phase})")


if __name__ == "__main__":
    asyncio.run(main())
