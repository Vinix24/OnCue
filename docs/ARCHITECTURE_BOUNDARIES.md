# Architecture boundaries (enforced)

This document describes the hard architecture invariants of the OnCue project. They are not meant as a "guideline": CI runs `scripts/check_architecture_boundaries.py`, so a PR that crosses a boundary turns red.

## Invariants

| # | Invariant | Why | Check |
|---|---|---|---|
| 1 | **Local-first data plane** | Audio, transcript, embeddings, session DB, and case DB stay on the device. The default outbound class is short, PII-redacted LLM fragments. A deliberate second class — the deep-insight lane — may send the PII-redacted full session transcript to an operator-configured frontier/enterprise destination (see "Deep-insight lane outbound class" below). | Provider SDKs may only be imported in `core/llm_client.py`. Network-client imports in `src/` are on an allow-list; new network code must be deliberately reviewed. |
| 2 | **BYO-tenant / provider-agnostic** | No hardcoded provider. The app uses `instructor` via `core/llm_client.py`. | Direct imports of `openai`, `google.genai`, `anthropic`, `groq`, `vertexai`, etc. outside `core/llm_client.py` fail. `import anthropic` is forbidden repo-wide. |
| 3 | **Dashboard = vanilla HTML/CSS/JS** | No build step or framework dependency; the hub serves the dashboard statically. | `dashboard/` may not contain `package.json`, `node_modules`, `.jsx`/`.tsx`, or framework references. |
| 4 | **WebSocket localhost-only** | The hub is only reachable on the same machine. | `hub.py`/`hub_core.py` bind/CORS only to `localhost`/`127.0.0.1`; `0.0.0.0`, `::`, and wildcard CORS are rejected. |
| 5 | **Config via `.env` / YAML** | No hardcoded configuration values or secrets in application code. | This invariant is partially automated: the check rejects hardcoded API keys. The rest of the configuration defaults are reviewed manually (see "Non-automated rules"). |
| 6 | **`.vnx-data/` writes stay app-support-scoped** | `.vnx-data/` is a local runtime data directory (local audit log and session state) that only exists on the user's machine — in an installed app under `~/Library/Application Support/SalesCopilot/`, not in shared or external storage. App code introduces no new writes there outside the app-support-scoped creation. | Literal `.vnx-data` in `src/` is forbidden, except in the five legacy modules and the app-support-scoped directory creation in `src/sales_copilot/core/paths.py::ensure_app_support_tree()`. |

## Privacy/cloud spectrum (product positioning)

Invariants 1 and 2 together define a spectrum, not an on/off switch:

1. **Local**: maximum privacy, nothing leaves the device. Default for sensitive/regulated conversations (invariant 1).
2. **BYO-tenant cloud**: the user's own Azure/Vertex/Bedrock tenant and keys, via `core/llm_client.py` (invariant 2). Also cloud, but within boundaries organizations already accept: data stays in their own, managed tenant.
3. **Public cloud**: a public API (e.g. OpenAI, OpenRouter), likewise allowed via `core/llm_client.py` (invariant 2), opt-in when the sensitivity of that specific conversation allows it.

The choice is made **per conversation**, not per customer and not once at installation: the same user can pick tier 1 for a sensitive customer conversation and tier 3 for an internal sales training. Privacy stays the default; cloud is a deliberate, explicit choice, never the silent default. See also `docs/PRIVACY.md` (configurable privacy tiers) and the root `CLAUDE.md` (product positioning).

## Deep-insight lane outbound class (revised 2026-08-04)

Invariant 1 originally stated a single outbound class: *only short, PII-redacted LLM fragments may leave the device.* That holds for the detection and coaching tracks (tracks 1 and 2). The deep-insight lane (track 3) is a deliberate, documented second outbound class and is not smuggled in as a footnote exception.

