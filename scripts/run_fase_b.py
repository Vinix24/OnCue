#!/usr/bin/env python3
"""Fase-B eval runner: grote-N shootout over de echte NL Fireflies-transcripties.

Orchestreert de bestaande eval-harnassen (``scripts/eval_cascade.py`` /
``scripts/eval_summarization.py``, via hun onderliggende modules) over het
vaste fase-A modelpanel (``config/eval_model_register.yaml``):

- Cascade-track: ``data/objection_eval/mined_all.jsonl`` (400 gemijnde
  utterances, geen ground-truth nodig -- karakteriseert routing/escalatie).
- Summarization-track: alle ``fireflies-*.md`` transcripties onder
  ``data/fireflies/``, met uitzondering van eventuele klant-mappen die via
  ``FASE_B_EXCLUDED_CLIENT_DIRS`` zijn geconfigureerd (bv. klanten in een
  andere taal dan de eval-scope; default: geen enkele map uitgesloten).
  De meeste transcripties hebben geen ``action_items.json`` ground truth
  naast zich staan; die lopen in KWALITATIEF-ONLY modus (side-by-side
  modelvoorstellen, geen precision/recall -- scoren tegen een lege
  referentie zou 100% hallucinatie melden, wat misleidend is, geen bevinding).

Standaard draait dit script een **cost dry-run**: modelpanel, per-provider
beschikbaarheid, eval-set groottes en een geschatte bovengrens op de kosten,
ZONDER ook maar 1 provider aan te roepen. Geef ``--run`` om de echte
(betaalde) evaluatie uit te voeren.

Geef ``--profiles`` om in plaats daarvan een thinking-policy x model-matrix te
sweepen (cascade-only, korte objection-utterances zodat dit goedkoop blijft):
elk model uit het panel wordt tegen elk gevraagd thinking-profiel gedraaid
(``no-think``, ``think-512``, ``think-2048``, ``think-8192`` -- zie
``sales_copilot.core.thinking_policy``), en het resultaat is een matrix-
rapport met latency/escalatie/kosten per (model, profiel). Modellen zonder
thinking-concept (GPT-4o-familie) verschijnen één keer, gemarkeerd "n/a".

Voorbeelden:

    # Cost-preview (default, veilig, geen API-calls)
    .venv/bin/python scripts/run_fase_b.py

    # Echte run tegen alle geconfigureerde providers
    .venv/bin/python scripts/run_fase_b.py --run

    # Thinking-sweep cost-preview (default, veilig, geen API-calls)
    .venv/bin/python scripts/run_fase_b.py --profiles no-think,think-512,think-2048,think-8192

    # Echte thinking-sweep (betaald -- draai dit in het hoofd-checkout met een
    # royale LLM_TIMEOUT_MS, niet vanuit een dispatch-worktree)
    .venv/bin/python scripts/run_fase_b.py --profiles no-think,think-512,think-2048,think-8192 --run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from sales_copilot.core.config import DetectorConfig, load_env, with_overrides  # noqa: E402
from sales_copilot.core.thinking_policy import (  # noqa: E402
    THINKING_PROFILES,
    ThinkingPolicy,
    thinking_applicable,
)
from sales_copilot.modules.detector import cascade_eval, summarization_eval  # noqa: E402
from sales_copilot.modules.detector.debouncer import PainPointDebouncer  # noqa: E402
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    DEFAULT_PACE_MS,
    ModelSpec,
    PricingTable,
    is_provider_available,
    load_model_register,
    resilience_header_lines,
)
from sales_copilot.modules.detector.router import PainPointRouter  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_REGISTER = REPO_ROOT / "config" / "eval_model_register.yaml"
_DEFAULT_MINED_SET = REPO_ROOT / "data" / "objection_eval" / "mined_all.jsonl"
_DEFAULT_FIREFLIES_DIR = REPO_ROOT / "data" / "fireflies"
_DEFAULT_PRICES = REPO_ROOT / "config" / "eval_model_prices.yaml"
_DEFAULT_REPORT_MD = REPO_ROOT / "claudedocs" / "2026-07-16-fase-b-results.md"
_DEFAULT_REPORT_JSON = REPO_ROOT / "claudedocs" / "2026-07-16-fase-b-results.json"
_DEFAULT_CASCADE_NDJSON = REPO_ROOT / "data" / "cascade_eval" / "fase_b_run.ndjson"
_DEFAULT_SUMMARY_NDJSON = REPO_ROOT / "data" / "summarization_eval" / "fase_b_run.ndjson"
_DEFAULT_SWEEP_REPORT_MD = REPO_ROOT / "claudedocs" / "2026-07-17-fase-b-thinking-sweep.md"
_DEFAULT_SWEEP_REPORT_JSON = REPO_ROOT / "claudedocs" / "2026-07-17-fase-b-thinking-sweep.json"
_DEFAULT_SWEEP_CASCADE_NDJSON = REPO_ROOT / "data" / "cascade_eval" / "fase_b_thinking_sweep.ndjson"

_EXCLUDED_CLIENT_DIRS_ENV = "FASE_B_EXCLUDED_CLIENT_DIRS"

# Ruwe cost-estimation heuristiek voor de dry-run: ~4 tekens/token (gangbare
# vuistregel voor Latijns schrift, ook bruikbaar voor Nederlands). Dit is een
# pre-flight BOVENGRENS-schatting, geen precieze voorspelling -- de cascade-
# schatting neemt aan dat ELKE utterance escaleert naar de LLM-confirm-laag,
# omdat de echte fast-path coverage pas bekend is na een echte pipeline-run.
_CHARS_PER_TOKEN = 4
_CASCADE_SYSTEM_PROMPT_TOKENS = 250
_CASCADE_OUTPUT_TOKENS = 80
_SUMMARY_SYSTEM_PROMPT_TOKENS = 220
_SUMMARY_OUTPUT_TOKENS_PER_ITEM = 60
_SUMMARY_ESTIMATED_ITEMS_PER_TRANSCRIPT = 5


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _estimate_tokens(chars: int) -> int:
    return max(1, chars // _CHARS_PER_TOKEN)


def _resolve_detector_config(timeout_ms_override: int | None) -> DetectorConfig:
    """Build the eval ``DetectorConfig``, with an optional ``--timeout-ms`` override.

    ``main()`` calls ``load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)`` before
    ``DetectorConfig.from_env()`` runs, so a live-copilot ``.env`` (typically ``LLM_TIMEOUT_MS
    =3000``) always wins over the invoking shell's env var. The override here applies AFTER
    ``from_env()`` builds the config, so ``--timeout-ms`` wins over both the shell env and
    ``.env`` regardless of that load order.
    """
    config = DetectorConfig.from_env()
    if timeout_ms_override is not None:
        config = with_overrides(config, llm_timeout_ms=timeout_ms_override)
    return config


# ---------------------------------------------------------------------------
# Fireflies discovery
#
# ``load_transcript_records`` in directory mode recurses for
# ``transcript_ours.txt`` (our own local-whisper track), not the real
# Fireflies exports. The real NL corpus lives in ``fireflies-*.md`` files, of
# which only some klanten also have a ``transcript_ours.txt`` sibling. So this
# module discovers ``fireflies-*.md`` files itself and reuses
# ``load_transcript_records`` in its *single-file* mode per match -- the
# loader function itself is not reimplemented, only invoked once per file
# instead of once per directory.
# ---------------------------------------------------------------------------


def _excluded_client_dirs() -> frozenset[str]:
    """Client directory names to exclude from fase-B discovery.

    Configured via the ``FASE_B_EXCLUDED_CLIENT_DIRS`` env var: a
    comma-separated list of top-level client directory names under
    ``--fireflies-dir`` (e.g. ``FASE_B_EXCLUDED_CLIENT_DIRS=client-x,client-y``
    in a local ``.env``, never committed). Defaults to empty -- without this
    var set, fase-B evaluates every ``fireflies-*.md`` transcript found. Use
    this to exclude e.g. a client whose calls are in a different language than
    the eval scope, without hardcoding any client name in the script itself.
    """
    raw = os.environ.get(_EXCLUDED_CLIENT_DIRS_ENV, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def discover_fireflies_transcripts(root: Path) -> list[Path]:
    """Find every ``fireflies-*.md`` transcript under ``root``, excluding configured client dirs.

    See ``_excluded_client_dirs`` / ``FASE_B_EXCLUDED_CLIENT_DIRS`` for how to
    exclude specific client directories structurally rather than via a runtime
    filter.
    """
    if not root.exists():
        return []
    excluded = _excluded_client_dirs()
    if not excluded:
        return sorted(root.rglob("fireflies-*.md"))
    return sorted(
        p for p in root.rglob("fireflies-*.md") if excluded.isdisjoint(p.relative_to(root).parts[:-1])
    )


def count_excluded_transcripts(root: Path) -> int:
    """Count transcripts under the configured-excluded client dirs (for dry-run reporting)."""
    count = 0
    for name in _excluded_client_dirs():
        client_dir = root / name
        if client_dir.exists():
            count += len(list(client_dir.rglob("fireflies-*.md")))
    return count


def load_fireflies_records(paths: list[Path], root: Path) -> list[dict[str, Any]]:
    """Load one transcript record per fireflies-*.md file.

    ``load_transcript_records`` runs unmodified in single-file mode (so
    ground-truth lookup and transcript-line splitting stay identical to the
    existing harness); only the ``source`` label is rewritten to
    ``<klant>__<stem>`` so klanten with more than one transcript stay
    disambiguated in the consolidated report, mirroring the naming
    convention already used by ``data/objection_eval/mined_*.jsonl``.
    """
    records: list[dict[str, Any]] = []
    for path in paths:
        klant = path.relative_to(root).parts[0]
        (record,) = summarization_eval.load_transcript_records(path)
        record["source"] = f"{klant}__{path.stem}"
        records.append(record)
    return records


def _load_cascade_records(path: Path) -> list[dict[str, str]] | None:
    if not path.exists():
        print(
            f"FATAL: cascade eval-set niet gevonden: {path}\n"
            "Genereer er een met scripts/mine_objection_eval_set.py --seed, of geef "
            "--eval-set op.",
            file=sys.stderr,
        )
        return None
    records = cascade_eval.load_eval_records(path)
    if not records:
        print(f"FATAL: geen bruikbare utterances in {path}", file=sys.stderr)
        return None
    return records


# ---------------------------------------------------------------------------
# Thinking-policy sweep: profile parsing + (model, profile) cell enumeration
#
# The sweep stays cheap because the cascade eval-set is short objection
# utterances, not full transcripts -- so it is the only track swept per
# profile (see _run_profile_sweep_dry_run / _run_profile_sweep_real). Models
# with no thinking concept (openai/gpt-4o, openai/gpt-4o-mini) collapse to a
# single "n/a" cell instead of one identical no-op row per requested profile.
# ---------------------------------------------------------------------------

_THINKING_NA_LABEL = "n/a (geen thinking-model)"


def _parse_profiles(raw: str | None) -> list[ThinkingPolicy] | None:
    """Parse a comma-separated list of thinking-profile names, or ``None`` if unset."""
    if not raw or not raw.strip():
        return None
    profiles: list[ThinkingPolicy] = []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        policy = THINKING_PROFILES.get(name)
        if policy is None:
            valid = ", ".join(sorted(THINKING_PROFILES))
            raise ValueError(f"onbekend profiel {name!r}. Geldige profielen: {valid}")
        profiles.append(policy)
    if not profiles:
        return None
    return profiles


def _sweep_matrix_cells(
    specs: list[ModelSpec], profiles: list[ThinkingPolicy]
) -> list[tuple[ModelSpec, ThinkingPolicy | None]]:
    """Enumerate (model, profile) sweep cells.

    Non-thinking providers (openai/azure) collapse to a single cell with
    ``profile=None`` regardless of how many profiles were requested.
    """
    cells: list[tuple[ModelSpec, ThinkingPolicy | None]] = []
    for spec in specs:
        if not thinking_applicable(spec.provider):
            cells.append((spec, None))
            continue
        for profile in profiles:
            cells.append((spec, profile))
    return cells


def _profile_extra_output_tokens(provider: str, profile: ThinkingPolicy | None, calls: int) -> int:
    """Upper-bound extra output tokens a thinking profile adds, for cost estimation.

    Mirrors the dry-run's existing worst-case philosophy: assumes each call spends its
    entire reasoning_budget (the real trace is usually shorter). Ollama's budget maps
    onto num_ctx (a context-window setting), not billed output tokens, so it contributes
    nothing here.
    """
    if profile is None or not profile.thinking_on or profile.reasoning_budget is None:
        return 0
    if provider.strip().lower() == "ollama":
        return 0
    return profile.reasoning_budget * calls


# ---------------------------------------------------------------------------
# Dry-run: cost preview, geen enkele provider aangeroepen
# ---------------------------------------------------------------------------


def _run_dry_run(
    specs: list[ModelSpec],
    pricing: PricingTable,
    cascade_records: list[dict[str, str]],
    summarization_records: list[dict[str, Any]],
    excluded_count: int,
    args: argparse.Namespace,
) -> int:
    lines: list[str] = ["# Fase-B cost dry-run (GEEN provider aangeroepen)", ""]

    lines.append("## Model-panel")
    lines.append("")
    availability: dict[str, tuple[bool, str]] = {}
    for spec in specs:
        avail, reason = is_provider_available(spec.provider)
        availability[str(spec)] = (avail, reason)
        marker = "beschikbaar" if avail else "NIET beschikbaar"
        suffix = f" -- {spec.description}" if spec.description else ""
        lines.append(f"- `{spec}` -- {marker} ({reason}){suffix}")
    lines.append("")

    cascade_sources = {rec["source"] for rec in cascade_records}
    lines.append("## Eval-set groottes")
    lines.append("")
    lines.append(
        f"- Cascade (`{args.eval_set}`): {len(cascade_records)} utterances over {len(cascade_sources)} bronnen"
    )
    lines.append(
        f"- Summarization (`{args.fireflies_dir}`): {len(summarization_records)} NL-transcripties "
        f"({excluded_count} uitgesloten via {_EXCLUDED_CLIENT_DIRS_ENV})"
    )
    lines.append("")

    cascade_chars = sum(len(rec["text"]) for rec in cascade_records)
    cascade_calls = len(cascade_records)
    cascade_input = cascade_calls * _CASCADE_SYSTEM_PROMPT_TOKENS + _estimate_tokens(cascade_chars)
    cascade_output = cascade_calls * _CASCADE_OUTPUT_TOKENS

    summary_chars = sum(len(rec["transcript"]) for rec in summarization_records)
    summary_calls = len(summarization_records)
    summary_input = summary_calls * _SUMMARY_SYSTEM_PROMPT_TOKENS + _estimate_tokens(summary_chars)
    summary_output = summary_calls * _SUMMARY_ESTIMATED_ITEMS_PER_TRANSCRIPT * _SUMMARY_OUTPUT_TOKENS_PER_ITEM

    lines.append("## Geschatte kosten (bovengrens)")
    lines.append("")
    lines.append(
        "Aanname: elke cascade-utterance escaleert naar de LLM-confirm-laag (worst case -- "
        "de echte fast-path coverage ligt hoger; zie het cascade-rapport na een echte run). "
        "~4 tekens/token heuristiek."
    )
    lines.append("")
    lines.append("| Model | Track | Input tokens | Output tokens | Geschatte kosten |")
    lines.append("|---|---|---|---|---|")
    grand_total = 0.0
    any_unknown = False
    for spec in specs:
        for track_name, in_tok, out_tok in (
            ("cascade", cascade_input, cascade_output),
            ("summarization", summary_input, summary_output),
        ):
            cost = pricing.estimate_cost(spec.provider, spec.model, in_tok, out_tok)
            if cost is None:
                any_unknown = True
                cost_str = "onbekend (geen prijs-entry)"
            else:
                grand_total += cost
                cost_str = f"${cost:.4f}"
            lines.append(f"| {spec} | {track_name} | {in_tok} | {out_tok} | {cost_str} |")
    lines.append("")
    incomplete_note = " (onvolledig -- niet elk model heeft een prijs-entry)" if any_unknown else ""
    lines.append(f"**Geschatte totaalkosten over het volledige panel: ${grand_total:.4f}**{incomplete_note}")
    lines.append("")
    lines.append("Dit was een DRY-RUN: er is geen enkele provider aangeroepen. Geef `--run` voor de echte evaluatie.")

    report = "\n".join(lines) + "\n"
    print(report)
    return 0


def _run_profile_sweep_dry_run(
    specs: list[ModelSpec],
    pricing: PricingTable,
    cascade_records: list[dict[str, str]],
    profiles: list[ThinkingPolicy],
    args: argparse.Namespace,
) -> int:
    """Cost-preview for the thinking-policy sweep. Calls NO provider.

    Cascade-only: the sweep's inputs are short objection utterances, so this stays cheap
    even at N profiles x N models -- the summarization track (full transcripts) is not
    part of the sweep.
    """
    lines: list[str] = ["# Fase-B thinking-sweep cost dry-run (GEEN provider aangeroepen)", ""]
    lines.append(f"**Profielen:** {', '.join(p.name for p in profiles)}")
    lines.append("")

    cascade_sources = {rec["source"] for rec in cascade_records}
    lines.append(
        f"**Cascade eval-set:** `{args.eval_set}` -- {len(cascade_records)} utterances over "
        f"{len(cascade_sources)} bronnen"
    )
    lines.append("")

    cascade_chars = sum(len(rec["text"]) for rec in cascade_records)
    cascade_calls = len(cascade_records)
    base_input = cascade_calls * _CASCADE_SYSTEM_PROMPT_TOKENS + _estimate_tokens(cascade_chars)
    base_output = cascade_calls * _CASCADE_OUTPUT_TOKENS

    lines.append("## Geschatte kosten (bovengrens) per (model, profiel)")
    lines.append("")
    lines.append(
        "Aanname: elke utterance escaleert naar de LLM-confirm-laag (worst case), en elk "
        "think-profiel verbruikt zijn volledige reasoning_budget als extra output-tokens "
        "(bovengrens -- de echte reasoning-lengte ligt meestal lager). Ollama's budget "
        "vertaalt naar num_ctx, geen extra billed output. ~4 tekens/token heuristiek."
    )
    lines.append("")
    lines.append("| Model | Profiel | Input tokens | Output tokens | Geschatte kosten |")
    lines.append("|---|---|---|---|---|")

    grand_total = 0.0
    any_unknown = False
    for spec, profile in _sweep_matrix_cells(specs, profiles):
        profile_label = profile.name if profile is not None else _THINKING_NA_LABEL
        out_tok = base_output + _profile_extra_output_tokens(spec.provider, profile, cascade_calls)
        cost = pricing.estimate_cost(spec.provider, spec.model, base_input, out_tok)
        if cost is None:
            any_unknown = True
            cost_str = "onbekend (geen prijs-entry)"
        else:
            grand_total += cost
            cost_str = f"${cost:.4f}"
        lines.append(f"| {spec} | {profile_label} | {base_input} | {out_tok} | {cost_str} |")
    lines.append("")
    incomplete_note = " (onvolledig -- niet elk model heeft een prijs-entry)" if any_unknown else ""
    lines.append(f"**Geschatte totaalkosten over de volledige sweep: ${grand_total:.4f}**{incomplete_note}")
    lines.append("")
    lines.append(
        "Dit was een DRY-RUN: er is geen enkele provider aangeroepen. Geef `--run` samen met "
        "`--profiles` voor de echte sweep."
    )

    report = "\n".join(lines) + "\n"
    print(report)
    return 0


# ---------------------------------------------------------------------------
# Echte run
# ---------------------------------------------------------------------------


def _run_cascade_track(
    specs: list[ModelSpec],
    config: DetectorConfig,
    records: list[dict[str, str]],
    pricing: PricingTable,
    *,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[list[dict[str, Any]], list[Any]]:
    router = PainPointRouter(config)
    debouncer = PainPointDebouncer(cooldown_seconds=0)
    hooks = cascade_eval.CascadeEvalHooks()

    reports: list[dict[str, Any]] = []
    all_records: list[Any] = []
    for spec in specs:
        logger.info("Cascade-track: evalueren van model %s", spec)
        pipeline, effective_spec, available, reason = cascade_eval.build_pipeline_for_spec(
            config, router, debouncer, spec, hooks, pace_ms=pace_ms
        )
        records_out = cascade_eval.run_cascade_on_records(pipeline, records, config, effective_spec, hooks)
        report = cascade_eval.build_per_model_report(records_out, config, pricing, spec, available, reason)
        reports.append(report)
        all_records.extend(records_out)
    return reports, all_records


def _run_profile_sweep_real(
    specs: list[ModelSpec],
    config: DetectorConfig,
    cascade_records: list[dict[str, str]],
    pricing: PricingTable,
    profiles: list[ThinkingPolicy],
    *,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Run the cascade over every (model, profile) sweep cell and record latency/escalation/cost.

    Cascade-only (see module docstring on ``_sweep_matrix_cells``): the sweep's inputs are
    short objection utterances, which keeps N profiles x N models cheap.
    """
    router = PainPointRouter(config)
    debouncer = PainPointDebouncer(cooldown_seconds=0)

    rows: list[dict[str, Any]] = []
    all_records: list[Any] = []
    for spec, profile in _sweep_matrix_cells(specs, profiles):
        profile_label = profile.name if profile is not None else _THINKING_NA_LABEL
        logger.info("Thinking-sweep: model=%s profiel=%s", spec, profile_label)
        hooks = cascade_eval.CascadeEvalHooks()
        pipeline, effective_spec, available, reason = cascade_eval.build_pipeline_for_spec(
            config, router, debouncer, spec, hooks, thinking=profile, pace_ms=pace_ms
        )
        records_out = cascade_eval.run_cascade_on_records(pipeline, cascade_records, config, effective_spec, hooks)
        report = cascade_eval.build_per_model_report(records_out, config, pricing, spec, available, reason)
        report["thinking_profile"] = profile_label
        report["thinking_on"] = profile.thinking_on if profile is not None else False
        report["reasoning_budget"] = profile.reasoning_budget if profile is not None else None
        rows.append(report)
        all_records.extend(records_out)
    return rows, all_records


