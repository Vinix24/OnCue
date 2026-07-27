# Domain presets

> **OSS export:** only the free `sales` preset ships in the public repository.
> The vertical starter-packs (`coach`, `recruitment`, `acquisitie`) are Pro
> content and ship with the Pro distribution. Requesting an absent Pro preset
> fail-softs to `sales` with a startup warning.

Presets adapt the detector to a specific conversation type. Each preset defines:
- `ui_labels` — how conversation participants are named in the dashboard
- `pain_points` — routes with utterances for semantic matching
- `objections` — objection patterns
- `buying_signals` — buying signal patterns
- `doubts` — doubt patterns
- `system_prompt_addendum` — extra context for the LLM

## Activating

Set the `PRESET` environment variable when starting:

```bash
PRESET=coach .venv/bin/python -m sales_copilot
PRESET=sales .venv/bin/python -m sales_copilot       # default
PRESET=recruitment .venv/bin/python -m sales_copilot
```

Without `PRESET`, the system falls back to the `sales` preset.

---

## Sales (default)

Sales conversations between a seller and a prospect (B2B).

**UI labels:** prospect / verkoper

**Pain points (12):** offerteproces, capaciteit, cross_sell, lead_generation,
kennisontsluiting, klantenservice, handmatig_werk, data_kwaliteit, rapportage,
onboarding, compliance, kosten

**PII filter:** off

---

## Coach

Coaching conversations between a coach and a client. Designed together with
**Frank (MyOffice Almere)** as design partner for the Coach Pack.

**UI labels:** klant / coach

**PII filter:** off (coaching data stays local, no PII redaction required)

### Pain points (8)

| Name | Label | Example utterance |
|---|---|---|
| `planning` | Planning en prioriteiten | "alles lijkt urgent maar niets is prioriteit" |
| `motivatie` | Motivatie en energie | "de vonk is er een beetje uit" |
| `accountability` | Accountability en follow-through | "uitstelgedrag is mijn grootste probleem" |
| `doelgerichtheid` | Doelgerichtheid en richting | "ik weet niet meer wat ik wil" |
| `weerstand` | Weerstand en patronen | "ik herken hetzelfde patroon steeds opnieuw" |
| `financiele-druk` | Financiele druk en geldzorgen | "de cashflow is een probleem" |
| `relationele-stress` | Relationele stress en spanning | "communicatie ligt moeilijk" |
| `gezondheid-balans` | Gezondheid en balans | "ik ben bang voor een burn-out" |

### Objections (4)

`investering`, `tijd`, `twijfel_effect`, `partner-bezwaar`

### Buying signals (5 routes)

`vraag-naar-implementatie`, `vraag-naar-traject`, `vraag-naar-tarieven`,
`vraag-naar-referenties`, `vraag-naar-aanpak`

### Approach principle

The coach preset follows the principle that a coach does not give advice but asks
questions and observes patterns. The system detects only what the client literally
names — no interpretations of underlying causes.

---

## Recruitment

Candidate conversations between a consultant and a candidate. PII filter active
(BSN, phone, IBAN, dates of birth are redacted).

**UI labels:** kandidaat / consultant

**PII filter:** on

**Pain points (7):** functie_fit, salarispens, doorstroom, locatie_flexibiliteit,
twijfel_werkgever, contractduur, secondary_benefits

**Buying signals (4 routes):** startdatum_vraag, verdieping_rol, bezwaar_opgelost,
interesse-in-startdatum

**Scope statement:** the system does not score or rank candidates.
Assessment remains solely with the human consultant (EU AI Act Annex III).

---

## Adding a new preset

1. Create `config/presets/<name>.yaml` with all required fields:
   `ui_labels`, `pain_points`, `objections`, `buying_signals`, `doubts`,
   `system_prompt_addendum`
2. Add tests to `tests/test_preset_loader.py` and
   `tests/test_preset_integration.py`
3. Document the preset in this file