**What leaves the device under this class.** The full session transcript — not a sliding window, not a fragment — plus prep-docs and, when opted in, earlier transcripts of the same customer. All of it is PII-redacted through the existing `core/outbound_policy.py` seam before it leaves. The destination is an operator-configured frontier or enterprise-grade endpoint: a BYO-tenant cloud (Azure OpenAI / Vertex / Bedrock), a public frontier API, or an MCP host the operator connects. The lane never runs against a local-only provider; that is by design, not a gap.

**Three hard conditions, all required:**

1. **Opt-in per conversation.** The lane is off by default and must be turned on for the specific conversation. The same user can run one call fully local (tracks 1/2 only) and route the next through the deep lane.
2. **Default off.** No installation, no preset, no license tier silently enables the deep lane. If the operator has not explicitly opted in for this conversation, invariant 1 behaves exactly as before: only short fragments may leave.
3. **Through the existing outbound_policy seam.** Every byte the lane sends goes through `core/outbound_policy.py` (`apply_outbound_pii` / `sanitize_for_outbound`). PII redaction applies on the exit, with the same `cloud_only` default the rest of the system uses. The deep lane adds a new *destination class*, never a new *policy path*.

This revision does not weaken any other invariant. Raw audio, session recordings, and embeddings still never leave the device. The provider-agnostic and `no-anthropic-sdk` rules (invariant 2) are untouched: the deep lane speaks through `core/llm_client.py` or through the MCP protocol (JSON-RPC), never through a vendor SDK imported elsewhere. With the lane off, the system is byte-for-byte identical to the pre-revision behavior.

**The MCP host as a destination, in both directions.** When the operator connects an MCP host, the bridge (`src/sales_copilot/mcp_bridge/`) is the exit. Its three read tools — `get_session_brief`, `get_transcript`, `get_detections` — are the outbound direction and carry the class described above, redacted through `core/outbound_policy.py` on the way out. An MCP host counts as a public-cloud destination even when it runs on the same machine, so the redaction is not conditional on the host being local.

`push_insight` is the opposite direction: it originates on the host and lands on the local insights channel. Nothing about it is outbound, so redaction does not apply to it; it is validated against a schema and rate-capped per session instead, and each row it produces is labeled with its origin so an operator can always tell a host-generated insight from an engine-generated one. Invariant 4 still holds throughout: the bridge reaches the hub as a localhost WebSocket client, and the hub binds nowhere else.

