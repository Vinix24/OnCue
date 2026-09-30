# OnCue: Doc Index

**Last update:** 2026-09-05 (observability round: detector, MCP bridge and chain preflight documented; log paths corrected)

Documentation index for the OnCue project. Start here if you don't know the docs yet.
Docs are organized by **who you are**, not alphabetically: pick your tier below.

---

## Tier 1 — Prospect / evaluator

You're deciding whether OnCue fits your use case, team, or compliance posture.

| Document | What you'll find | Language |
|---|---|---|
| [FUNCTIONAL_SPEC.md](FUNCTIONAL_SPEC.md) | What OnCue does from a user's point of view, no technical detail | NL |
| [USE_CASES.md](USE_CASES.md) | Usage scenarios: sales, coaching, recruitment, training | NL |
| [FEATURES.md](FEATURES.md) | Canonical Free vs Pro tier matrix | EN |
| [PRIVACY.md](PRIVACY.md) | Privacy, GDPR and AI Act: data flows, processor agreement, architecture | NL |
| [SCOPE_STATEMENT.md](SCOPE_STATEMENT.md) | Canonical public scope statement: what the system does and does not do (for prospects/HR) | NL |

---

## Tier 2 — Operator

You're installing, configuring, running calls, or supporting a team that does.

| Document | What you'll find | Language |
|---|---|---|
| [BETA_TESTER_GUIDE.md](BETA_TESTER_GUIDE.md) | Onboarding for invited beta testers: install, first use, what to test | NL |
| [SETUP_EXPECTATIONS.md](SETUP_EXPECTATIONS.md) | Honest picture of what setup costs, including phone-call routing | NL |
| [FAQ.md](FAQ.md) | 60+ operator questions and answers | NL |
| [FAQ_SEARCH_INDEX.md](FAQ_SEARCH_INDEX.md) | Quick-scan list (Cmd+F) that jumps into the full FAQ.md entries | NL |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Canonical step-by-step fixes for setup and runtime problems | NL |
| [CONFIG.md](CONFIG.md) | Dashboard setup screen and the `start_call` config it sends | EN |
| [LICENSE_KEY.md](LICENSE_KEY.md) | License key formats, verification flow, revocation; entitlement table canonical in LICENSE_PRO_CONTRACT.md | EN |
| [PRESETS.md](PRESETS.md) | Domain presets: sales / coach / recruitment, configuration and extension | NL |
| [RECRUITMENT_PROFILE.md](RECRUITMENT_PROFILE.md) | Recruitment-preset detail: routes, PII filter, how to use it | NL |
| [DIARIZATION.md](DIARIZATION.md) | Multi-speaker diarization via pyannote v3: shadow mode vs production | EN |
| [DPIA_RECRUITMENT.md](DPIA_RECRUITMENT.md) | Data Protection Impact Assessment template for recruitment-mode (AI Act Annex III) | NL |
| [AUDIT_LEDGER.md](AUDIT_LEDGER.md) | Audit ledger: NDJSON log + HMAC signing for AI Act Annex III compliance | NL |

---

## Tier 3 — Contributor

You're reading or changing the code, and need the architecture, contracts, and test surface.

| Document | What you'll find | Language |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System overview: layers, mermaid diagram, dataflow, design decisions, security posture | EN |
| [ARCHITECTURE_BOUNDARIES.md](ARCHITECTURE_BOUNDARIES.md) | Hard architecture invariants, enforced by CI (`check_architecture_boundaries.py`) | NL |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Audio routing (AudioTee/BlackHole/calltap/single-stream), LLM providers, extensions | EN |
| [MODULE1.md](MODULE1.md) / [MODULE1_CONTRACT.md](MODULE1_CONTRACT.md) | Module 1 (Talk Time Coaching): narrative reference / normative API contract | EN |
| [MODULE2.md](MODULE2.md) / [MODULE2_CONTRACT.md](MODULE2_CONTRACT.md) | Module 2 (Live Transcriber): narrative reference / normative API contract | EN |
| [MODULE3.md](MODULE3.md) | Module 3 (Pain Point Detection + Slide Injection) | EN |
| [MODULE4.md](MODULE4.md) | Module 4 (Post-Call Reports) | EN |
| [REPLAY_TESTING.md](REPLAY_TESTING.md) | Replay harness for audio-fixture tests without a live microphone | EN |
| [BENCHMARKS.md](BENCHMARKS.md) | Local-model shootout: which model per job, measured latency | EN |
| [SESSION_PERSISTENCE.md](SESSION_PERSISTENCE.md) | SQLite-backed session state for crash recovery | NL |
| [SHARED_INFERENCE_QUEUE_DESIGN.md](SHARED_INFERENCE_QUEUE_DESIGN.md) | Design: shared inference queue for transcription (PR-102) | EN |
| [WHISPER_CPP_DESIGN.md](WHISPER_CPP_DESIGN.md) | Design: whisper.cpp portable transcription backend | EN |
| [FINETUNING_GUIDE.md](FINETUNING_GUIDE.md) | Fine-tuning Whisper for Dutch sales vocabulary | EN |
| [DEPENDENCIES.md](DEPENDENCIES.md) | Vendored dependencies: `vendor/audiotee/` tracked MIT source, whisper.cpp submodule, pin rationale | EN |
| [LICENSING.md](LICENSING.md) | AGPL-3.0 + dual-license rationale, CLA structure (recommendation, not final) | NL |
| [assets/README.md](assets/README.md) | How to produce the README screenshot/GIF assets | EN |
---

## Doc-status definitions

- **Current**: reflects the actual state of the codebase.
- **Foundational**: written at project start; requirements stable, details evolve.
- **Design doc**: written for or during a specific PR. Describes the intended design; implementation may differ in detail.
- **Component reference**: API contract and evidence document for a specific module.
- **Sprint+1 staging**: design document; not yet implemented.
