# DPIA Recruitment Mode

**Status:** Draft v1.0
**Date:** 2026-05-16
**Author:** Vincent van Deth
**Location:** `docs/DPIA_RECRUITMENT.md`

| Version | Date | Change | By |
|---|---|---|---|
| 1.0 | 2026-05-16 | Initial document | Vincent van Deth |
| - | 2027-05-16 | Planned annual review | - |

---

## §1 Purpose of this DPIA

This Data Protection Impact Assessment has been prepared in accordance with GDPR Art. 35. That article requires a DPIA when a processing operation "is likely to result in a high risk to the rights and freedoms of natural persons". Recruitment meets three of the nine criteria listed by the Dutch Data Protection Authority (Autoriteit Persoonsgegevens): large-scale processing of special categories of personal data (GDPR Art. 9), systematic monitoring of individuals, and use of new technology.

Additionally, the EU AI Act (Regulation 2024/1689) classifies recruitment AI as high-risk under Annex III, §4. This requires conformity documentation, transparency to data subjects, and structural human oversight (Art. 14 AI Act).

The purpose of this document is to demonstrate that OnCue recruitment mode has an acceptable residual risk after implementing the measures described. This document is not legal advice. The organization that deploys the software is the data controller and remains responsible for GDPR compliance itself. Legal review by a data protection officer (DPO) or external lawyer is recommended for any recruiter with more than ten candidates per month.

---

## §2 Data controller and processor

The **data controller** is the recruiter or recruitment agency that deploys OnCue. They determine the purpose and means of the processing: who is recorded, how long files are retained, and which candidates are in the system.

**OnCue (software)** is not a processor within the meaning of GDPR Art. 4(8) in a self-hosted setup. The software runs locally on the recruiter's machine. There is no central server to which personal data is sent. In that configuration, the software is a technical tool, comparable to a local word processor.

The **LLM provider (Vertex AI / Gemini / Groq)** is a sub-processor under GDPR Art. 28. The provider receives redacted transcript fragments of 1-3 sentences for classification. No raw audio, no full transcripts, no name or contact details thanks to the active PII filter. A data processing agreement (DPA) with the LLM provider is mandatory for any business use. All named providers offer a standard DPA.

**Vincent van Deth as supplier** is not a processor in a self-hosted deployment: I have no access to the recruiter's data. With the Pro tier and central audit logging this changes: then I am a processor and I provide a DPA. See `docs/SCOPE_STATEMENT.md` for the DPA request procedure.

---

## §3 Nature, scope, and context of the processing

**Nature of the processing**

OnCue performs three processing activities during a recruitment conversation:

1. Speech-to-text transcription (local, via whisper.cpp)
2. Pain point, buying signal, and objection detection on redacted fragments (optionally via LLM)
3. Summary and audit trail after the conversation (local, NDJSON)

**Scope**

The default configuration processes, per session: one recruiter, one candidate, one conversation. There is no default aggregation across multiple candidates. The system keeps no cross-references across candidate profiles.

**Context**

OnCue is deployed during telephone or video intakes. The candidate is informed of the tool's use via the consent script in §8. The purpose is solely to support the recruiter: better notes, flagging of conversation themes, and a structured summary afterward.

**Purpose**

The system supports the recruiter with documentation. It generates no scores, rankings, or fit statements about candidates. All of the system's recommendations are informative, not decisive. GDPR Art. 22 (prohibition on solely automated decision-making) applies: the system is explicitly out of scope for any automated decision.

**Data categories**

| Category | Type | Sensitivity |
|---|---|---|
| Speech content (audio) | Special category (biometric potential, GDPR Art. 9) | High |
| Transcript | Regular personal data + possibly special category | Medium-high |
| Detected pain points/buying signals | Derived data | Medium |
| PII after redaction | Pseudonymized | Low |
| Audit NDJSON | Metadata + redacted fragments | Low |

**Sources**

Audio is captured via the device microphone (recruiter speech) and system audio via AudioTee (candidate speech) — a process tap on the meeting app, detected automatically by default, without a virtual audio device. BlackHole is the fallback when that detection is not unambiguous. There is no webcam processing. No screen recording. No cursor tracking.

---

## §4 Necessity and proportionality

**Necessity**

During fast intake conversations, recruiters miss relevant signals: salary expectations, doubts about the client company, preference for hybrid work. Without a tool, this leads to incomplete records and missed opportunities for candidate and recruiter. The tool addresses this through real-time flagging and structured summaries.

**Proportionality**