def _format_thinking_sweep_markdown(
    rows: list[dict[str, Any]],
    eval_set_path: Path,
    config: DetectorConfig,
    *,
    pace_ms: int = DEFAULT_PACE_MS,
) -> str:
    lines: list[str] = [
        "# Fase-B thinking-sweep resultaten",
        "",
        f"**Eval-set:** {eval_set_path}",
        *resilience_header_lines(effective_timeout_ms=config.llm_timeout_ms, pace_ms=pace_ms),
        "Thinking-profielen met een groot reasoning_budget (bv. think-8192) kunnen bij een "
        "laag effectief timeout-budget alsnog timeouten -- geef `--timeout-ms` een royale "
        "waarde voor een echte sweep-run.",
        "",
        "| Model | Profiel | Beschikbaar | Escalatie-rate | LLM-confirm p50/p95 | Geschatte kosten |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        avail = "ja" if row["available"] else "nee"
        llm_lat = row["llm_latency_ms"]
        lat_str = f"{llm_lat['p50']:.0f}/{llm_lat['p95']:.0f} ms" if llm_lat.get("n") else "n/a"
        esc = f"{row['escalation_rate'] * 100:.1f}%"
        cost = f"${row['estimated_cost_usd']:.6f}" if row["estimated_cost_usd"] is not None else "n/a"
        lines.append(f"| {row['model']} | {row['thinking_profile']} | {avail} | {esc} | {lat_str} | {cost} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def _run_summarization_track(
    specs: list[ModelSpec],
    config: DetectorConfig,
    records: list[dict[str, Any]],
    pricing: PricingTable,
    *,
    pace_ms: int = DEFAULT_PACE_MS,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[Any]]:
    """Run every model spec over the fireflies transcripts.

    Returns ``(scored_reports, qualitative_by_source, all_ndjson_records)``.
    ``scored_reports`` only covers transcripts that DO have ground truth
    (an ``action_items.json`` sibling); it is empty for the current real
    corpus (none have one yet). Transcripts without ground truth end up in
    ``qualitative_by_source`` instead: a side-by-side comparison of predicted
    action items per model, with no fabricated precision/recall score.
    """
    reports: list[dict[str, Any]] = []
    qualitative_by_source: dict[str, dict[str, Any]] = {}
    all_records: list[Any] = []

    for spec in specs:
        logger.info("Summarization-track: evalueren van model %s", spec)
        out, available, reason = summarization_eval.run_summarization_eval(
            records, config, spec, pace_ms=pace_ms
        )
        all_records.extend(out)

        with_ground_truth = [rec for rec in out if rec.reference]
        without_ground_truth = [rec for rec in out if not rec.reference]

        if with_ground_truth:
            report = summarization_eval.build_per_model_report(with_ground_truth, pricing, spec, available, reason)
            report["scored_transcripts"] = len(with_ground_truth)
            report["unscored_transcripts"] = len(without_ground_truth)
            reports.append(report)

        for rec in without_ground_truth:
            bucket = qualitative_by_source.setdefault(rec.source, {})
            bucket[str(spec)] = {
                "available": available,
                "error": rec.error,
                "predicted": rec.predicted,
                "item_count": len(rec.predicted) if rec.predicted else 0,
            }

    return reports, qualitative_by_source, all_records


def _format_qualitative_section(qualitative_by_source: dict[str, dict[str, Any]], model_order: list[str]) -> str:
    if not qualitative_by_source:
        return ""
    lines = [
        "## Summarization -- kwalitatief (geen ground truth)",
        "",
        "Onderstaande transcripties hebben geen `action_items.json` naast zich staan. Er is dus "
        "geen ground truth om precision/recall tegen te scoren -- de modelvoorstellen staan "
        "side-by-side ter beoordeling, zonder verzonnen score.",
        "",
    ]
    for source in sorted(qualitative_by_source):
        lines.append(f"### {source}")
        lines.append("")
        lines.append("| Model | Actiepunten | Voorbeeld |")
        lines.append("|---|---|---|")
        per_model = qualitative_by_source[source]
        for model in model_order:
            entry = per_model.get(model)
            if entry is None:
                continue
            if not entry["available"]:
                lines.append(f"| {model} | n/a | provider niet beschikbaar |")
                continue
            if entry["error"]:
                lines.append(f"| {model} | n/a | fout: {entry['error']} |")
                continue
            example = entry["predicted"][0].get("description", "") if entry["predicted"] else ""
            lines.append(f"| {model} | {entry['item_count']} | {example} |")
        lines.append("")
    return "\n".join(lines)


def _run_real(
    specs: list[ModelSpec],
    pricing: PricingTable,
    cascade_records: list[dict[str, str]],
    summarization_records: list[dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    config = _resolve_detector_config(args.timeout_ms)

    cascade_reports, cascade_all_records = _run_cascade_track(
        specs, config, cascade_records, pricing, pace_ms=args.pace_ms
    )
    cascade_eval.records_to_ndjson(cascade_all_records, args.cascade_output)
    cascade_md = cascade_eval.format_markdown_report(cascade_reports, args.eval_set, config)

    scored_reports, qualitative_by_source, summary_all_records = _run_summarization_track(
        specs, config, summarization_records, pricing, pace_ms=args.pace_ms
    )
    summarization_eval.records_to_ndjson(summary_all_records, args.summarization_output)

    if not summarization_records:
        summary_md = f"# Samenvattings-evaluatie\n\nGeen fireflies-transcripties gevonden onder {args.fireflies_dir}.\n"
    else:
        parts = []
        if scored_reports:
            parts.append(summarization_eval.format_markdown_report(scored_reports, args.fireflies_dir))
        qualitative_md = _format_qualitative_section(qualitative_by_source, [str(s) for s in specs])
        if qualitative_md:
            parts.append(qualitative_md)
        summary_md = "\n".join(parts)

    header_lines = resilience_header_lines(
        effective_timeout_ms=config.llm_timeout_ms,
        pace_ms=args.pace_ms,
        thinking_output_tokens=summarization_eval.OUTPUT_TOKENS_FLOOR,
    )
    consolidated = (
        "# Fase-B evaluatie-resultaten\n\n"
        f"**Model-panel:** {', '.join(str(s) for s in specs)}\n\n"
        + "\n".join(header_lines)
        + "\n\n---\n\n"
        + cascade_md
        + "\n---\n\n"
        + summary_md
    )

    args.report_md.parent.mkdir(parents=True, exist_ok=True)
    args.report_md.write_text(consolidated, encoding="utf-8")

    payload = {
        "mode": "run",
        "models": [str(s) for s in specs],
        "cascade": cascade_reports,
        "summarization_scored": scored_reports,
        "summarization_qualitative": qualitative_by_source,
    }
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(consolidated)
    print(f"\nConsolidated report: {args.report_md}")
    print(f"Machine-readable JSON: {args.report_json}")
    return 0


def _run_profile_sweep(
    specs: list[ModelSpec],
    pricing: PricingTable,
    cascade_records: list[dict[str, str]],
    profiles: list[ThinkingPolicy],
    args: argparse.Namespace,
) -> int:
    """Real thinking-policy sweep: calls every configured provider, writes the matrix report."""
    config = _resolve_detector_config(args.timeout_ms)

    rows, all_records = _run_profile_sweep_real(
        specs, config, cascade_records, pricing, profiles, pace_ms=args.pace_ms
    )
    cascade_eval.records_to_ndjson(all_records, args.sweep_cascade_output)

    md = _format_thinking_sweep_markdown(rows, args.eval_set, config, pace_ms=args.pace_ms)
    args.sweep_report_md.parent.mkdir(parents=True, exist_ok=True)
    args.sweep_report_md.write_text(md, encoding="utf-8")

    payload = {
        "mode": "profile-sweep",
        "profiles": [p.name for p in profiles],
        "models": [str(s) for s in specs],
        "rows": rows,
    }
    args.sweep_report_json.parent.mkdir(parents=True, exist_ok=True)
    args.sweep_report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(md)
    print(f"\nThinking-sweep report: {args.sweep_report_md}")
    print(f"Machine-readable JSON: {args.sweep_report_json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models-yaml",
        type=Path,
        default=_DEFAULT_REGISTER,
        help=f"YAML model register. Default: {_DEFAULT_REGISTER}",
    )
    parser.add_argument(
        "--eval-set",
        type=Path,
        default=_DEFAULT_MINED_SET,
        help=f"Cascade eval-set (gemijnde objection JSONL). Default: {_DEFAULT_MINED_SET}",
    )
    parser.add_argument(
        "--fireflies-dir",
        type=Path,
        default=_DEFAULT_FIREFLIES_DIR,
        help=(
            f"Root met fireflies-*.md transcripties (client-mappen uit "
            f"{_EXCLUDED_CLIENT_DIRS_ENV} uitgesloten). Default: {_DEFAULT_FIREFLIES_DIR}"
        ),
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=_DEFAULT_PRICES,
        help=f"Pricing table YAML. Default: {_DEFAULT_PRICES}",
    )
    parser.add_argument("--report-md", type=Path, default=_DEFAULT_REPORT_MD, help=f"Default: {_DEFAULT_REPORT_MD}")
    parser.add_argument(
        "--report-json", type=Path, default=_DEFAULT_REPORT_JSON, help=f"Default: {_DEFAULT_REPORT_JSON}"
    )
    parser.add_argument("--cascade-output", type=Path, default=_DEFAULT_CASCADE_NDJSON)
    parser.add_argument("--summarization-output", type=Path, default=_DEFAULT_SUMMARY_NDJSON)
    parser.add_argument(
        "--profiles",
        type=str,
        default=None,
        help=(
            "Comma-separated thinking-profielen om te sweepen over het modelpanel x profiel-matrix "
            f"(kies uit: {', '.join(sorted(THINKING_PROFILES))}). Cascade-only (korte objection-"
            "utterances), zodat de sweep goedkoop blijft. Zonder deze vlag draait run_fase_b.py "
            "zoals voorheen: één run per model, geen thinking-policy toegepast."
        ),
    )
    parser.add_argument(
        "--sweep-report-md", type=Path, default=_DEFAULT_SWEEP_REPORT_MD, help=f"Default: {_DEFAULT_SWEEP_REPORT_MD}"
    )
    parser.add_argument(
        "--sweep-report-json",
        type=Path,
        default=_DEFAULT_SWEEP_REPORT_JSON,
        help=f"Default: {_DEFAULT_SWEEP_REPORT_JSON}",
    )
    parser.add_argument("--sweep-cascade-output", type=Path, default=_DEFAULT_SWEEP_CASCADE_NDJSON)
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=None,
        help=(
            "Override LLM_TIMEOUT_MS voor deze eval-run, wint over .env (dat via "
            "load_dotenv(override=True) anders altijd zou winnen). Zonder deze vlag: "
            "config.llm_timeout_ms zoals geladen via .env/shell env (default gedrag)."
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

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Cost-preview only, geen provider aangeroepen (default gedrag).",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Voer de echte (betaalde) evaluatie uit tegen elke geconfigureerde provider.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args(argv)

    dry_run = not args.run

    try:
        profiles = _parse_profiles(args.profiles)
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    _configure_logging(args.verbose)
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    if not args.models_yaml.exists():
        print(f"FATAL: model-register niet gevonden: {args.models_yaml}", file=sys.stderr)
        return 1
    specs = load_model_register(args.models_yaml)
    if not specs:
        print(f"FATAL: geen modellen gedefinieerd in {args.models_yaml}", file=sys.stderr)
        return 1

    pricing = PricingTable.from_path(args.prices)

    cascade_records = _load_cascade_records(args.eval_set)
    if cascade_records is None:
        return 1

    if profiles is not None:
        if dry_run:
            return _run_profile_sweep_dry_run(specs, pricing, cascade_records, profiles, args)
        return _run_profile_sweep(specs, pricing, cascade_records, profiles, args)

    fireflies_paths = discover_fireflies_transcripts(args.fireflies_dir)
    excluded_count = count_excluded_transcripts(args.fireflies_dir)
    if fireflies_paths:
        summarization_records = load_fireflies_records(fireflies_paths, args.fireflies_dir)
    else:
        logger.warning("Geen fireflies-*.md transcripties gevonden onder %s", args.fireflies_dir)
        summarization_records = []

    if dry_run:
        return _run_dry_run(specs, pricing, cascade_records, summarization_records, excluded_count, args)

    return _run_real(specs, pricing, cascade_records, summarization_records, args)


if __name__ == "__main__":
    raise SystemExit(main())
