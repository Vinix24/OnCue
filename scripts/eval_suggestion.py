#!/usr/bin/env python3
"""Fase-B track B3: live-suggestie / weerlegging-evaluator voor harde bezwaarmomenten.

Voor elke "hard moment" (een gemijnde prospect-uiting met een gedetecteerde
bezwaarcategorie, zie ``scripts/mine_objection_eval_set.py``) genereert dit
script twee weerleggingen -- één van de OpenRouter-kandidaat
(``qwen/qwen3.6-35b-a3b``) en één van een Claude Sonnet referentie (eveneens
via OpenRouter, geen ``anthropic`` SDK) -- met de klant-specifieke sales-prep
(secties 4/5/6: bezwaren-antwoord-hoek, differentiators, trigger) als context.
Dit is een KWALITATIEVE side-by-side vergelijking: geen scores, geen ground
truth. De uitkomst is bedoeld om te beoordelen of de kandidaat bruikbare
live-weerleggingen genereert in vergelijking met de referentie.

Reuse: ``config/eval_model_register.yaml`` levert de kandidaat-modelspec (via
``load_model_register`` -- zie ``_load_candidate_spec``),
``sales_copilot.core.llm_client.LLMClient`` doet de instructor-patched
structured-output call, en ``eval_shared.py`` levert ``ModelSpec``,
``PricingTable`` en ``is_provider_available``. De structuur mirrort
``scripts/run_fase_b.py``: standaard een cost dry-run (geen provider
aangeroepen), ``--run`` voor de echte (betaalde) evaluatie.

Input (gitignored, runtime-only -- dit script leest ze, genereert ze niet):
    - ``data/objection_eval/mined_<client>__*.jsonl`` -- gemijnde objection-
      rows per klant/transcript (``scripts/mine_objection_eval_set.py``).
      Rows met een non-lege ``predicted_category`` gelden als "hard moment".
    - ``data/fireflies/<client>/sales-prep.md`` -- 8-secties sales-prep per
      klant. Secties 4 ("Waarschijnlijke bezwaren + beste antwoord-hoek"),
      5 ("Relevante differentiators") en 6 ("Trigger") worden als context
      geïnjecteerd; de rest (bedrijfscontext, DMU, concurrenten, next-step)
      blijft buiten de prompt.

De kandidaat is een THINKING-model (Qwen3.6 heeft thinking aan by default). De
gedeelde ``LLMClient.create()``-seam (``src/sales_copilot/core/llm_client.py``)
geeft daarom voor de kandidaat-call een expliciete, royale ``max_tokens`` mee
(``_ESTIMATED_OUTPUT_TOKENS_CANDIDATE``) zodat de redeneertrace plus het
uiteindelijke antwoord allebei passen -- zonder die budget-vloer kapt een laag
provider-default de trace af voordat het antwoord er is en komt ``content``
als ``None`` terug (``finish_reason='length'``). De referentie (niet-thinking,
kort antwoord) krijgt geen ``max_tokens``-cap: het provider-default was daar
nooit het probleem, en een cap zou alleen risico toevoegen.

Rate-limit-fouten (Google 429 RESOURCE_EXHAUSTED, OpenRouter 429/rate_limit)
worden EVAL-ONLY opgevangen met exponential backoff + jitter (tot 4 retries),
plus een kleine, configureerbare pacing tussen calls (``--pace-ms``, default
150ms) om quota-druk te verminderen. Beide leven in
``sales_copilot.modules.detector.eval_shared`` en raken de live-copilot-laag
(``core/llm_client.py``'s gedeelde seam, ``3s``-budget) niet aan.

Voorbeelden:

    # Cost-preview (default, veilig, geen API-calls)
    .venv/bin/python scripts/eval_suggestion.py

    # Echte run (NIET vanuit deze worktree -- betaalde inference)
    .venv/bin/python scripts/eval_suggestion.py --run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from sales_copilot.core.config import DetectorConfig, load_env  # noqa: E402
from sales_copilot.core.llm_client import LLMClient  # noqa: E402
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    DEFAULT_BASE_DELAY_S,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PACE_MS,
    ModelSpec,
    PricingTable,
    is_provider_available,
    load_model_register,
    pace,
    parse_model_specs,
    resilience_header_lines,
    retry_with_backoff,
)

logger = logging.getLogger(__name__)

_DEFAULT_REGISTER = REPO_ROOT / "config" / "eval_model_register.yaml"
_DEFAULT_FIREFLIES_DIR = REPO_ROOT / "data" / "fireflies"
_DEFAULT_OBJECTION_DIR = REPO_ROOT / "data" / "objection_eval"
_DEFAULT_PRICES = REPO_ROOT / "config" / "eval_model_prices.yaml"
_DEFAULT_REPORT_MD = REPO_ROOT / "claudedocs" / "2026-07-16-b3-suggestion-results.md"
_DEFAULT_REPORT_JSON = REPO_ROOT / "claudedocs" / "2026-07-16-b3-suggestion-results.json"

# Kandidaat matcht de fase-A/B "local candidate" uit config/eval_model_register.yaml.
# De referentie is B3-specifiek (kwaliteitsvergelijking, niet onderdeel van de bredere
# fase-B shootout-panel) en leeft daarom alleen hier, niet in de gedeelde register-yaml.
_CANDIDATE_PROVIDER = "openrouter"
_CANDIDATE_MODEL = "qwen/qwen3.6-35b-a3b"
_REFERENCE_SPEC = ModelSpec(
    provider="openrouter",
    model="anthropic/claude-sonnet-5",
    description=(
        "Claude Sonnet 5 via OpenRouter (OpenAI-compatible endpoint, geen anthropic SDK) -- "
        "B3 kwaliteitsreferentie. Model-id + pricing zijn NIET geverifieerd tegen de live "
        "OpenRouter-feed (dit worker-profiel heeft geen WebFetch/WebSearch); zie Open Items."
    ),
)

_TOP_N_PER_CLIENT = 6

# Ruwe cost-estimation heuristiek voor de dry-run, zelfde ~4 tekens/token vuistregel als
# scripts/run_fase_b.py.
_CHARS_PER_TOKEN = 4
# Kandidaat is een thinking model: het provider-default max_tokens staat aan (zie
# module-docstring), dus dit is een ruwe bovengrens voor redeneertrace + antwoord samen,
# geen precieze voorspelling.
_ESTIMATED_OUTPUT_TOKENS_CANDIDATE = 4096
# Referentie is een niet-thinking model met een kort, beknopt antwoord (2-4 zinnen).
_ESTIMATED_OUTPUT_TOKENS_REFERENCE = 300

_DEFAULT_TIMEOUT_MS = 30_000

# Sales-prep secties zijn genummerd maar de nummering/parenthetische toevoegingen kunnen
# per klant licht verschillen (zie "6. Trigger (waar het hem raakt)"), dus matchen op een
# stabiel trefwoord in plaats van de exacte headertekst.
_SECTION_KEYWORDS: dict[str, str] = {
    "objections": "bezwaren",
    "differentiators": "differentiator",
    "trigger": "trigger",
}

_LANGUAGE_MARKER_RE = re.compile(r"\*\*Taal:\*\*\s*([^.\n]+)", re.IGNORECASE)
_EN_MARKER_WORDS = ("engels", "english")

_NL_STOPWORDS = frozenset(
    {
        "de", "het", "een", "van", "dat", "met", "voor", "niet", "wij", "dit", "is", "en",
        "we", "ik", "je", "wat", "maar", "ja", "hebben", "zijn",
    }
)
_EN_STOPWORDS = frozenset(
    {"the", "and", "you", "that", "for", "with", "this", "our", "is", "are", "we", "i", "what", "but", "have", "yes"}
)

_NL_SYSTEM_PROMPT = (
    "Je bent een ervaren B2B-verkoopcoach die een verkoper live tijdens een gesprek helpt. "
    "Op basis van de sales-prep-context en het bezwaar van de prospect formuleer je ÉÉN "
    "beknopte, direct bruikbare weerlegging (2-4 zinnen) die de verkoper hardop kan zeggen. "
    "Koppel de weerlegging waar relevant aan de differentiators en de trigger uit de "
    "sales-prep. Antwoord in het Nederlands."
)
_EN_SYSTEM_PROMPT = (
    "You are an experienced B2B sales coach helping a salesperson live during a call. "
    "Based on the sales-prep context and the prospect's objection, craft ONE concise, "
    "immediately usable rebuttal (2-4 sentences) the salesperson can say out loud. Tie the "
    "rebuttal to the prep's differentiators and trigger where relevant. Answer in English."
)


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


class RebuttalSuggestion(BaseModel):
    """Strict schema returned by the LLM via instructor."""

    rebuttal: str = Field(
        ...,
        description=(
            "Eén beknopte, direct bruikbare weerlegging (2-4 zinnen) die de verkoper hardop "
            "kan zeggen, aansluitend bij de sales-prep context."
        ),
    )


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardMoment:
    """One mined objection utterance selected as a "hard moment" for a client."""

    client: str
    source: str
    id: str
    text: str
    predicted_category: str | None
    predicted_confidence: float | None
    language: str


@dataclass
class DiscoveryResult:
    hard_moments_by_client: dict[str, list[HardMoment]] = field(default_factory=dict)
    skipped_clients: dict[str, str] = field(default_factory=dict)


@dataclass
class SuggestionResult:
    """One hard moment after generating both the candidate and reference rebuttal."""

    moment: HardMoment
    candidate_spec: ModelSpec
    reference_spec: ModelSpec
    candidate_rebuttal: str | None
    candidate_meta: dict[str, Any]
    reference_rebuttal: str | None
    reference_meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.moment.client,
            "source": self.moment.source,
            "id": self.moment.id,
            "utterance": self.moment.text,
            "predicted_category": self.moment.predicted_category,
            "predicted_confidence": self.moment.predicted_confidence,
            "language": self.moment.language,
            "candidate_model": str(self.candidate_spec),
            "candidate_rebuttal": self.candidate_rebuttal,
            "candidate_latency_ms": self.candidate_meta.get("latency_ms"),
            "candidate_usage": self.candidate_meta.get("usage"),
            "candidate_error": self.candidate_meta.get("error"),
            "reference_model": str(self.reference_spec),
            "reference_rebuttal": self.reference_rebuttal,
            "reference_latency_ms": self.reference_meta.get("latency_ms"),
            "reference_usage": self.reference_meta.get("usage"),
            "reference_error": self.reference_meta.get("error"),
        }


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _estimate_tokens(chars: int) -> int:
    return max(1, chars // _CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Sales-prep parsing
# ---------------------------------------------------------------------------


def parse_sales_prep_sections(text: str) -> dict[str, str]:
    """Split a sales-prep markdown into ``##``-sections and return the three target ones.

    Returns a dict with up to the keys ``objections``, ``differentiators`` and ``trigger``
    (only present when found), each mapped to the section's full text including its
    heading. Sections not matched by ``_SECTION_KEYWORDS`` (bedrijfscontext, DMU,
    concurrenten, next-step, ...) are parsed but discarded -- they must never leak into
    the rebuttal prompt.
    """
    sections: dict[str, str] = {}
    current_header: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_header is not None:
                sections[current_header] = "\n".join(current_lines).strip()
            current_header = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_header is not None:
        sections[current_header] = "\n".join(current_lines).strip()

    result: dict[str, str] = {}
    for key, keyword in _SECTION_KEYWORDS.items():
        for header, body in sections.items():
            if keyword in header.lower():
                result[key] = f"## {header}\n{body}".strip()
                break
    return result


def load_prep_sections(fireflies_dir: Path, client: str) -> dict[str, str]:
    text = (fireflies_dir / client / "sales-prep.md").read_text(encoding="utf-8")
    return parse_sales_prep_sections(text)


def _detect_language_from_text(text: str) -> str:
    tokens = re.findall(r"[a-zA-ZàáâäçèéêëìíîïñòóôöùúûüÀ-ÿ]+", text.lower())
    nl_hits = sum(1 for t in tokens if t in _NL_STOPWORDS)
    en_hits = sum(1 for t in tokens if t in _EN_STOPWORDS)
    return "en" if en_hits > nl_hits else "nl"


def detect_language(sales_prep_text: str, utterance: str) -> str:
    """Return ``"en"`` or ``"nl"`` for a hard moment's rebuttal language.

    Prefers the sales-prep's explicit ``**Taal:**`` marker (only some clients have one
    today); falls back to a lightweight Dutch/English stopword heuristic on the
    utterance itself when no marker is present.
    """
    match = _LANGUAGE_MARKER_RE.search(sales_prep_text)
    if match:
        marker = match.group(1).strip().lower()
        if any(word in marker for word in _EN_MARKER_WORDS):
            return "en"
        return "nl"
    return _detect_language_from_text(utterance)


def system_prompt_for_language(language: str) -> str:
    return _EN_SYSTEM_PROMPT if language == "en" else _NL_SYSTEM_PROMPT


def build_user_prompt(prep_sections: dict[str, str], utterance: str, category: str | None) -> str:
    """Build the rebuttal-generation prompt: prep-context (sections 4/5/6 only) + utterance."""
    parts = [prep_sections[key] for key in ("objections", "differentiators", "trigger") if key in prep_sections]
    context_block = "\n\n".join(parts) if parts else "(geen sales-prep-context beschikbaar)"
    category_line = f"Gedetecteerde bezwaarcategorie: {category}\n" if category else ""
    return (
        f"Sales-prep context:\n{context_block}\n\n"
        f"{category_line}"
        f'Bezwaar van de prospect (letterlijk uit het transcript):\n"{utterance}"\n\n'
        "Formuleer de weerlegging."
    )


# ---------------------------------------------------------------------------
# Hard-moment discovery
# ---------------------------------------------------------------------------


def load_mined_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def select_hard_moments(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` rows with a non-empty ``predicted_category``.

    Ordered by ``predicted_confidence`` descending when every candidate row carries one;
    otherwise the mined-file order is kept (first ``limit`` rows) rather than sorting on a
    partially-missing field.
    """
    hard = [r for r in rows if r.get("predicted_category")]
    if hard and all(r.get("predicted_confidence") is not None for r in hard):
        hard = sorted(hard, key=lambda r: r["predicted_confidence"], reverse=True)
    return hard[:limit]


