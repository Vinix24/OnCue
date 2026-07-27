# Module 3 — Pain Point Detection

Module 3 classifies prospect speech into pain points, objections, buying signals, and
doubt, then publishes detection cards to the dashboard and drives in-call slide
navigation. The default live path is a sliding-window multi-task LLM classifier
(PR-105); a semantic-router fallback keeps detection working with no LLM configured,
and a separate always-on fast path handles objections and buying signals independently
of the LLM classifier.

## Architecture Overview

**Primary pipeline (default live path, runtime order):**

```
Prospect transcript chunk (from /ws/transcript, speaker = "prospect")
    │
    ▼
SlidingWindowBuffer — deque of the last DETECTOR_WINDOW_SIZE chunks (default 5)
    │
    ▼ once len(buffer) >= DETECTOR_MIN_CHUNKS (default 3)
    │  and DETECTOR_DEBOUNCE_S has elapsed since the last classification (default 5s)
WindowClassifier.classify() — single multi-task LLM call over the window's joined text
    │
    ├── category=pain_point    → SlideInjector.handle_window_detection() → /ws/pain-points (+ /ws/slide-control)
    ├── category=doubt         → /ws/coaching (coaching_alert, subtype="doubt")
    ├── category=objection     → dropped (ObjectionDetector is the sole emitter — see below)
    ├── category=buying_signal → dropped (ObjectionDetector is the sole emitter — see below)
    └── category=none          → dropped
```

`WindowClassifier.classify()` is not gated on `llm_provider`: it only no-ops when
`provider` is `"none"` or empty. `DetectorConfig.llm_provider` defaults to `openrouter`
(`.env.example` ships `LLM_PROVIDER=gemini`), so this sliding-window path is what runs
on a default install. Detections below `CONFIDENCE_THRESHOLD_LOW` are dropped in
`_consume_transcripts` before dispatch; `CONFIDENCE_THRESHOLD_HIGH` plays no role in this
path (it is only used by the fallback router below).

Components and locations:
- `SlidingWindowBuffer`, `TranscriptChunk`: `src/sales_copilot/modules/detector/sliding_window.py`
- `WindowClassifier`, `WindowAnalysis`, `WindowDetection`: `src/sales_copilot/modules/detector/window_classifier.py`
- Detector runner (owns the consume loop, wiring, dispatch): `src/sales_copilot/modules/detector/__main__.py`
- `SlideInjector.handle_window_detection`: `src/sales_copilot/modules/copilot/injector.py`
- Case DB: `src/sales_copilot/modules/slides/case_db.py`
- Preset system (per-vertical pain point / objection / doubt taxonomies): `src/sales_copilot/core/preset.py`, `config/presets/*.yaml`

## Data Flow

1. Transcriber publishes `{type:"transcript", text, speaker, start_ms, end_ms}` to `/ws/transcript`.
2. `_consume_transcripts` (`__main__.py`) buffers each prospect chunk into `SlidingWindowBuffer`. Non-prospect (`self`) chunks are skipped here when `ONLY_CLASSIFY_PROSPECT=true` (default) — the buffer only ever holds prospect utterances.
3. Once the buffer holds `DETECTOR_MIN_CHUNKS` chunks and the classification debounce has elapsed, `WindowClassifier.classify(window_text, latest_chunk)` sends one `instructor`-structured LLM call over the joined window text and returns zero or more `WindowDetection` entries (`pain_point | objection | buying_signal | doubt | none`).
4. `_dispatch_window_detection` acts only on `pain_point` and `doubt` detections above `CONFIDENCE_THRESHOLD_LOW`. `pain_point` detections go to `SlideInjector.handle_window_detection`, which looks up a matching case and publishes to `/ws/pain-points` (and `/ws/slide-control` on a case or dynamic-slide match). `doubt` detections publish directly to `/ws/coaching`.
5. The `phase` passed to `classify()` defaults to `"discovery"`: `_consume_transcripts` never threads the live call phase (tracked separately by `AutomaticPhaseDetector` on `/ws/phase`) into the classification call, so the phase-aware system prompt currently always runs in discovery framing regardless of the call's actual phase.

## Fast objection & buying-signal detection (always-on, independent of LLM config)

