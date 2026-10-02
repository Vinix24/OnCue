#!/usr/bin/env python3
"""Label speakers (VINCENT / FRANK) in a transcript and generate a sales summary.

Usage:
    python scripts/label_and_summarize.py <transcript.txt> --prospect-name Frank
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from sales_copilot.core.config import env_int, load_env
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.outbound_policy import sanitize_for_outbound


class LabeledTurn(BaseModel):
    timestamp: str = Field(description="[MM:SS] from the transcript")
    speaker: str = Field(description="VINCENT or PROSPECT")
    text: str


class LabelingResult(BaseModel):
    turns: list[LabeledTurn]


class FrankWish(BaseModel):
    category: str = Field(description="Short label, e.g. 'CRM', 'Voice capture', 'Lead intelligence'")
    description: str = Field(description="What Frank wants, in his own words paraphrased")
    quote_timestamp: str = Field(description="A [MM:SS] timestamp where this came up")
    priority: str = Field(description="HIGH / MEDIUM / LOW based on how Frank discussed it")


class ProposedModule(BaseModel):
    name: str
    description: str = Field(description="What this module delivers, in Vincent's voice")
    addresses_wishes: list[str] = Field(description="Which wish categories this module solves")
    setup_hours: int = Field(description="Estimated setup hours")
    setup_price_eur: int = Field(description="One-time setup price in EUR")
    monthly_eur: int = Field(description="Monthly maintenance/support EUR, 0 if none")


class CallSummary(BaseModel):
    one_paragraph_summary: str = Field(description="3-5 sentences in Dutch describing the call")
    frank_wishes: list[FrankWish]
    proposed_modules: list[ProposedModule]
    total_setup_eur_range: str = Field(description="e.g. '€12.000 - €18.000'")
    total_monthly_eur_range: str = Field(description="e.g. '€350 - €500'")
    recommended_starting_module: str = Field(description="Which module Vincent should propose first and why")
    risks_or_concerns: list[str] = Field(description="Things Frank brought up that need careful handling")
    next_step: str = Field(description="Concrete next step for Vincent")


def _llm_client(provider: str) -> LLMClient:
    load_env()
    return LLMClient(
        provider=provider,
        timeout_ms=env_int("LLM_TIMEOUT_MS", 60_000) or 60_000,
    )


def label_speakers(
    transcript: str, prospect_name: str, model: str, provider: str
) -> LabelingResult:
    transcript = sanitize_for_outbound(transcript, provider=provider, allow_local=True)
    prospect_name = sanitize_for_outbound(prospect_name, provider=provider, allow_local=True)
    system = (
        "Je labelt een Nederlandse sales-call transcript. "
        "VINCENT is een solo AI-consultant die zijn diensten pitcht en daarnaast "
        "AI-oplossingen ontwikkelt voor MKB-klanten. Hij praat over zichzelf in de eerste "
        "persoon over salesmanagers, AI-implementaties, lokale modellen op MacBook, "
        "agents, knowledge bases. "
        f"PROSPECT ({prospect_name}) is een coach voor MKB-ondernemers. Hij geeft "
        "workshops, heeft 35+ klanten in begeleiding, omzetdoel €4,5M, charges "
        "€7500/jaar voor coaching, gebruikt geen CRM, deelt wekelijks documenten met "
        "zijn coachees, doet trainingen voor groepen van 8-15 ondernemers. "
        "Per beurt label je VINCENT of PROSPECT. Bij twijfel: kijk naar context "
        "(over wie wordt gesproken, welk perspectief, welk jargon). "
        "Behoud de tijdstempel [MM:SS] zoals in input. Split nooit een [MM:SS]-blok in "
        "twee turns als sprekers wisselen — kies de hoofdspreker van dat blok."
    )
    return _llm_client(provider).create(
        model=model,
        system_prompt=system,
        user_text=f"""Transcript om te labellen:

