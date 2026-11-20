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
      "provider": "openrouter",
      "model": "anthropic/claude-haiku-4.5"
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

## Auto-stop on silence

A recording stops by itself when both tracks (the other side's system audio and your microphone)
have been quiet for `RECORDER_AUTOSTOP_SILENCE_SECONDS`, so a forgotten STOP does not leave the
recorder running for hours. The notetaker (recorder page and bar) defaults to 120 seconds; the
copilot recorder (`scripts/record_call.py`) defaults to off. `0` switches it off, and the value
should be at least 90. Both recorders read the `RECORDER_AUTOSTOP_*` settings from `.env`; no shell export is needed.

- The clock starts at the first audible chunk, so silence before the call begins never stops a recording.
- A pause suspends the clock, and audio during a pause does not count as speech.
- One track talking is enough to keep the recording alive.
- "Quiet" is a level in dBFS, the unit of the recorder's level meter: `RECORDER_AUTOSTOP_PROSPECT_DB`
  (default -50) for the system audio and `RECORDER_AUTOSTOP_SELF_DB` (default -40) for the
  microphone, where room noise sits higher. The defaults come from 22 real recordings: speech on
  the system track sits above -42 dBFS (5th percentile) and on the microphone above -49, while the
  microphone's room noise after the hang-up stays under -38 (99th percentile) and the system track
  after the call is digital silence. The longest quiet stretch inside a call was about 2 minutes
  on the microphone, but never on both tracks at once for as long as the window.
- A stop by silence is visible: the recorder page and the bar show "stopped after N s of silence",
  `metadata.json` gets an `auto_stop` entry, and the transcript ends with a line at the stop point.

## Tap warnings in the notetaker

The notetaker watches the system track (the other side). Exact zeros after the track carried signal
mean the tap is lost: the recorder page and the bar warn at once, it reattaches up to 3 times per
dropout (a later, independent dropout gets 3 attempts of its own), and the transcript gets a line
at the lost and the reattached moment. The thresholds are `AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS`
(default 30) and `AUDIO_TAP_HEARTBEAT_SECONDS`.

A system track that is exact zero from the very start while the microphone carries signal for
`AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS` (default 120) gets a calm `no_system_audio` note: the call may not
play through this computer. It is warning only, never a reattach, is logged once in `recorder.log`
and recorded in `tap_events` of `metadata.json`, and clears at the first real system signal.

## Model pick, report wording and word lists

An empty model pick in the dashboard ("Select a model") sends no override: `LLM_PROVIDER` and
`LLM_MODEL` from `.env` stay in force, and so does the code default when `.env` sets nothing.

The report prompt speaks of a seller and a prospect only for a call on the `sales` domain preset
without call-preset labels. The `notetaker` preset and any call without a sales preset get a
neutral prompt that names the two speakers by the preset's `ui_labels` (neutral by language when
it names none) and tells the model to write the summary in `CALL_LANGUAGE`.

`config/transcription_vocabulary.yaml` and `config/transcript_normalization.yaml` are found the
way the rest of the app finds its files, whatever the working directory: a copy in the app
support folder first, then the bundled one (checkout, pip/pipx install, packaged app). An
absolute `WHISPER_VOCABULARY_CONFIG` is used as given.

### A vocabulary file per customer

Copy `config/transcription_vocabulary.yaml`, replace the `terms` with the customer's product
names, systems and jargon, and point `WHISPER_VOCABULARY_CONFIG` at the copy. Only list terms
Whisper gets wrong: proper nouns, acronyms, compound jargon.

The budget is hard. Whisper reserves 223 tokens for the prompt (the framing `prompt_prefix`
plus all terms), and a Dutch proper noun costs about 4 tokens, so the ceiling is roughly 55
terms. whisper.cpp keeps the last tokens and drops the head, framing sentence included, without
a log line. Check a file before use:

```bash
python scripts/render_vocabulary.py path/to/customer.yaml
```

It prints the exact prompt, the token count (the multilingual tokenizer Whisper uses, offline)
and the margin. Over budget it exits 1 and names the trailing terms past the budget. At startup
the whisper.cpp and mlx backends log a WARNING with the same numbers when the configured file
is over budget.

To find terms from a corrected call, run
`python scripts/vocabulary_candidates.py <raw transcript> <corrected transcript>`: it lists the
words and phrases the correction changed (case-insensitive, word level), ranked by count.

## One model per task

Every LLM task has its own provider, model, timeout and output limit. A task
without its own value falls back to the global `LLM_PROVIDER`, `LLM_MODEL` and
`LLM_TIMEOUT_MS`; `report_terms` falls back to `report` first. `LLM_PROVIDER` defaults to `none` in the code as well as in `.env.example`: without a `.env` no model is configured, no client is built and nothing is sent. A provider of `none` counts as local for a `klant.yaml` privacy ceiling. A client's
`klant.yaml` privacy ceiling applies to every LLM task, and a task whose provider
is above it is refused. The ceiling covers the transcription backend too:
`WHISPER_BACKEND=groq`, whisper.cpp on a non-loopback `WHISPER_CPP_SERVER_HOST` and an
unknown backend count as `public`, so a `local` or `tenant` ceiling refuses them at start-call.
A provider other than the global one needs its own
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

### OpenRouter fallback models

For tasks on `openrouter`, `<TASK>_LLM_FALLBACK_MODELS` (`INSIGHT_LLM_FALLBACK_MODELS` for
`insight`) or the global `LLM_FALLBACK_MODELS` lists models to try, comma-separated and empty
by default. The task's list wins; `report_terms` reads `REPORT_LLM_FALLBACK_MODELS` first.
With a list set, the SDK does not retry a failing model and the `LLM_TIMEOUT_MS` budget is split equally over the attempts (the overall live cap is unchanged); without a list nothing changes.
A 5xx, 429, timeout or connection error sends the same request to the next model; any other
4xx does not fall back. A stream falls back only before its first chunk. When every model fails,
the last error surfaces. Each attempt passes the privacy ceiling, each switch is logged as a
warning, and other providers ignore the list. OpenRouter's `models` / `allow_fallbacks` fields are not sent.

## Related References

- `.env.example` for global defaults
- `docs/TTD.md` section 7 for WebSocket schemas
- `dashboard/js/setup.js` for config payload construction
