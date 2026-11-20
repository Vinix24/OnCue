# ADR-009: Which LLM tasks may run on which privacy profile

Status: Proposed

Date: 2026-11-20

## Context

A conversation carries one of three privacy profiles, strictest first: `local`, `tenant` and `public` (`PRIVACY_PROFILES` in `src/sales_copilot/core/privacy_gate.py`). The profile is a ceiling on where text may go. `tier_of()` in `src/sales_copilot/core/outbound_policy.py` classifies a provider: `local` for Ollama on a loopback address and for `none`, `tenant` for azure or vertex only when `TRUST_OWN_TENANT` is set, `public` for everything else, an azure or vertex without that flag and a remote Ollama included. `privacy_allows()` accepts a provider when its tier ranks at or below the ceiling. No ceiling (`None`) accepts every provider.

Every LLM call already goes through one resolver. `resolve_llm(task, ...)` in `src/sales_copilot/core/llm_routing.py` picks provider, model, timeout, output limit and the OpenRouter fallback models (`<TASK>_LLM_FALLBACK_MODELS`, PR #410) per task, and calls `enforce_privacy` on the result. `LLMClient` (`src/sales_copilot/core/llm_client.py`) checks the same ceiling again before every call. At start-call, `_enforce_task_ceiling` in `src/sales_copilot/websocket/hub_core.py` runs `resolve_llm` for each task in `active_tasks()` and refuses the call (a `PrivacyGateError`, HTTP 400) when one task's provider is above the ceiling. A caller-supplied `privacy` is validated first in `extract_start_call_config` (`hub_core.py` around line 440, via `validate_privacy`), and then `_apply_klant_config` lets the client's `klant.yaml` set `privacy`, overwriting it.

So the ceiling exists and is enforced per task. What does not exist is a decision about the **task set**: which tasks are meant to run at all on a given profile. Today the answer is implicit. Each task has its own provider variable, and the ceiling only says whether that provider is allowed. A user who picks `local` and has no local model gets a 400 for the first task whose provider is not local, or, with `LLM_PROVIDER=none`, a conversation where all LLM tasks are silently off. Criterion 5 of the track `llm-routering-per-taak` needs the rules written down before it is built.

The product line stays as in `CLAUDE.md` and `docs/ARCHITECTURE_BOUNDARIES.md`: local is the default, cloud is chosen per conversation and never per customer, and cloud never replaces the local default.

### The tasks today

`TASKS` in `llm_routing.py` has ten entries. "Live" means it runs during the call and a person waits on the answer. `active_tasks()` says when each one runs. Text is always passed through `apply_outbound_pii` in `LLMClient` before it is sent (`PII_REDACTION`, default `cloud_only`: redacted before public providers, raw for local and trusted tenant).

| Task | Call site | What it sends | How much | When |
|---|---|---|---|---|
| `detector_confirm` | `modules/detector/llm_confirm.py` | One uncertain prospect fragment plus the category list from `config/pain_points.yaml` and context documents | One fragment | Live. Always active |
| `window_classifier` | `modules/detector/window_classifier.py` | A sliding window of the latest utterances, prospect only by default (`ONLY_CLASSIFY_PROSPECT`) | A few utterances | Live. Always active |
| `phase` | `modules/detector/phase_detector.py` | Recent transcript lines to pick discovery, pitch or closing | A few lines | Live. Only with `AUTO_PHASE_DETECTION` (default off) |
| `suggestions` | `modules/detector/suggestions.py` | Prospect utterances (at most 8 lines or 1,800 characters of context) plus the uploaded context and prep documents | A short window plus documents | Live. `ENABLE_SUGGESTIONS` (default on) |
| `slides` | `modules/copilot/slide_generator.py` | A pain point category name in a fixed prompt | One word, no transcript | Live. Always active |
| `script_tracking` | `modules/coaching/script_tracker.py` | The script points and the transcript lines checked against them | A window of lines | Live. `ENABLE_SCRIPT_TRACKING`, plus the license feature `FEATURE_SCRIPT_TRACKING_LIVE` |
| `summary` | `modules/detector/summary.py` | The last 40 lines at most (4,000 characters), with speaker labels, every 60 seconds | A rolling window | Near-live. Runs off the 5-second path with a 30 s timeout. `ENABLE_SUMMARY` (default on) |
| `insight` | `modules/insight/engine.py` | The full running transcript and prep documents | The whole conversation, repeatedly | Minutes-latency, during the call. `INSIGHT_ENABLED` plus the deep-insights entitlement (Pro). Its own variables: `INSIGHT_PROVIDER`, `INSIGHT_MODEL` |
| `report` | `modules/reports/enrichment.py` | The whole numbered transcript with speaker labels | The whole conversation, once | After the call, 300 s timeout. Active when the reports module runs |
| `report_terms` | `modules/reports/enrichment.py` | The same transcript with candidate terms under the segments that have candidates | The whole conversation, once | After the call. Inherits the `report` route (`_TASK_PARENT`); one call with `report` when the route is the same |

Two other LLM call sites exist and are not tasks. `LLMClient(spec.provider, ...)` in `modules/detector/card_selection_eval.py` and `modules/detector/summarization_eval.py` are offline evaluation tools that run on fixtures, never on a live conversation, and `llm_routing.py` does not know them. `_build_provider_client` in `src/sales_copilot/__main__.py` builds a client only to check that a provider initialises. Neither sends conversation text.

Which detector paths never use an LLM: the embedding-based pain point and objection detection and talk-time coaching run locally in every profile. They are not part of this decision.

## Decision

1. **A task has a minimum sensitivity class, and the profile decides which classes may run.** The task list is grouped by how much of the conversation it sends:
   - **Fragment tasks** send one fragment or a short window, or no transcript at all: `detector_confirm`, `window_classifier`, `phase`, `suggestions`, `slides`, `script_tracking`.
   - **Rolling task**: `summary` (up to 40 lines a minute, the transcript repeated in overlapping windows).
   - **Whole-conversation tasks** send the full transcript: `report`, `report_terms`, `insight`.

2. **Matrix.** "On" means the task runs when its existing switch is on. "Off" means it is not started for that profile unless the user names a provider for it that satisfies the ceiling. A cell never allows a provider above the profile: the ceiling in `privacy_gate.py` stays the hard check and this matrix only decides what is started by default.

   | Task | `local` profile | `tenant` profile | `public` profile |
   |---|---|---|---|
   | `detector_confirm`, `window_classifier`, `slides` | On, local provider | On, local or tenant provider | On, any provider |
   | `phase`, `script_tracking` | On, local provider (when their switch is on) | On, local or tenant provider | On, any provider |
   | `suggestions` | On, local provider | On, local or tenant provider | On, any provider |
   | `summary` | On, local provider | On, local or tenant provider | **Off by default**; the user opts in |
   | `report`, `report_terms` | On, local provider | On, local or tenant provider | **Off by default**; the user opts in |
   | `insight` (Pro) | **Off.** Not started with a local provider; see point 3 | On when `INSIGHT_ENABLED` and entitled, tenant provider | **Off by default**; the user opts in per conversation |

   The `public` profile is the one that turns the whole-conversation tasks off by default. A public provider receives the redacted full conversation for these tasks (`docs/PRIVACY.md`: "that is the redacted full conversation, not 1-3 sentences"). The user turns them on for that conversation on purpose, as the deep-insight lane already requires ("opt-in per conversation, default off" in `docs/ARCHITECTURE_BOUNDARIES.md`). The `summary` is the rolling case: it is on by default only up to the `tenant` profile.

3. **`insight` never runs on a local provider.** `docs/ARCHITECTURE_BOUNDARIES.md` states that the deep-insight lane "never runs against a local-only provider". With a `local` profile the lane therefore stays off, and the dashboard says so. This ADR keeps that rule and does not add the exception that the code currently allows (see Open Items).

4. **Defaults when the user sets nothing.** The default conversation profile stays `local` in the sense of `LLM_PROVIDER=none`: no task sends text, and the local detection runs. `tier_of("none")` is `local`, so this satisfies every ceiling. A user who picks a profile in the dashboard or `klant.yaml` and has not configured a provider for it gets the behaviour of point 5, not a silent move to another tier.

5. **A task whose tier is not configured.** The rule is: **stricter or off, never looser.**
   - A task with no provider at its tier is **skipped**: the task is not started and the dashboard shows the reason as a module warning, the same channel `_health_check_providers` in `src/sales_copilot/__main__.py` already uses. No client is built and nothing is sent.
   - A task is **never** moved to a less strict tier. With a `local` profile and no local model installed, `suggestions` does not fall to Azure or OpenRouter; it is off.
   - A task may move to a **stricter** tier, for example `report` on a `public` profile using the user's local model, only when the user named that provider for the task. The code does not choose it.
   - Where today a task above the ceiling makes `_enforce_task_ceiling` return a 400 for the whole start-call, the later PR keeps that refusal for a provider the user named explicitly above the ceiling (an explicit setting that contradicts the profile is an error the user should see), and uses the skip above only for a task that was never given a provider.
   - The OpenRouter fallback models (`<TASK>_LLM_FALLBACK_MODELS`) stay inside the same provider tier by construction, because they are only set for `openrouter` (`resolve_llm`). They never cross a ceiling.

6. **How the per-conversation choice selects the task set (a later PR, no code here).** The choice already travels the right way and needs one addition:
   - Today the profile is read in `extract_start_call_config` (`hub_core.py`): a caller-supplied `privacy` is validated with `validate_privacy`, `_apply_klant_config` lets `klant.yaml` overwrite it, and `_enforce_task_ceiling` runs `resolve_llm` for each task in `active_tasks()`.
   - `active_tasks()` in `llm_routing.py` gains the profile: `active_tasks(detector_config, insight_active=..., report_active=...)` reads `detector_config.privacy` and drops the tasks that the matrix in point 2 turns off for that profile, unless the task's own provider variable (`<TASK>_LLM_PROVIDER`, `INSIGHT_PROVIDER`) is set. One table of task and profile lives in `llm_routing.py` next to `TASKS`, so `__main__._health_check_providers`, the detector module and the start-call check read the same set.
   - `_conversation_detector_config` and `_enforce_task_ceiling` in `hub_core.py` call the new `active_tasks` unchanged in shape. `_insight_lane_active` also returns false for a `local` profile.
   - `resolve_llm` returns, or raises a typed error for, "no provider at this tier"; the module that owns the task catches it and skips the task. `modules/reports/enrichment.py::_resolve_part` already does this for `report` and `report_terms` (logs a WARNING and skips on `PrivacyGateError`); the live modules get the same treatment.
   - The dashboard start-call form sends the profile it already sends (`privacy` in the start-call config); it shows which tasks the chosen profile starts. Dashboard stays vanilla HTML/CSS/JS.
   - The profile stays per conversation. `klant.yaml` supplies a customer's profile for a conversation with that customer, which is a per-conversation start-call input, not a stored setting of the installation.

7. **Docs follow the code.** The later PR updates the task table in `docs/PRIVACY.md` (which today lists only detection, summary, suggestions, report, term corrections and the deep lane, and not `window_classifier`, `phase`, `script_tracking` or `slides`) and adds the matrix.

## Alternatives considered

**One list of allowed providers per task, no profile.** Rejected: that is per customer or per installation, which `CLAUDE.md` rules out. The same user runs a local call and a public call on the same installation.

**Let a task fall back to the next less strict tier when its own tier is not configured.** Rejected: it sends text off the machine without a decision by the user. A missing local model is a reason to skip, not a reason to publish.

**Turn every task on for every profile and rely on the ceiling alone (the situation today).** Rejected as the target: on `public`, the whole-conversation tasks send the redacted full call to a public provider by default. That is allowed, but it should be a choice for that conversation.

**Raise the per-task minimum in code as a fourth profile.** Rejected: three profiles are what `klant.yaml`, the dashboard and `PRIVACY.md` already name. A fourth one adds a migration and no new guarantee.

## Consequences

- The ceiling in `privacy_gate.py` remains the enforcement point. This ADR adds a default task set on top and does not weaken the ceiling.
- A `local` conversation without a local model runs only the local detection and talk-time coaching. That is a smaller product, and it is the intended trade for the default.
- A user on `public` has to switch on `summary`, `report` and `insight` for that conversation. This costs a click and makes the full-transcript exit visible.
- `__main__._health_check_providers`, `hub_core._enforce_task_ceiling` and the detector module read one task set, so the start-call check and the runtime cannot disagree.

### Risks

- **Latency on local models for live tasks.** The live tasks have a 5-second budget (`LLM_TIMEOUT_MS`). Local models measured so far were too slow for a live answer, and `resolve_llm` already raises the timeout to the Ollama floor (`_OLLAMA_TIMEOUT_FLOOR_MS`). On a `local` profile `suggestions` and `script_tracking` may arrive late or miss the moment. The fast detection that does not use an LLM keeps working. The later PR does not promise live latency on `local`.
- **PII leaving the machine on the public tier.** `apply_outbound_pii` redacts text in `cloud_only` mode, and redaction is a pattern filter, not a guarantee. The whole-conversation tasks send the entire transcript. `PII_REDACTION=off` or `ALLOW_RAW_LLM_PII` removes it, and `tier_of` does not look at either. The profile does not change that; it only decides what starts.
- **Tenant is only tenant with `TRUST_OWN_TENANT`.** Without it, an azure or vertex provider counts as `public`, so a `tenant` profile refuses it. The user who expects "my own tenant" to work must set the flag. The error message names the provider and tier.
- **Cost.** Each live task is a call per window or per utterance; `insight` and `report` are large calls over the whole transcript. On `public` and `tenant` this is billed to the user's key or tenant. Output limits (`TASK_DEFAULT_MAX_OUTPUT_TOKENS`) bound the answer, not the input. The OpenRouter fallback models can change the price per call.

## What this does not do

- It does not change the ceiling, the tier classification, or the transcription backends (`enforce_transcription_privacy` stays as is; audio is a separate class).
- It does not decide models or prices per task; `config/llm_models.yaml` and `<TASK>_LLM_MODEL` stay as they are.
- It does not add code. The later PR in the track builds point 6.
- It does not cover the MCP bridge or report delivery, which have their own outbound classes in `docs/ARCHITECTURE_BOUNDARIES.md`.
