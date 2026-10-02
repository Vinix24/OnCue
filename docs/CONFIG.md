# Configuration Panel

The dashboard setup screen is the primary way to configure a call before starting modules.
It drives the `start_call` payload sent to the WebSocket hub and overrides `.env` defaults
for a single session.

## Setup Screen Overview

- **Screen mode**: Single (one display) or Dual (dashboard + presentation).
- **Module toggles**: Enable or disable individual modules per call.
- **LLM settings**: Provider and model for pain point confirmation.
- **Prospect details**: Company and industry for report context.
- **Context docs**: Upload files to add extra sales context for detection and reporting.

## Module Toggles

The setup screen exposes these modules:
- `talk_time` (Module 1)
- `transcript` (Module 2)
- `pain_points` (Module 3)
- `presentation` (Reveal.js slide control)
- `post_call_report` (Module 4)

Disabled modules are not started by the orchestrator, and the orchestrator says so:
next to `Enabled modules` it logs a `Disabled modules` line naming each one and where
its value came from (explicit in the `start_call` payload, a legacy top-level flag, or
the default). A disabled `pain_points` additionally produces a warning, because a call
without detection is a copilot that cannot do its main job.

`pain_points` is the AI-detection master switch: it gates pain points, objections,
buying signals and suggestions in one. Two shipped presets set it to `false` on purpose
(`discovery`, `coaching_only`). The dashboard writes the module checkboxes from a
preset only once on a fresh browser with no stored config; a deliberate preset click or
a restored session always wins, and a call with detection off carries a standing
indicator in the UI. Before 2026-09-05 the first preset was re-applied on every page
load, which silently switched detection off on every reload.

## WebSocket Config Flow

The dashboard `POST`s the setup-screen config to `/api/start-call`. The hub applies it
and broadcasts it to module processes (and the dashboard itself) on `/ws/config`:

```json
{
  "type": "start_call",
  "config": {
    "screen_mode": "dual",
    "modules": {
      "talk_time": true,
      "transcript": true,
      "pain_points": true,
      "presentation": true,
      "post_call_report": true
    },
    "llm": {
      "provider": "gemini",
      "model": "gemini-2.5-flash"
    },
    "prospect": {
      "company": "Acme BV",
      "industry": "SaaS"
    },
    "uploads": ["/path/to/context.pdf"]
  }
}
```

To end a session, the dashboard `POST`s to `/api/end-call`; the hub broadcasts on `/ws/config`:

```json
{ "type": "end_call" }
```

## Presets

Presets are loaded from `/api/presets` and can be applied before starting a call.
They map into the same configuration fields as the setup screen.

## Uploads

Context documents are uploaded via `/upload` and passed in `config.uploads`.
These files are appended to the LLM confirmation prompt and the report context.

## Client-specific terms (`klant.yaml`)

`config/transcript_normalization.yaml` is the *global* post-ASR correction
list, loaded for every client. It must never carry a real client, company, or
person name — a name added there would "fix" one client's calls at the cost
of leaking that client's name into every other client's normalization pass,
and it would also end up in the public export (`scripts/export_public.sh`).

A term that only matters for one client (its own company name, a person the
client mentions often, a product name specific to that account) belongs
per-conversation instead, under `termen` in that client folder's
`klant.yaml`:

```yaml
# data/clients/<slug>/klant.yaml -- synthetic example client, not a real one
bedrijf: Wattelaar Turbines B.V.
branche: Industriele aandrijftechniek
contactpersonen:
  - Roos Bergkamp
termen:
  - Wattelaar
  - Bergkamp
  - VWA -> VBA
```

An entry is either a canonical term (`Bergkamp`: near-misses of 5+ characters
are corrected to it) or an exact variant written `bron -> doel` (`VWA -> VBA`:
for short forms that fuzzy matching never touches). A name of two words is written as an exact variant (`Marius Bakker -> Marius Bakkers`), because a bare two-word canonical term is not fuzzy-matched; a common first name such as `Nathan -> n8n` also belongs only here, never in the global list. Per-conversation entries
take precedence over the global list. The list stays in the transcriber
process memory, is cleared when the call ends, and is never logged at INFO or
sent in a payload. The original wording and each replacement go to the local
transcriber log at DEBUG only.

`termen` is validated (max 200 entries, 64 characters each) and passed
through to the transcriber as `config.call_terms` on session start -- see
`src/sales_copilot/core/klant_config.py` for the schema and
`src/sales_copilot/websocket/hub_core.py` for where it's added to the
outgoing config.

## Post-call term list (`termenlijst.yaml`)

