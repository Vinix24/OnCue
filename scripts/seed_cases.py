"""Seed the local SQLite database with example case studies."""

import asyncio
from pathlib import Path

from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB

DB_PATH = Path(__file__).parent.parent / "data" / "cases.db"

EXAMPLE_CASES = [
    {
        "id": "case-001",
        "title": "Manufacturing Co — 70% snellere offertes",
        "industry": "manufacturing",
        "pain_point": "offerteproces",
        "description": (
            "Van 4 uur naar 45 minuten per offerte door AI-gestuurde templates "
            "en automatische productconfiguratie."
        ),
        "slide_html": """<section id="case-001" class="case-slide" data-visibility="hidden">
    <h2>Manufacturing Co</h2>
    <h3>70% snellere offertes</h3>
    <div class="case-metrics">
        <div class="metric"><span class="number">4 uur</span><span class="label">→ 45 minuten</span></div>
        <div class="metric"><span class="number">70%</span><span class="label">tijdsbesparing</span></div>
        <div class="metric"><span class="number">0</span><span class="label">fouten in offertes</span></div>
    </div>
    <p class="case-quote">"We maken nu in een ochtend wat vroeger een hele dag kostte."</p>
</section>""",
        "metrics": "70% tijdsbesparing, 0 fouten",
        "priority": 10,
    },
    {
        "id": "case-002",
        "title": "IT Services — Van 3 naar 12 leads per maand",
        "industry": "services",
        "pain_point": "lead_generation",
        "description": (
            "Geautomatiseerde lead scoring en outreach pipeline verhoogde het "
            "aantal gekwalificeerde leads met 300%."
        ),
        "slide_html": """<section id="case-002" class="case-slide" data-visibility="hidden">
    <h2>IT Services</h2>
    <h3>Van 3 naar 12 leads per maand</h3>
    <div class="case-metrics">
        <div class="metric"><span class="number">300%</span><span class="label">meer leads</span></div>
        <div class="metric"><span class="number">12</span><span class="label">per maand</span></div>
        <div class="metric"><span class="number">40%</span><span class="label">conversie</span></div>
    </div>
    <p class="case-quote">"Onze pipeline is voor het eerst in jaren gevuld."</p>
</section>""",
        "metrics": "300% meer leads, 40% conversie",
        "priority": 10,
    },
    {
        "id": "case-003",
        "title": "Retail Group — Team van 8 doet werk van 12",
        "industry": "retail",
        "pain_point": "capaciteit",
        "description": (
            "AI-automatisering van repetitieve taken gaf het team 50% meer "
            "capaciteit zonder extra personeel."
        ),
        "slide_html": """<section id="case-003" class="case-slide" data-visibility="hidden">
    <h2>Retail Group</h2>
    <h3>Team van 8 doet werk van 12</h3>
    <div class="case-metrics">
        <div class="metric"><span class="number">50%</span><span class="label">meer capaciteit</span></div>
        <div class="metric"><span class="number">0</span><span class="label">extra FTE nodig</span></div>
        <div class="metric"><span class="number">3 maanden</span><span class="label">ROI terugverdientijd</span></div>
    </div>
    <p class="case-quote">"We hoeven niet meer nee te zeggen tegen klanten."</p>
</section>""",
        "metrics": "50% meer capaciteit, 0 extra FTE",
        "priority": 10,
    },
    {
        "id": "case-004",
        "title": "Consultancy Firm — Kennis geborgd in 6 weken",
        "industry": "services",
        "pain_point": "kennisontsluiting",
        "description": (
            "AI-kennisbank die automatisch leert van documenten, mails en "
            "tickets. Nieuwe medewerkers vinden antwoorden in seconden."
        ),
        "slide_html": """<section id="case-004" class="case-slide" data-visibility="hidden">
    <h2>Consultancy Firm</h2>
    <h3>Kennis geborgd in 6 weken</h3>
    <div class="case-metrics">
        <div class="metric"><span class="number">80%</span><span class="label">sneller antwoord vinden</span></div>
        <div class="metric"><span class="number">6 weken</span><span class="label">tot volledig
operationeel</span></div>
        <div class="metric"><span class="number">0</span><span class="label">kennis verloren bij vertrek</span></div>
    </div>
    <p class="case-quote">"Nieuwe collega's zijn nu in weken productief, niet in maanden."</p>
</section>""",
        "metrics": "80% sneller antwoorden, 6 weken setup",
        "priority": 10,
    },
    {
        "id": "case-005",
        "title": "Logistiek Bedrijf — 20 uur per week bespaard",
        "industry": "manufacturing",
        "pain_point": "handmatig_werk",
        "description": "Automatisering van data-invoer tussen 4 systemen elimineerde copy-paste werk volledig.",
        "slide_html": """<section id="case-005" class="case-slide" data-visibility="hidden">
    <h2>Logistiek Bedrijf</h2>
    <h3>20 uur per week bespaard</h3>
    <div class="case-metrics">
        <div class="metric"><span class="number">20 uur</span><span class="label">per week bespaard</span></div>
        <div class="metric"><span class="number">0</span><span class="label">handmatige invoer</span></div>
        <div class="metric"><span class="number">99.8%</span><span class="label">accuracy</span></div>
    </div>
    <p class="case-quote">"Niemand mist het copy-paste werk."</p>
</section>""",
        "metrics": "20 uur/week bespaard, 99.8% accuracy",
        "priority": 10,
    },
]


async def main() -> None:
    db = SQLiteCaseDB(DB_PATH)
    await db.initialize()
    cases = [Case(**case) for case in EXAMPLE_CASES]
    await db.upsert_cases(cases)
    total_cases = len(await db.list_cases())

    print(f"Database seeded: {DB_PATH}")
    print(f"Total cases: {total_cases}")


if __name__ == "__main__":
    asyncio.run(main())