`PainPointRouter` (`router.py`, the embedding router used by the fallback path below) is
also the base class of `ObjectionRouter` (`objection_detector.py`), which backs the
always-on fast objection/buying-signal path. `ObjectionDetector` runs
`ObjectionRouter.classify_async()` — a `semantic-router` embedding match, not an LLM call
— on **every** transcript chunk, gated only by `ENABLE_OBJECTION_DETECTION` (default
`true`), regardless of `LLM_PROVIDER`. In the live wiring (`main()` in `__main__.py`) it
is constructed with `objections=preset.objections`, so its taxonomy comes from the active
`PRESET` (`config/presets/<name>.yaml`), not directly from `config/objections.yaml`
(that file is only the fallback when no preset override is passed).

- Matches below `CONFIDENCE_THRESHOLD_LOW` are dropped here too — the same threshold
  used to gate the primary window path's dispatch.
- Matches resolving to an opportunity-route name (`INCLUDE_OPPORTUNITIES`, default
  `true`) are treated as buying signals and published to `/ws/buying-signals`; everything
  else is treated as an objection and published to `/ws/objections`.
- A loaded negative/"none" class (`INCLUDE_NEGATIVES`, default `true`) competes for the
  best match and is filtered out in `ObjectionRouter.classify()`, so generic prospect
  speech does not get forced into the nearest objection category.
- Each category is rate-limited by its own `PainPointDebouncer` (`DEBOUNCE_SECONDS`,
  default 45s) — a separate debouncer instance from the fallback path's.