def discover_hard_moments(fireflies_dir: Path, objection_dir: Path, limit: int) -> DiscoveryResult:
    """Discover, per client, the top ``limit`` hard moments with sales-prep context.

    A client is skipped (with a reason recorded) when it has no ``sales-prep.md``, no
    matching ``mined_<client>__*.jsonl`` files, or no rows with a predicted category.
    """
    result = DiscoveryResult()
    if not fireflies_dir.exists():
        return result

    for client_dir in sorted(p for p in fireflies_dir.iterdir() if p.is_dir()):
        client = client_dir.name
        sales_prep_path = client_dir / "sales-prep.md"
        if not sales_prep_path.exists():
            result.skipped_clients[client] = "geen sales-prep.md"
            continue

        mined_paths = sorted(objection_dir.glob(f"mined_{client}__*.jsonl")) if objection_dir.exists() else []
        if not mined_paths:
            result.skipped_clients[client] = "geen gemijnde objection-rows (mined_<client>__*.jsonl)"
            continue

        rows = load_mined_rows(mined_paths)
        hard_rows = select_hard_moments(rows, limit)
        if not hard_rows:
            result.skipped_clients[client] = "geen rows met predicted_category"
            continue

        sales_prep_text = sales_prep_path.read_text(encoding="utf-8")
        result.hard_moments_by_client[client] = [
            HardMoment(
                client=client,
                source=row.get("source", ""),
                id=row.get("id", ""),
                text=row["text"],
                predicted_category=row.get("predicted_category"),
                predicted_confidence=row.get("predicted_confidence"),
                language=detect_language(sales_prep_text, row["text"]),
            )
            for row in hard_rows
        ]

    return result


