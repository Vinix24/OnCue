# Recruitment Profile

Domain preset for conversations between recruiters/consultants and candidates.

## What the profile does

The `recruitment` preset adapts OnCue for recruitment conversations. Labels, pain points, buying signals, and objections are tuned to the conversation between a consultant and a candidate in recruitment and selection.

The preset detects seven pain point categories:
- **Role fit** — candidate doubts the role match or level
- **Salary gap** — salary expectation versus offer
- **Progression uncertainty** — career path and growth after the assignment
- **Location/hybrid** — commute time, working from home, flexibility
- **Doubt about the employer** — culture, stability, reviews
- **Contract duration** — extension, too short a term
- **Secondary employment benefits** — training budget, lease car, pension

Buying signals are concrete signs of interest: start-date questions, going deeper into the role, and objections that the candidate relativizes themselves.

## AI Act scope statement

Canonical treatment (classification + the three grounds why recruitment mode falls outside Annex III high-risk): [SCOPE_STATEMENT.md §4](SCOPE_STATEMENT.md#4-ai-act-positie).

## PII filter

When the recruitment preset is loaded, `apply_pii_filter=True` is set. All text that runs through `preset.redact(text)` is redacted before it leaves the local environment.

Detected patterns:

| Pattern | Example | Replacement |
|---|---|---|
| BSN (9 digits) | `123456789` | `[BSN]` |
| Phone number | `06-12345678`, `+31 6 12345678`, `020-1234567` | `[TELEFOON]` |
| IBAN NL | `NL91ABNA0417164300` | `[IBAN]` |
| Email address | `naam@bedrijf.nl` | `[EMAIL]` |
| Postal code | `1234 AB` | `[POSTCODE]` |
| Date of birth | `01-02-1985` | `[DATUM]` |

Names are not caught by the regex filter. Use GLiNER NER as an additional layer for person and organization names (see `docs/PRIVACY.md`).

## Activating

Set the environment variable `PRESET=recruitment` before the server starts:

```bash
PRESET=recruitment .venv/bin/python -m sales_copilot
```

Or via `.env`:

```
PRESET=recruitment
```

Verification after start:

```python
from sales_copilot.core.preset import load_preset
p = load_preset("recruitment")
print(p.ui_labels)        # {'prospect': 'kandidaat', 'self': 'consultant', ...}
print(p.apply_pii_filter) # True
```
