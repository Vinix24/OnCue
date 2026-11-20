# Architecture decision records

This folder holds one architecture decision per file, written in Nygard format (Context, Decision, Alternatives considered, Consequences) and numbered in the order they were accepted. A record that is superseded stays in the folder and points to its successor, so the history of a decision remains readable.

## Index

- [ADR-001: Recording bar as a separate macOS app](ADR-001-recording-bar-window.md)
- [ADR-002: One timeline for a recording, with pause and markers](ADR-002-recording-timeline-pause-markers.md)
- [ADR-003: Detect a call by microphone use per process](ADR-003-call-detection-by-microphone-use.md)
- [ADR-004: Consent, one route at a time, and the prompt lifecycle](ADR-004-consent-and-one-route-at-a-time.md)
- [ADR-005: One recording per call, the notetaker first and the assistant as a layer](ADR-005-one-recording-notetaker-first.md)
- [ADR-006: macOS .dmg packaging, build source, signing and updates](ADR-006-macos-dmg-packaging.md)
- [ADR-007: Autostart layers per license and three opt-in macOS permissions](ADR-007-autostart-layers-and-permissions.md)
- [ADR-008: The Windows capture route follows the call's endpoint](ADR-008-windows-capture-follows-endpoint.md)
- [ADR-009: Which LLM tasks may run on which privacy profile](ADR-009-llm-task-set-per-privacy-profile.md)