def _load_candidate_spec(register_path: Path) -> ModelSpec:
    """Return the fase-B local candidate from the shared register, or a documented fallback."""
    if register_path.exists():
        for spec in load_model_register(register_path):
            if spec.provider == _CANDIDATE_PROVIDER and spec.model == _CANDIDATE_MODEL:
                return spec
    logger.warning(
        "Kandidaat %s:%s niet gevonden in %s; val terug op een spec zonder register-description.",
        _CANDIDATE_PROVIDER,
        _CANDIDATE_MODEL,
        register_path,
    )
    return ModelSpec(
        provider=_CANDIDATE_PROVIDER,
        model=_CANDIDATE_MODEL,
        description="Qwen3.6-35B-A3B via OpenRouter -- fase-B B3 live-suggestie kandidaat (THINKING model).",
    )


# ---------------------------------------------------------------------------
# Dry-run: cost preview, geen enkele provider aangeroepen
# ---------------------------------------------------------------------------


def estimate_dry_run_cost(
    hard_moments_by_client: dict[str, list[HardMoment]],
    fireflies_dir: Path,
    pricing: PricingTable,
    candidate_spec: ModelSpec,
    reference_spec: ModelSpec,
) -> dict[str, Any]:
    total_input_chars = 0
    total_calls = 0
    for client, moments in hard_moments_by_client.items():
        prep_sections = load_prep_sections(fireflies_dir, client)
        for moment in moments:
            system_prompt = system_prompt_for_language(moment.language)
            user_prompt = build_user_prompt(prep_sections, moment.text, moment.predicted_category)
            total_input_chars += len(system_prompt) + len(user_prompt)
            total_calls += 1

    input_tokens = _estimate_tokens(total_input_chars)
    candidate_output_tokens = total_calls * _ESTIMATED_OUTPUT_TOKENS_CANDIDATE
    reference_output_tokens = total_calls * _ESTIMATED_OUTPUT_TOKENS_REFERENCE

    return {
        "total_calls": total_calls,
        "input_tokens": input_tokens,
        "candidate_output_tokens": candidate_output_tokens,
        "reference_output_tokens": reference_output_tokens,
        "candidate_cost": pricing.estimate_cost(
            candidate_spec.provider, candidate_spec.model, input_tokens, candidate_output_tokens
        ),
        "reference_cost": pricing.estimate_cost(
            reference_spec.provider, reference_spec.model, input_tokens, reference_output_tokens
        ),
    }


