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
```

`termen` is validated (max 200 entries, 64 characters each) and passed
through to the transcriber as `config.call_terms` on session start -- see
`src/sales_copilot/core/klant_config.py` for the schema and
`src/sales_copilot/websocket/hub_core.py` for where it's added to the
outgoing config.

## Related References

- `.env.example` for global defaults
- `docs/TTD.md` section 7 for WebSocket schemas
- `dashboard/js/setup.js` for config payload construction