{transcript}""",
        response_model=LabelingResult,
        temperature=0.0,
        allow_local=True,
    )


def summarize_for_pitch(
    labeled: LabelingResult, prospect_name: str, model: str, provider: str
) -> CallSummary:
    prospect_name = sanitize_for_outbound(prospect_name, provider=provider, allow_local=True)
    labeled_text = "\n".join(
        f"[{t.timestamp}] {t.speaker}: {sanitize_for_outbound(t.text, provider=provider, allow_local=True)}"
        for t in labeled.turns
    )
    system = (
        f"Je bent een sales-coach voor Vincent (solo AI-consultant). Je leest een "
        f"call-transcript tussen Vincent en {prospect_name} (een MKB-coach). "
        f"Je doel: identificeer alle wensen die {prospect_name} heeft uitgesproken voor "
        f"AI-tools/automatisering, en stel een MODULAIR aanbod samen dat Vincent kan "
        f"pitchen. {prospect_name} heeft expliciet aangegeven dat hij modulair wil kopen "
        f"en sceptisch is over hoge investeringen aan de voorkant. "
        f"Pricing-richtlijn voor MKB AI-implementaties in NL: setup per module "
        f"€2.000-€5.000, maandelijks support €250-€750, knowledge-base setup "
        f"€1.500-€3.000. {prospect_name} rekent zelf €7.500/jaar voor coaching, dus prijs "
        f"is bekend bij dit niveau klant. "
        f"Vincent's stijl: lokale AI (privacy-first), modulair, geen wollige praatjes, "
        f"concrete deliverables. "
        f"Schrijf in het Nederlands."
    )
    return _llm_client(provider).create(
        model=model,
        system_prompt=system,
        user_text=f"Gelabeld transcript:\n\n{labeled_text}",
        response_model=CallSummary,
        temperature=0.2,
        allow_local=True,
    )


def render_labeled(labeled: LabelingResult) -> str:
    lines: list[str] = []
    for t in labeled.turns:
        lines.append(f"[{t.timestamp}] {t.speaker}: {t.text}")
    return "\n".join(lines)


def render_summary(summary: CallSummary, prospect_name: str) -> str:
    out: list[str] = []
    out.append(f"# Call-samenvatting — Vincent x {prospect_name}\n")
    out.append("## Samenvatting in één paragraaf\n")
    out.append(summary.one_paragraph_summary)
    out.append("")
    out.append(f"## Wensen van {prospect_name}\n")
    for w in summary.frank_wishes:
        out.append(f"### {w.category} ({w.priority})")
        out.append(f"_{w.quote_timestamp}_")
        out.append(w.description)
        out.append("")
    out.append("## Voorgesteld modulair aanbod\n")
    for i, m in enumerate(summary.proposed_modules, 1):
        out.append(f"### Module {i} — {m.name}")
        out.append(m.description)
        out.append(f"**Dekt wensen:** {', '.join(m.addresses_wishes)}")
        out.append(f"**Setup:** {m.setup_hours} uur — €{m.setup_price_eur:,}".replace(",", "."))
        if m.monthly_eur:
            out.append(f"**Maandelijks:** €{m.monthly_eur}")
        out.append("")
    out.append("## Totaal-pricing range\n")
    out.append(f"- Setup totaal: **{summary.total_setup_eur_range}**")
    out.append(f"- Maandelijks support: **{summary.total_monthly_eur_range}**")
    out.append("")
    out.append("## Aanbevolen startmodule\n")
    out.append(summary.recommended_starting_module)
    out.append("")
    if summary.risks_or_concerns:
        out.append("## Aandachtspunten\n")
        for r in summary.risks_or_concerns:
            out.append(f"- {r}")
        out.append("")
    out.append("## Volgende concrete stap\n")
    out.append(summary.next_step)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Label speakers + summarize a sales call")
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--prospect-name", default="Frank")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument(
        "--provider",
        default=os.getenv("LLM_PROVIDER", "gemini"),
        help="LLM provider (default: LLM_PROVIDER env var or gemini)",
    )
    parser.add_argument("--labeled-output", type=Path, default=None)
    parser.add_argument("--summary-output", type=Path, default=None)
    args = parser.parse_args(argv)

    transcript = args.transcript.read_text(encoding="utf-8")
    print(f"Labelen ({args.model})...", file=sys.stderr)
    provider = args.provider.strip().lower()
    labeled = label_speakers(transcript, args.prospect_name, args.model, provider)
    print(f"  {len(labeled.turns)} beurten gelabeled", file=sys.stderr)

    print(f"Samenvatten ({args.model})...", file=sys.stderr)
    summary = summarize_for_pitch(labeled, args.prospect_name, args.model, provider)
    print(f"  {len(summary.frank_wishes)} wensen, {len(summary.proposed_modules)} modules", file=sys.stderr)

    labeled_text = render_labeled(labeled)
    summary_text = render_summary(summary, args.prospect_name)

    labeled_path = args.labeled_output or args.transcript.with_name(
        args.transcript.stem + "-labeled.txt"
    )
    summary_path = args.summary_output or args.transcript.with_name(
        args.transcript.stem + "-summary.md"
    )
    labeled_path.write_text(labeled_text, encoding="utf-8")
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"\nGelabeld → {labeled_path}", file=sys.stderr)
    print(f"Samenvatting → {summary_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