def _run_dry_run(
    candidate_spec: ModelSpec,
    reference_spec: ModelSpec,
    pricing: PricingTable,
    discovery: DiscoveryResult,
    args: argparse.Namespace,
) -> int:
    lines: list[str] = ["# Fase-B B3 cost dry-run (GEEN provider aangeroepen)", ""]

    lines.append("## Model-panel")
    lines.append("")
    for role, spec in (("Kandidaat", candidate_spec), ("Referentie", reference_spec)):
        avail, reason = is_provider_available(spec.provider)
        marker = "beschikbaar" if avail else "NIET beschikbaar"
        suffix = f" -- {spec.description}" if spec.description else ""
        lines.append(f"- **{role}:** `{spec}` -- {marker} ({reason}){suffix}")
    lines.append("")

    total_moments = sum(len(v) for v in discovery.hard_moments_by_client.values())
    lines.append("## Hard moments")
    lines.append("")
    lines.append(
        f"- Totaal: {total_moments} hard moments over {len(discovery.hard_moments_by_client)} klanten "
        f"(cap {args.top_n}/klant)"
    )
    for client in sorted(discovery.hard_moments_by_client):
        lines.append(f"  - {client}: {len(discovery.hard_moments_by_client[client])}")
    if discovery.skipped_clients:
        lines.append("")
        lines.append("**Overgeslagen klanten:**")
        for client, reason in sorted(discovery.skipped_clients.items()):
            lines.append(f"  - {client}: {reason}")
    lines.append("")

    estimate = estimate_dry_run_cost(
        discovery.hard_moments_by_client, args.fireflies_dir, pricing, candidate_spec, reference_spec
    )
    lines.append("## Geschatte kosten (bovengrens)")
    lines.append("")
    lines.append(
        "Kandidaat is een THINKING-model; max_tokens wordt bewust NIET laag gezet (provider-default "
        f"blijft staan, zie module-docstring), dus de output-schatting "
        f"({_ESTIMATED_OUTPUT_TOKENS_CANDIDATE} tokens/call) is een ruwe bovengrens voor redeneertrace "
        "+ antwoord samen. ~4 tekens/token heuristiek."
    )
    lines.append("")
    lines.append("| Rol | Model | Input tokens | Output tokens | Geschatte kosten |")
    lines.append("|---|---|---|---|---|")
    grand_total = 0.0
    any_unknown = False
    for role, spec, out_tok, cost in (
        ("Kandidaat", candidate_spec, estimate["candidate_output_tokens"], estimate["candidate_cost"]),
        ("Referentie", reference_spec, estimate["reference_output_tokens"], estimate["reference_cost"]),
    ):
        if cost is None:
            any_unknown = True
            cost_str = "onbekend (geen prijs-entry)"
        else:
            grand_total += cost
            cost_str = f"${cost:.4f}"
        lines.append(f"| {role} | {spec} | {estimate['input_tokens']} | {out_tok} | {cost_str} |")
    lines.append("")
    incomplete_note = " (onvolledig -- niet elk model heeft een prijs-entry)" if any_unknown else ""
    lines.append(f"**Geschatte totaalkosten over kandidaat + referentie: ${grand_total:.4f}**{incomplete_note}")
    lines.append("")
    lines.append("Dit was een DRY-RUN: er is geen enkele provider aangeroepen. Geef `--run` voor de echte evaluatie.")

    report = "\n".join(lines) + "\n"
    print(report)
    return 0