The processing is proportionate because:

- Audio does not leave the machine (local-first architecture)
- The PII filter is active with the recruitment preset (`apply_pii_filter=True`): BSN, phone number, IBAN, email address, postal code, and date of birth are replaced with placeholders before any LLM processing
- The LLM receives at most 1-3 sentences per classification call, not the full transcript
- Audio recording can be disabled via `RECORD_AUDIO=false` in `.env`

**Minimization (GDPR Art. 5(1)(c))**

The audit ledger in the Pro tier stores redacted fragments, not raw transcript snippets. Candidate names are not stored in the audit log unless the PII filter does not recognize them (names are not a regex pattern; GLiNER NER is the additional layer).

**Retention periods**

By default 30 days for session files in `data/sessions/`. Configurable via `.env` (retention script in sprint+1). Transcripts and audio can also be deleted earlier at the candidate's request (see §7).

---

## §5 Risk analysis

**R1 Unwarranted conclusions from incorrect classification**

Risk: the system detects a buying signal or objection that was not there. The recruiter acts on erroneous information.

Measure: the system presents detections as hypotheses, not as facts. The recruiter retains all decision-making power. No automated action is tied to detection. Residual risk after measure: low.

**R2 Transcript leakage via unencrypted SQLite**

Risk: a third party gains access to `data/sessions/` or the SQLite database via a stolen device or unauthorized access.

Measure: enable FileVault disk encryption on the recruiter's machine (operational recommendation, not enforceable by software). `.gitignore` contains `data/` and excludes uploads of session data. Residual risk without FileVault: medium. With FileVault: low.

**R3 LLM call data leakage to a third-party provider**

Risk: candidate data is sent to an external LLM provider without a DPA or outside the scope of the DPA.

Measure: PII filter active pre-LLM-call (F01 fix 2026-05-17: the code previously sent the original text, now redacted text). Only redacted fragments are sent to the router and LLM. DPA obligation on the recruiter. Fully local use possible via Ollama. Residual risk after fix: **LOW**.

**R4 Bias in pain point classification against candidates with an accent or dialect**

Risk: Whisper transcribes candidates with a non-standard Dutch accent less accurately, leading to missed signals or incorrect classifications.

Measure: recruiter training on the limitations of speech recognition. Human oversight of all summaries. Monitoring of transcription quality as part of onboarding. Residual risk: medium (technically unavoidable with ASR systems).

**R5 Identification without consent via voice biometrics**

Risk: diarization technology links speech to a speaker profile without the candidate's explicit consent.

Measure: diarization is opt-in per conversation (ADR-SPEAKER-ENROLLMENT). Default setting: no speaker enrollment. The recruitment preset leaves diarization disabled unless explicitly enabled. Residual risk: low with the default configuration.

**R6 GDPR Art. 22 violation through automated decision-making**

Risk: the system generates a "fit" score or recommendation that is implicitly used as a decision.

Measure: the system generates no scores, rankings, or fit statements. The scope statement (§8 of this document, plus `docs/SCOPE_STATEMENT.md`) is explicit about this boundary. Verifiable via code review of `config/pain_points.yaml` and the output schemas in `docs/TTD.md`. Residual risk: low.

**R7 Audit trail tampering**

Risk: the NDJSON audit log is altered after the fact to conceal which classifications were made.

Measure: NDJSON append-only writing. HMAC-SHA256 signing per record via the `AUDIT_HMAC_SECRET` env var — **IMPLEMENTED**. Tamper detection via `verify_audit_ledger()`. Residual risk: **LOW after HMAC implementation**.

---

## §6 Measures

**Technical measures**

- PII filter active with the recruitment preset: regex patterns for BSN, phone number, IBAN, email, postal code, date of birth
- PII redaction is **now actually applied** before every LLM call: `pipeline.py` and `window_classifier.py` send `clean` (redacted text) to the router and LLM — **F01 FIX IMPLEMENTED (2026-05-17)**
- WS hub endpoints secured with the `X-Sales-Copilot-Token` header; CORS restricted to localhost — **F02 + F04 FIX IMPLEMENTED (2026-05-17)**
- SALES_COPILOT_LICENSE_SECRET placeholder fallback removed; startup fails if the secret is missing or a placeholder — **F03 FIX IMPLEMENTED (2026-05-17)**
- Audit ledger NDJSON append-only (write mode `a`, never `w`) — **IMPLEMENTED**
- HMAC-SHA256 signing per audit record via `AUDIT_HMAC_SECRET` — **IMPLEMENTED**
- GDPR Art. 17 purge-session CLI (`scripts/purge_session.py`) for the right to erasure — **IMPLEMENTED**
- `.gitignore` excludes `data/`, `.env`, and `*.wav` from version control
- No automated decision-making or scoring output
- Fully local operation available via `LLM_PROVIDER=ollama`