- `ObjectionDetector` is the **sole emitter** on `/ws/objections` and `/ws/buying-signals`.
  The `WindowClassifier` above can also classify `objection`/`buying_signal`, but
  `_dispatch_window_detection` intentionally drops those two categories so the curated
  `response_suggestion` and the Free/Pro response-playbook gate are only ever applied
  once, from this fast path (see the comment at `__main__.py`'s `_dispatch_window_detection`).

## Graceful-degradation fallback (`LLM_PROVIDER=none`)

When no LLM provider is configured, `_consume_transcripts` runs a second, LLM-free
detection path on every prospect chunk so pain points still surface without an API key.
This is gated explicitly:

```python
# __main__.py, _consume_transcripts (~line 242)
if config.llm_provider in {"none", ""}:
    await injector.process_transcript(text, speaker, timestamp_ms)
```

This is intentional, tested graceful degradation, not dead code — the sliding-window
path above still runs in parallel, but `WindowClassifier.classify()` is a no-op when
`provider` is `"none"`/empty, so this fallback is what actually produces pain-point
detections in that configuration. It uses the pre-PR-105 pipeline:

```
PainPointRouter.classify()          — semantic-router embedding match against
                                       config/pain_points.yaml (not preset-driven)
    │
    ├── score >= CONFIDENCE_THRESHOLD_HIGH → emit immediately (no LLM call)
    ├── score <  CONFIDENCE_THRESHOLD_LOW  → drop
    └── in between                          → LLMConfirmClient.confirm_async()
                                               (a no-op when provider="none": build_client
                                               returns None, so confirm_async returns None
                                               and the provisional match is never upgraded)
    │
    ▼
PainPointDebouncer.should_trigger() — 45s per-category cooldown (DEBOUNCE_SECONDS)
    │
    ▼
DetectionPipeline.aprocess() → SlideInjector.process_transcript() → /ws/pain-points
```

Components: `PainPointRouter` (`router.py`), `LLMConfirmClient` (`llm_confirm.py`),
`PainPointDebouncer` (`debouncer.py`), `DetectionPipeline` (`pipeline.py`),
`SlideInjector.process_transcript` (`copilot/injector.py`). `LLMConfirmClient` is also
reused (with a real provider configured) by `SlideGenerator`
(`copilot/slide_generator.py`) for on-the-fly slide generation when no pre-authored case
matches a detected pain point.

## Configuration (.env)

Window classifier (primary path):
- `LLM_PROVIDER` — `.env.example` ships `gemini`; `DetectorConfig` falls back to
  `openrouter` if unset. Any non-empty value other than `none`/`""` activates the
  sliding-window path.
- `LLM_MODEL`, `LLM_TEMPERATURE` (default 0.1), `LLM_TIMEOUT_MS` (default 7000; a 90s
  floor is enforced for `ollama`)
- `DETECTOR_WINDOW_SIZE` (default 5) — sliding-window buffer size
- `DETECTOR_MIN_CHUNKS` (default 3) — minimum buffered chunks before classifying
- `DETECTOR_DEBOUNCE_S` (default 5.0) — minimum seconds between window classifications
- `CONFIDENCE_THRESHOLD_LOW` (default 0.50) — drop threshold applied to each `WindowDetection`
- `PRESET` (default `sales`) — selects `config/presets/<name>.yaml`, which supplies the
  pain point / objection / buying-signal / doubt category taxonomy for both the window
  classifier and the always-on objection path

Always-on objection/buying-signal path:
- `ENABLE_OBJECTION_DETECTION` (default `true`)
- `INCLUDE_OPPORTUNITIES` (default `true`), `INCLUDE_NEGATIVES` (default `true`)
- `DEBOUNCE_SECONDS` (default 45) — per-category cooldown, shared with the fallback path's `PainPointDebouncer`

Fallback path (`LLM_PROVIDER=none`) + shared thresholds:
- `EMBEDDING_MODEL` (default `paraphrase-multilingual-MiniLM-L12-v2`)
- `CONFIDENCE_THRESHOLD_HIGH` (default 0.85) — used only by the fallback router's tier decision
- `PAIN_POINTS_CONFIG` (default `config/pain_points.yaml`), `OBJECTIONS_CONFIG` (default `config/objections.yaml`)
- `ONLY_CLASSIFY_PROSPECT` (default `true`)
- `CALL_LANGUAGE` (default `nl`)

Case DB:
- `CASE_DB_SQLITE_PATH` (default `data/cases.db`)
- `PROSPECT_INDUSTRY` (optional, filters case lookup)
- `DYNAMIC_SLIDES` (default `true`) — Pro-gated LLM slide generation when no case matches

WebSocket hub:
- `WS_HUB_HOST` (default `127.0.0.1`)
- `WS_HUB_PORT` (default 8760)

## WebSocket Schema (TTD.md Section 7)

**Channel: `/ws/pain-points`**

```json
{
  "type": "pain_point",
  "category": "offerteproces",
  "label": "Offerteproces",
  "confidence": 0.92,
  "trigger_phrase": "we zitten echt uren aan zo'n offerte",
  "timestamp_ms": 124500,
  "live": true,
  "provisional": false,
  "case_matched": true,
  "case_id": "case-017",
  "case_title": "Klant X — 70% snellere offertes",
  "response_suggestion": "string (matched case description) | teaser | \"\""
}
```

`category` is the detection's subcategory (route name from `config/pain_points.yaml` /
the active preset), falling back to the literal `"pain_point"` if the classifier left
it empty. `response_suggestion` is gated by `FEATURE_RESPONSE_PLAYBOOK`: the matched
case's description on Pro, the fixed `RESPONSE_LOCKED_TEASER` teaser on Free, or `""`
when no case matched. `live`/`provisional` only vary on the fallback path (async
embedding match confirmed by the LLM in the background); the sliding-window path always
publishes a single, final detection per window.

**Channel: `/ws/pain-points` (async enrichment — fallback path only)**

```json
{
  "type": "pain_point_enrichment",
  "category": "offerteproces",
  "confidence": 0.94,
  "trigger_phrase": "we zitten echt uren aan zo'n offerte",
  "timestamp_ms": 124500,
  "case_matched": true,
  "case_id": "case-017"
}
```

Published only by `SlideInjector.process_transcript`'s `_on_enrichment` callback when the
fallback `DetectionPipeline.aprocess()`'s background LLM confirmation upgrades a
provisional match. The sliding-window path never emits this message.

**Channel: `/ws/coaching` — doubt detection**

```json
{
  "type": "coaching_alert",
  "subtype": "doubt",
  "category": "sceptisch",
  "confidence": 0.81,
  "trigger_phrase": "ik weet het nog niet, klinkt duur",
  "timestamp_ms": 145200,
  "speaker_label": "prospect"
}
```