# ---------------------------------------------------------------------------
# Echte run
# ---------------------------------------------------------------------------


def generate_rebuttal(
    spec: ModelSpec,
    moment: HardMoment,
    prep_sections: dict[str, str],
    config: DetectorConfig,
    timeout_ms: int,
    *,
    max_tokens: int | None = None,
    pace_ms: int = DEFAULT_PACE_MS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
) -> tuple[str | None, dict[str, Any]]:
    """Issue one structured-output rebuttal call. Returns ``(rebuttal_text, meta)``.

    ``max_tokens`` should be set generously for a thinking-capable candidate model (the
    caller passes ``_ESTIMATED_OUTPUT_TOKENS_CANDIDATE``) so the reasoning trace does not
    exhaust the output budget before the final answer is emitted; left ``None`` for a
    non-thinking model. ``pace_ms``/``max_retries``/``base_delay_s`` configure EVAL-ONLY
    inter-call pacing and retry-with-backoff on rate-limit errors (see
    ``eval_shared.pace``/``retry_with_backoff``) -- this script has no live-path caller, so
    the live copilot's latency budget is unaffected.
    """
    client = LLMClient(spec.provider, timeout_ms=timeout_ms)
    system_prompt = system_prompt_for_language(moment.language)
    user_prompt = build_user_prompt(prep_sections, moment.text, moment.predicted_category)

    start = time.perf_counter()
    try:
        pace(pace_ms)
        response = retry_with_backoff(
            lambda: client.create(
                model=spec.model,
                system_prompt=system_prompt,
                user_text=user_prompt,
                response_model=RebuttalSuggestion,
                temperature=config.llm_temperature,
                allow_local=True,
                max_tokens=max_tokens,
            ),
            max_retries=max_retries,
            base_delay_s=base_delay_s,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - start) * 1000
        logger.warning("Rebuttal-generatie faalde voor %s (moment %s): %s", spec, moment.id, exc)
        return None, {"latency_ms": latency_ms, "usage": {}, "error": str(exc)}

    latency_ms = (time.perf_counter() - start) * 1000
    usage = client.last_usage or {}

    if response is None:
        return None, {
            "latency_ms": latency_ms,
            "usage": usage,
            "error": "LLM returned None (provider disabled, timeout, of leeg antwoord)",
        }

    return response.rebuttal, {"latency_ms": latency_ms, "usage": usage, "error": None}


