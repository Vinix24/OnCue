# Use cases

OnCue is built for B2B sales conversations, but the architecture is broadly
usable for any type of conversation context where real-time transcription, coaching, and
post-call analysis are useful.

This document describes five concrete scenarios. Per scenario: what the tool does,
which legal context applies, which modules you use, and which
configuration differs from the default.

---

## 1. B2B sales calls (primary scenario)

**Who:** sales professional, account manager, or director who runs demos and discovery calls
with potential customers via Teams, Zoom, or Google Meet.

**What the tool does:**

- Transcribes the conversation in real time with speaker attribution
- Detects pain points from the standard 12 NL categories (quoting process, capacity,
  manual work, etc.) or your own configuration
- Shows the talk-time ratio per phase and warns on a monologue
- Generates follow-up question suggestions based on the conversation context
- Produces a post-call report with transcript, pain points, and statistics

**Legal context:**

This is the lowest-risk scenario for the AI Act. The tool coaches the seller;
no automated decisions are made about the prospect. Use it
as an internal coaching instrument. Inform the prospect at the start of the
conversation if you are recording.

**Modules:**

All modules on (default configuration).

**Configuration changes vs. default:**

None. The default `.env.example` is configured for this scenario.

**Useful files:**

- `config/pain_points.yaml`: adjust the pain point categories
- `config/objections.yaml`: adjust the objection categories
- `scripts/seed_cases.py`: add your own case studies to the database

---

## 2. 1-on-1 coaching and mentoring

**Who:** coach, mentor, manager who runs development conversations with employees
or clients.

**What the tool does:**

- Transcribes and writes up the conversation for reflection and follow-up
- Detects recurring themes and blockers that the employee names
- Shows the talk ratio: good coaching is listening, not talking
- Generates follow-up questions based on what the employee says
- Post-call report with transcript and key moments for the coaching file

**Legal context:**

In an employment relationship (a manager coaching an employee), the purpose of
recording must be clear and proportionate. Inform the employee.
Discuss together what the transcript will be used for and who can view it.
This is an internal HR instrument; the report does not go to third parties.

In a coaching context without an employment relationship (external coach, ICF-certified
coach), the professional-ethical standards of the coaching association apply alongside the GDPR.
Coaching content is usually confidential; explicitly discuss whether and how
recording fits the coaching relationship.

**Modules:**

- Talk-time: on (talk ratio is extra relevant)
- Detection: on, but adjust `config/pain_points.yaml` for coaching themes
  instead of sales pain points
- Suggestions: on

**Configuration changes:**

```bash
# in .env
DYNAMIC_SLIDES=false
```

Replace `config/pain_points.yaml` with coaching-relevant categories such as:
"stalled project", "working-relationship conflict", "uncertainty about direction",
"overload", "lack of feedback".

---

## 3. Recruitment intakes (high AI Act risk)

**Who:** recruiter, HR staff member, or headhunter who runs candidate intakes.

**What the tool does:**

- Transcribes the conversation for internal use and the candidate file
- Detects relevant themes (availability, salary indication, motivation)
- Generates a structured summary of the conversation afterwards

**Legal context (read this carefully):**

Recruitment and selection using AI systems falls under **Annex III
of the EU AI Act** as a high-risk category, as of 2 August 2026. Canonical
classification and scope rationale: [SCOPE_STATEMENT.md §4](SCOPE_STATEMENT.md#4-ai-act-positie).
Full risk analysis, obligations (DPIA, conformity assessment, human
oversight, transparency, consent, retention periods), and the consent script:
[DPIA_RECRUITMENT.md](DPIA_RECRUITMENT.md).

**Recommendation:** in this scenario set `LLM_PROVIDER=ollama` so that candidate data
does not leave the machine. The AI Act imposes requirements on data minimization.

**Modules:**

- Talk-time: optional
- Detection: on, with recruitment-specific routes

**Configuration changes:**

```bash
# in .env
LLM_PROVIDER=ollama        # fully local for maximum privacy
RECORD_AUDIO=true          # for file-building (apply your retention policy)
DYNAMIC_SLIDES=false
ONLY_CLASSIFY_PROSPECT=true
```

Replace `config/pain_points.yaml` with recruitment categories such as:
"availability", "salary indication", "motivation", "no-show risk",
"fit indicator".

**Not yet available in 0.9.0:** automatic PII filtering (BSN, IBAN, phone numbers)
in the transcription. If candidates mention such data, it is stored unfiltered
in the transcript. Add manual redaction to the post-call report
before it goes into a file.

---

## 4. Customer support conversations

**Who:** customer support staff member who handles complex complaints or technical questions
by phone or video.

**What the tool does:**

- Transcribes in real time for quick reference during the conversation
- Detects categories of complaints or questions relevant for escalation
- Generates a summary for the ticket or CRM afterwards
- Shows the talk ratio (support staff sometimes talk too much)

**Legal context:**

Similar to B2B sales: low risk for the AI Act if no automated
decisions are made about customers. Inform customers at the start that the
conversation is being recorded in line with the organization's privacy policy.

Customer service recordings are common and customers are usually familiar with them.
Make sure the notice "this conversation is being recorded" is technically and legally sound.

**Modules:**

- Talk-time: on
- Detection: on, with support-specific categories (escalation indicators,
  complaint categories, urgency)
- Summary: on (for the ticket update after the conversation)

**Configuration changes:**

Replace `config/pain_points.yaml` with support categories such as:
"technical problem", "invoice dispute", "escalation signal", "churn risk",
"feature request".

---

## 5. Qualitative research

**Who:** researcher, UX designer, consultant, or journalist who conducts in-depth interviews.

**What the tool does:**

- Transcribes the interview for analysis
- Detects recurring themes that you have configured as pain point routes
- Produces a transcript with timestamps for qualitative coding tasks
- Post-call summary as a starting point for analysis

**Legal context:**

Scientific research and journalism sometimes have exceptions to GDPR rules,
but they do not apply automatically. Standard GDPR norms apply:

- Consent of the respondent for recording and processing
- Purpose limitation: the transcript may only be used for the stated purpose
- Retention period appropriate to the research design

If the research processes special categories of personal data (health,
political opinions, sexual orientation), stricter rules apply.

**Recommendation:** set `RECORD_AUDIO=false` if you do not need an audio recording
and only want the transcript. This reduces the amount of stored data.

**Modules:**

- Talk-time: optional (can give insight into interview dynamics)
- Detection: on, with research-code-specific categories (themes from your
  research design)
- Summary: on

**Configuration changes:**

```bash
# in .env
RECORD_AUDIO=false          # transcript only, no audio
DYNAMIC_SLIDES=false
```

Replace `config/pain_points.yaml` with your research themes.

---

## Scenarios that fall outside scope (0.9.0)

The following scenarios need additional functionality that is not yet
available in 0.9.0:

- **Review of recordings by third parties** (quality control, legal investigation):
  this requires access control on `data/sessions/` and an audit trail.
- **Multi-user teams** where multiple staff members share their own sessions:
  this requires the Pro team dashboard.
- **Fully automated PII filtering** in the transcription: it is on the
  roadmap but not built into 0.9.0.
- **CRM synchronization**: it is on the Pro roadmap (HubSpot first, then Pipedrive
  and Salesforce).

---

## Further reading

- [PRIVACY.md](PRIVACY.md): full privacy and GDPR explanation
- [FUNCTIONAL_SPEC.md](FUNCTIONAL_SPEC.md): which modules are available
- [INTEGRATIONS.md](INTEGRATIONS.md): LLM providers and audio routing
