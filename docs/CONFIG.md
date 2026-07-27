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

Disabled modules are not started by the orchestrator.

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

## Related References

- `.env.example` for global defaults
- `docs/TTD.md` section 7 for WebSocket schemas
- `dashboard/js/setup.js` for config payload construction