def run_real(
    hard_moments_by_client: dict[str, list[HardMoment]],
    fireflies_dir: Path,
    config: DetectorConfig,
    timeout_ms: int,
    candidate_spec: ModelSpec,
    reference_spec: ModelSpec,
    *,
    pace_ms: int = DEFAULT_PACE_MS,
) -> dict[str, list[SuggestionResult]]:
    candidate_available, candidate_reason = is_provider_available(candidate_spec.provider)
    reference_available, reference_reason = is_provider_available(reference_spec.provider)

    results_by_client: dict[str, list[SuggestionResult]] = {}
    for client, moments in hard_moments_by_client.items():
        prep_sections = load_prep_sections(fireflies_dir, client)
        client_results: list[SuggestionResult] = []
        for moment in moments:
            logger.info("B3: genereren van weerleggingen voor %s / %s", client, moment.id)
            if candidate_available:
                candidate_text, candidate_meta = generate_rebuttal(
                    candidate_spec,
                    moment,
                    prep_sections,
                    config,
                    timeout_ms,
                    max_tokens=_ESTIMATED_OUTPUT_TOKENS_CANDIDATE,
                    pace_ms=pace_ms,
                )
            else:
                candidate_text, candidate_meta = None, {
                    "latency_ms": 0.0,
                    "usage": {},
                    "error": f"provider unavailable: {candidate_reason}",
                }
            if reference_available:
                reference_text, reference_meta = generate_rebuttal(
                    reference_spec, moment, prep_sections, config, timeout_ms, pace_ms=pace_ms
                )
            else:
                reference_text, reference_meta = None, {
                    "latency_ms": 0.0,
                    "usage": {},
                    "error": f"provider unavailable: {reference_reason}",
                }
            client_results.append(
                SuggestionResult(
                    moment=moment,
                    candidate_spec=candidate_spec,
                    reference_spec=reference_spec,
                    candidate_rebuttal=candidate_text,
                    candidate_meta=candidate_meta,
                    reference_rebuttal=reference_text,
                    reference_meta=reference_meta,
                )
            )
        results_by_client[client] = client_results

    return results_by_client


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def format_markdown_report(
    results_by_client: dict[str, list[SuggestionResult]],
    candidate_spec: ModelSpec,
    reference_spec: ModelSpec,
    skipped_clients: dict[str, str],
    *,
    effective_timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    pace_ms: int = DEFAULT_PACE_MS,
) -> str:
    lines: list[str] = [
        "# Fase-B B3 -- live-suggestie shootout",
        "",
        f"**Kandidaat:** `{candidate_spec}`"
        + (f" -- {candidate_spec.description}" if candidate_spec.description else ""),
        f"**Referentie:** `{reference_spec}`"
        + (f" -- {reference_spec.description}" if reference_spec.description else ""),
        *resilience_header_lines(
            effective_timeout_ms=effective_timeout_ms,
            pace_ms=pace_ms,
            thinking_output_tokens=_ESTIMATED_OUTPUT_TOKENS_CANDIDATE,
        ),
        "",
        "Kwalitatief side-by-side per hard moment (bezwaar met een gedetecteerde categorie). "
        "Geen scores, geen ground truth -- beoordeel de weerleggingen op leesbaarheid en "
        "aansluiting bij de sales-prep.",
        "",
    ]

    for client in sorted(results_by_client):
        results = results_by_client[client]
        lines.append(f"## {client}")
        lines.append("")
        lang = results[0].moment.language if results else "n/a"
        lines.append(f"**Taal:** {lang}")
        lines.append("")
        lines.append("| Bron | Categorie | Bezwaar | Qwen3.6 | Sonnet |")
        lines.append("|---|---|---|---|---|")
        for r in results:
            cand = (
                _md_escape(r.candidate_rebuttal)
                if r.candidate_rebuttal
                else f"_fout: {r.candidate_meta.get('error', 'onbekend')}_"
            )
            ref = (
                _md_escape(r.reference_rebuttal)
                if r.reference_rebuttal
                else f"_fout: {r.reference_meta.get('error', 'onbekend')}_"
            )
            lines.append(
                f"| {r.moment.source} | {r.moment.predicted_category or '-'} | "
                f"{_md_escape(r.moment.text)} | {cand} | {ref} |"
            )
        lines.append("")

    if skipped_clients:
        lines.append("## Overgeslagen klanten")
        lines.append("")
        for client, reason in sorted(skipped_clients.items()):
            lines.append(f"- {client}: {reason}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _run_real(
    candidate_spec: ModelSpec,
    reference_spec: ModelSpec,
    discovery: DiscoveryResult,
    args: argparse.Namespace,
) -> int:
    config = DetectorConfig.from_env()
    results_by_client = run_real(
        discovery.hard_moments_by_client,
        args.fireflies_dir,
        config,
        args.timeout_ms,
        candidate_spec,
        reference_spec,
        pace_ms=args.pace_ms,
    )

    report_md = format_markdown_report(
        results_by_client,
        candidate_spec,
        reference_spec,
        discovery.skipped_clients,
        effective_timeout_ms=args.timeout_ms,
        pace_ms=args.pace_ms,
    )
    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(report_md, encoding="utf-8")

    payload = {
        "mode": "run",
        "candidate_model": str(candidate_spec),
        "reference_model": str(reference_spec),
        "skipped_clients": discovery.skipped_clients,
        "results": {client: [r.to_dict() for r in results] for client, results in results_by_client.items()},
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(report_md)
    print(f"\nConsolidated report: {args.report_md}")
    print(f"Machine-readable JSON: {args.report_json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models-yaml",
        type=Path,
        default=_DEFAULT_REGISTER,
        help=f"Fase-B model-register waar de kandidaat-spec uit gelezen wordt. Default: {_DEFAULT_REGISTER}",
    )
    parser.add_argument(
        "--fireflies-dir",
        type=Path,
        default=_DEFAULT_FIREFLIES_DIR,
        help=f"Root met <klant>/sales-prep.md. Default: {_DEFAULT_FIREFLIES_DIR}",
    )
    parser.add_argument(
        "--objection-dir",
        type=Path,
        default=_DEFAULT_OBJECTION_DIR,
        help=f"Root met mined_<klant>__*.jsonl. Default: {_DEFAULT_OBJECTION_DIR}",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=_DEFAULT_PRICES,
        help=f"Pricing table YAML. Default: {_DEFAULT_PRICES}",
    )
    parser.add_argument(
        "--candidate",
        type=str,
        default=None,
        help="Override provider:model voor de kandidaat (default: uit --models-yaml).",
    )
    parser.add_argument(
        "--reference",
        type=str,
        default=str(_REFERENCE_SPEC),
        help=f"Override provider:model voor de referentie. Default: {_REFERENCE_SPEC}",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=_TOP_N_PER_CLIENT,
        help=f"Cap op hard moments per klant. Default: {_TOP_N_PER_CLIENT}",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=_DEFAULT_TIMEOUT_MS,
        help=(
            "LLM request-timeout in ms (kandidaat is een thinking-model). Gaat rechtstreeks "
            "naar LLMClient, negeert config.llm_timeout_ms/.env volledig -- wint dus altijd "
            f"over een live-copilot .env (bv. LLM_TIMEOUT_MS=3000). Default: {_DEFAULT_TIMEOUT_MS}"
        ),
    )
    parser.add_argument(
        "--pace-ms",
        type=int,
        default=DEFAULT_PACE_MS,
        help=(
            "Wachttijd in ms voor elke eval-LLM-call, om quota-druk (bv. Vertex 429's) te "
            f"verminderen. Default: {DEFAULT_PACE_MS} ms."
        ),
    )
    parser.add_argument("--report-md", type=Path, default=_DEFAULT_REPORT_MD, help=f"Default: {_DEFAULT_REPORT_MD}")
    parser.add_argument(
        "--report-json", type=Path, default=_DEFAULT_REPORT_JSON, help=f"Default: {_DEFAULT_REPORT_JSON}"
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Cost-preview only, geen provider aangeroepen (default gedrag).",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Voer de echte (betaalde) evaluatie uit. NIET vanuit deze worktree.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    dry_run = not args.run

    _configure_logging(args.verbose)
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    candidate_spec = (
        parse_model_specs(args.candidate)[0] if args.candidate else _load_candidate_spec(args.models_yaml)
    )
    reference_spec = parse_model_specs(args.reference)[0]

    pricing = PricingTable.from_path(args.prices)

    discovery = discover_hard_moments(args.fireflies_dir, args.objection_dir, args.top_n)
    if not discovery.hard_moments_by_client:
        print(
            "FATAL: geen hard moments gevonden (geen overlap tussen sales-prep.md en "
            f"gemijnde objection-data onder {args.fireflies_dir} / {args.objection_dir}).",
            file=sys.stderr,
        )
        if discovery.skipped_clients:
            for client, reason in sorted(discovery.skipped_clients.items()):
                print(f"  - {client}: {reason}", file=sys.stderr)
        return 1

    if dry_run:
        return _run_dry_run(candidate_spec, reference_spec, pricing, discovery, args)

    return _run_real(candidate_spec, reference_spec, discovery, args)


if __name__ == "__main__":
    raise SystemExit(main())
