# Architecture boundaries (enforced)

This document describes the hard architecture invariants of the OnCue project. They are not meant as a "guideline": CI runs `scripts/check_architecture_boundaries.py`, so a PR that crosses a boundary turns red.

## Invariants

| # | Invariant | Why | Check |
|---|---|---|---|
| 1 | **Local-first data plane** | Audio, transcript, embeddings, session DB, and case DB stay on the device. Only short, PII-redacted LLM fragments may leave. | Provider SDKs may only be imported in `core/llm_client.py`. Network-client imports in `src/` are on an allow-list; new network code must be deliberately reviewed. |
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
