"""HubSpot CRM integratie -- skeleton voor sprint+1.

Push contact, deal, en timeline-activity naar HubSpot na een sales-call.
Zie claudedocs/ADR-CRM-HUBSPOT.md voor design-rationale.

Sprint+1 implementation plan (5 PRs):
1. OAuth-flow + macOS Keychain token storage
2. Contact + deal upsert (idempotent op email + title)
3. Timeline-activity met transcript-summary
4. Setup-script voor custom HubSpot properties
5. Dashboard-button + CLI command

Public API (sprint+1):
    push_session(session_id: str, *, dry_run: bool = False) -> PushResult
    setup_custom_properties() -> None
    revoke_token() -> None
"""