Design source: `claudedocs/2026-08-04-track3-deep-lane-mcp-plan.md`. The lane, its engine, its dashboard surface, and the MCP bridge all shipped between 2026-08-05 and 2026-09-05 (#169-#178, #202); this document states the invariant they satisfy, and `docs/MCP_BRIDGE.md` documents the bridge itself.

## Trigger delivery outbound class (added 2026-09-06)

A third, independent outbound class: the customer buys OnCue as a *trigger* — local
capture and transcription, and when the call ends the finished report can be handed
to their own automation. `src/sales_copilot/modules/reports/delivery.py` implements
two optional, independent sinks: a directory (including a mounted network share) and
an HTTP endpoint the operator names. Full contract: `docs/MODULE4.md` ("Report
Delivery").

**This class deliberately does NOT go through `core/outbound_policy.py`.** That seam
exists to redact PII before text reaches an LLM/AI provider (the deep-insight lane
above, or any `core/llm_client.py` call). This feature's destination is never an LLM
— it is a directory the customer owns or an endpoint the customer configured, and the
entire point is handing their own automation their own words verbatim so it can act
on them (extract a name, a company, a deal detail for CRM sync). Redacting PII by
default here would defeat the feature the customer is paying for. Redaction for this
payload is instead controlled by the existing `REPORT_REDACT_PII` flag — the same one
already governing the local `data/reports/` copy — so there is exactly one redaction
decision for this data, not two independently-configured ones that could disagree.

**Same three conditions as the deep-insight lane, adapted:**

1. **Both sinks off by default.** `REPORT_DELIVERY_DIR` and `REPORT_DELIVERY_ENDPOINT`
   are unset until the operator configures them. A fresh install writes only the local
   report, byte-for-byte as before.
2. **The code cannot distinguish a LAN destination from a public one**, and does not
   try to. A directory on a mounted network share is, in practice, the customer's own
   infrastructure; an HTTP endpoint could be `http://192.168.1.50:5678/webhook` (an
   n8n instance on the LAN) or a public internet host — the configuration format is
   identical either way. This is an honest limitation, not a gap: the operator is
   trusted to point the destination at infrastructure they control, the same trust
   boundary as a BYO-tenant cloud provider.
3. **Local copy is unconditional and independent of delivery outcome.** The
   `data/reports/` write in `generator.generate_report()` happens before either sink
   is attempted and is never rolled back or rewritten by a delivery failure — a failed
   delivery is loud (a logged `ERROR` naming the local report path) and recoverable
   (redeliver manually once the destination is reachable again), never a silently
   dropped transcript.

**Network-client allow-list.** `src/sales_copilot/modules/reports/delivery.py` uses
`urllib.request` for the HTTP sink — the same stdlib-only idiom already used by
`core/measurement_signals.py` and `auth/revocation_cache.py` for simple,
config-driven, timeout-bounded outbound POSTs. It is listed in
`_NETWORK_ALLOWLIST` in `scripts/check_architecture_boundaries.py`.

## How the check works

`scripts/check_architecture_boundaries.py` is a deterministic, grep/AST-based gate with no external dependencies. It scans the following paths relative to the repo root:

- `src/` — application code.
- `scripts/` — scripts (only for the provider-SDK rule).
- `dashboard/` — frontend.
- `.github/workflows/ci.yml` — CI integration.

For every violation the script prints a concrete message (`[rule] path:line: ...`) and returns exit code `1`. If everything is green, it returns `0`.

## Marking deliberate exceptions

There are two ways to mark an exception:

1. **Per line** — add a suppression comment on the relevant line:
   ```python
   import some_cloud_sdk  # architecture-boundary-ignore
   ```
   Use this sparingly; every suppression must be explained in the PR.

2. **Per file / library** — add a file to the relevant allow-list in `scripts/check_architecture_boundaries.py`. This is the preferred way for well-argued, recurring exceptions (for example a new module that talks locally to the hub via `websockets`).

3. **Legacy exemptions** — the following files are exempted for the `.vnx-data/` rule because they contain historical hardcoded paths. New `.vnx-data` references in other files fail:
   - `src/sales_copilot/core/session_store.py`
   - `src/sales_copilot/core/audit_ledger.py`
   - `src/sales_copilot/auth/startup_check.py`
   - `src/sales_copilot/auth/email_capture.py`
   - `src/sales_copilot/websocket/hub_auth.py`

4. **App-support-scoped `.vnx-data` creation** — `src/sales_copilot/core/paths.py::ensure_app_support_tree()` creates the subdirectory `.vnx-data/audit` under `resolve_app_support()`. In a frozen `.app` that is `~/Library/Application Support/SalesCopilot/.vnx-data/`, not the repo-root governance directory. The check exempts this specific use within `ensure_app_support_tree()`; all other `.vnx-data` references in `src/` remain forbidden.

## Non-automated rules

Some invariants cannot be reliably checked in full without false positives:

- **Config defaults in dataclasses** — `src/sales_copilot/core/config.py` contains safe default values for env variables. An automatic check on "all literals" would be a false positive here. The check therefore limits itself to hardcoded secrets/API keys.
- **Semantics of network payloads** — the check does not know whether a new `httpx.post()` sends transcript/audio or only does a local health check. That is why network-client imports are put on an allow-list, so every new use is deliberately reviewed.

## CI integration

The check runs as the last step in `.github/workflows/ci.yml`:

```yaml
- name: Architecture boundary check
  run: |
    source .venv/bin/activate
    python scripts/check_architecture_boundaries.py
```

Run locally:

```bash
python scripts/check_architecture_boundaries.py
```

With a different root:

```bash
python scripts/check_architecture_boundaries.py --root /path/to/checkout
```