This is a distinct message shape from Module 1's talk-time `coaching_alert`
(`alert_type`/`message`/`severity` — see `docs/MODULE1.md`); both share the
`/ws/coaching` channel and `"type": "coaching_alert"`, discriminated by the presence of
`subtype`.

**Channel: `/ws/objections`** (emitted by `ObjectionDetector` only, never by `WindowClassifier`)

```json
{
  "type": "objection",
  "category": "prijs | timing | concurrent | scope | autoriteit",
  "confidence": 0.88,
  "trigger_phrase": "dat is echt te duur voor ons",
  "response_suggestion": "string (curated) | teaser",
  "timestamp_ms": 145200
}
```

**Channel: `/ws/buying-signals`** (emitted by `ObjectionDetector` only, never by `WindowClassifier`)

```json
{
  "type": "buying_signal",
  "category": "prijs | timing | concurrent | scope | autoriteit",
  "confidence": 0.81,
  "trigger_phrase": "dat klinkt wel als iets wat we kunnen gebruiken",
  "response_suggestion": "string (curated) | teaser",
  "timestamp_ms": 167800
}
```

Both `response_suggestion` fields are gated by `FEATURE_RESPONSE_PLAYBOOK`: curated text
from `config/objection_responses.yaml` / `config/opportunity_responses.yaml` on Pro, the
fixed `RESPONSE_LOCKED_TEASER` teaser on Free.

## Dashboard

- Dashboard panel: `dashboard/js/pain-points.js` listens to `/ws/pain-points`.

## Presentation control (Pro capability)

Advancing the shared presentation during a call — for example moving to a relevant case
slide when a pain point is detected — is a Pro-gated capability
(`presentation.dynamic_slides`, `FEATURE_DYNAMIC_SLIDES`). Both `SlideInjector` entry
points (`process_transcript` for the fallback path, `handle_window_detection` for the
primary path) check this gate before publishing to `/ws/slide-control`; its
implementation is not documented publicly.

## Running Module 3

```bash
PYTHONPATH=src python -m sales_copilot.modules.detector
```

This connects to `/ws/transcript` and emits detection cards on `/ws/pain-points`,
`/ws/coaching`, `/ws/objections`, and `/ws/buying-signals`.

## Limitations

- The window classifier's `phase` argument is not wired to `AutomaticPhaseDetector`'s
  live phase output; live classification always runs with `phase="discovery"` framing
  in the system prompt unless a caller (e.g. a test harness) passes `phase=` explicitly.
- The sliding-window path has no per-category debounce — only the window-level
  `DETECTOR_DEBOUNCE_S` throttles how often a new classification call fires at all. Rapid
  distinct windows can re-report the same pain point sooner than the 45s
  `DEBOUNCE_SECONDS` cooldown that applies to the fallback path and to the objection/
  buying-signal path.
- In the `LLM_PROVIDER=none` fallback, middle-confidence matches
  (`CONFIDENCE_THRESHOLD_LOW` ≤ score < `CONFIDENCE_THRESHOLD_HIGH`) are published as
  `provisional: true` and never upgraded — the background `LLMConfirmClient` confirmation
  is itself a no-op with no provider configured.
- The fallback `PainPointRouter`'s pain-point taxonomy is read directly from
  `PAIN_POINTS_CONFIG` (`config/pain_points.yaml`), not from the active `PRESET`; only
  the sliding-window classifier and the always-on objection path are preset-driven.
- Embedding paths (fallback classification, the always-on objection/buying-signal path)
  require a `sentence-transformers` model download on first run.

## Certification Evidence (PR-105)

- CI gate: `bash scripts/ci.sh`
- Unit tests: `tests/test_sliding_window.py`, `tests/test_window_classifier.py`,
  `tests/test_window_classifier_error_logging.py`, `tests/test_detector_main.py`,
  `tests/test_objection_detector.py`, `tests/test_objection_router_negatives.py`,
  `tests/test_objection_router_opportunities.py`, `tests/test_detector_router.py`,
  `tests/test_detection_pipeline.py`, `tests/test_detector_async.py`
- E2E: `tests/test_module3_e2e.py`, `tests/test_replay_detector_e2e.py`