**Organizational measures**

- Recruiter onboarding (15 minutes): scope and limits of the system, consent script, PII filter explanation
- Conclude a DPA with the LLM provider for business use (Google, Groq, or others)
- Acknowledgment + opt-in script per candidate (template in §8)
- Document the retention policy: 30 days by default, recruiter-configurable

**Legal measures**

- OnCue Pro data processing agreement available on request (DPA request via `info@vincentvandeth.nl`)
- GDPR Art. 9 legal basis: explicit candidate consent via the consent script
- AI Act declaration of conformity to follow with the official sprint+1 release
- External legal review recommended for recruiters with more than ten candidates per month

---

## §7 Rights of the data subject (candidate)

**Right of access (GDPR Art. 15)**

At the candidate's request, the recruiter provides the full transcript and the generated summary. Deadline: within 30 days of the request.

**Right to rectification (GDPR Art. 16)**

The recruiter corrects the transcript or summary in case of factual errors or incorrect PII redaction. This can be done directly in the session files in `data/sessions/`.

**Right to erasure (GDPR Art. 17)**

Complete session purge via CLI — **IMPLEMENTED**:

```bash
.venv/bin/python scripts/purge_session.py --session-id <UUID> --dry-run   # preview
.venv/bin/python scripts/purge_session.py --session-id <UUID> --confirm   # delete
.venv/bin/python scripts/purge_session.py --email <address> --confirm     # delete lead registration
```

The CLI removes SQLite records (sessions, detections, and call_sessions),
transcript/audio/metadata files, reports, linked context uploads,
NDJSON audit lines, and optionally Supabase records. Dry-run by default; `--confirm`
is required for actual deletion. Bulk purge by date is possible via
`--before-date YYYY-MM-DD`.

**Right to object (GDPR Art. 21)**

The candidate can indicate during the conversation that they object to recording. The recruiter disables recording via `RECORD_AUDIO=false` and restarts the system. Detection and transcription continue via in-memory buffers without persistent storage.

**Right to data portability (GDPR Art. 20)**

JSON export of session data is planned for sprint+1. Until then: the recruiter manually exports the files in `data/sessions/<session_id>/`.

---

## §8 Consent template for candidates

**Spoken script (recruiter reads aloud at the start of the conversation):**

> "In preparation for this conversation, I use an AI tool that listens along to take notes. The audio stays on my computer and is automatically deleted after 30 days. The tool does not store any recording with an external party. Do you agree to this?"

**Written confirmation (optional, by email before the conversation):**

> "I want to let you know that during this conversation I use OnCue, an AI tool that helps with taking conversation notes. The tool runs locally on my computer. Audio and transcription are not sent to external parties, except for short anonymized fragments to my AI provider for keyword detection. All data is deleted after 30 days. You always have the right to access or have your data deleted. Do you have questions or objections? Let me know before or during the conversation."

**Tip:** mention the name of the tool explicitly ("OnCue") in the communication. This ensures traceability in the event of a complaint to the Autoriteit Persoonsgegevens (the Dutch DPA).

---

## §9 Residual risk assessment

With all measures from §6 implemented, the residual risk of OnCue recruitment mode is **low**.

Without the measures, the residual risk is **unacceptable**. This implies that the software may not be deployed in a recruitment context until at least the PII filter, the consent procedure, and the DPA with the LLM provider have been implemented.

The two open risks that reach medium (R4 accent bias, R7 without HMAC) are addressed in sprint+1. After sprint+1, the expected residual risk on all seven axes is low.

---

## §10 Release and review

**Released by:** Vincent van Deth (DPIA owner)
**Release date:** 2026-05-16
**Next planned review:** 2027-05-16 or earlier upon a significant feature change (new data category, new LLM provider, new audit mechanism).

**External legal review:** recommended for any recruiter with more than ten candidates per month. An internal DPO or external privacy lawyer can use this document as a basis for a formal DPIA in accordance with GDPR Art. 35.

**Document location:**

- This repo: `docs/DPIA_RECRUITMENT.md`
- Per-client DPA extract: on request via `info@vincentvandeth.nl`
- Scope statement (public): `docs/SCOPE_STATEMENT.md`
