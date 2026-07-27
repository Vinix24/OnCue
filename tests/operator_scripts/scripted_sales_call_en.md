# Scripted Sales Call (EN) - Short Route Verification

Use this for PR-77 language routing smoke test with `CALL_LANGUAGE=en`.

Set:

```bash
sed -i '' 's/^CALL_LANGUAGE=.*/CALL_LANGUAGE=en/' .env
```

Dialogue:

S: Thanks for joining. I want to understand where your team is losing time today.
P: We are overloaded and creating quotes takes too much time.
[EXPECT] pain_point: capacity, quote_process

S: Where does this affect your pipeline most?
P: We do too much manual work and our CRM data is inconsistent.
[EXPECT] pain_point: manual_work, data_quality

S: How is reporting to management?
P: Reports take days and we lack clear visibility.
[EXPECT] pain_point: reporting

S: What is your biggest concern about moving forward?
P: Price. This feels too expensive for us.
[EXPECT] objection: price

S: Understood. Is timing also a constraint?
P: Yes, not this quarter.
[EXPECT] objection: timing

S: Would a small pilot with clear ROI metrics be realistic?
P: Yes, if finance and my manager can review it.
[EXPECT] objection: authority

S: Great, then we will schedule next steps and define scope.
[EXPECT] phase: closing

Pass criteria:
- EN categories appear (not NL or DE).
- Response templates are in English.
- Transcriber `Language:` line shows `en`.