After the call, the report model can say where a term from the client's list
was meant while speech recognition wrote an ordinary word or a name that looks
or sounds alike (`ROI` written as the first name `Roy`, `Teamleader` written as
`Team Leader`). This is its own task, `report_terms` (`REPORT_TERMS_LLM_*`),
that rides along in the one report call as long as it resolves to the same
provider and model as `report`; see `src/sales_copilot/modules/reports/term_corrections.py`.

The term list for that step merges three sources, in this order, without
duplicates:

1. `termen` from the client's `klant.yaml` (the live list above);
2. `termenlijst.yaml` in the same client folder, optional and post-call only;
3. the global `config/transcript_normalization.yaml` (its `terms`, and its
   `variants` as `bron -> doel`).

A call without a client uses the global list only.

`termenlijst.yaml` takes the same entry syntax as `termen`, as a list under
`termen`:

```yaml
# data/clients/<slug>/termenlijst.yaml -- synthetic example, not a real client
termen:
  - Wattelaar
  - Teamleader
  - ROI
  - Turbinewacht
  - VWA -> VBA
```

It is never sent to the live transcriber, so the live cap of 200 does not apply.
Its own limits: at most 5000 entries, each a non-empty string of at most 64
characters without a line break or `;`, a file of at most 1 MiB, and the only
key is `termen`. The file is read through the same client-folder path safety as
`klant.yaml` (`KLANTEN_ROOT`), and a `termenlijst.yaml` that resolves outside
that root (a symlink) is refused. A file that fails validation is left out of
that call's report with an ERROR in the log naming the entry by its number,
never by its text; the other two sources still count.

Per segment, only the terms that resemble something in it go to the model, in
the user part of the prompt, so they pass `apply_outbound_pii` like the
transcript does (`PII_REDACTION`, `TRUST_OWN_TENANT`). The code then checks
every correction: the target is in the list and was a candidate the model saw,
the source really occurs at that place in the original segment, the PII policy
did not change how often it occurs there, and no word is lost. The report keeps
the original transcript (`full_transcript`) and lists the accepted corrections
in `term_corrections`, each with `segment_index`, `source`, `occurrence`,
`target` and `reason`. `term_corrections_pii_limited` counts the segments with
candidate terms that were anonymised before the model saw them: a name stripped
to `[NAAM]` there is not corrected. With `REPORT_REDACT_PII=true`, `source`,
`target` and `reason` are redacted on disk like the rest of the report. The
dashboard's `report_enriched` event carries only the counts.

## One model per task

Every LLM task has its own provider, model, timeout and output limit. A task
without its own value falls back to the global `LLM_PROVIDER`, `LLM_MODEL` and
`LLM_TIMEOUT_MS`; `report_terms` falls back to `report` first. A client's
`klant.yaml` privacy ceiling applies to every task, and a task whose provider
is above it is refused. A provider other than the global one needs its own
`<TASK>_LLM_MODEL`. Defaults live in `src/sales_copilot/core/llm_routing.py`.

| Task | Does | Keys |
|---|---|---|
| `detector_confirm` | confirms a pain point | `DETECTOR_CONFIRM_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `window_classifier` | classifies a transcript window | `WINDOW_CLASSIFIER_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `phase` | detects the call phase | `PHASE_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `suggestions` | follow-up questions | `SUGGESTIONS_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `summary` | running conversation summary | `SUMMARY_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `script_tracking` | script coverage | `SCRIPT_TRACKING_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `slides` | generated slides | `SLIDES_LLM_PROVIDER/_MODEL/_TIMEOUT_MS/_MAX_OUTPUT_TOKENS` |
| `insight` | deep insights | `INSIGHT_PROVIDER`, `INSIGHT_MODEL`, `INSIGHT_LLM_TIMEOUT_MS`, `INSIGHT_LLM_MAX_OUTPUT_TOKENS` |
| `report` | post-call summary, overview, keywords, action items | `REPORT_LLM_PROVIDER/_MODEL/_TIMEOUT_MS`, `REPORT_MAX_OUTPUT_TOKENS` |
| `report_terms` | post-call term correction | `REPORT_TERMS_LLM_PROVIDER/_MODEL/_TIMEOUT_MS`, `REPORT_TERMS_MAX_OUTPUT_TOKENS` |

When `report` and `report_terms` resolve to the same provider and model, the
report stays one call. When they differ, the report step makes two calls, and
the term correction only runs when the call has candidate terms. A failing or
refused part never costs the other. The measured recommendation (Sonnet 5.5 for
`report_terms`, a faster model for `report`) is in `.env.example`.

## Related References

- `.env.example` for global defaults
- `docs/TTD.md` section 7 for WebSocket schemas
- `dashboard/js/setup.js` for config payload construction
