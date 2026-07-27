# Scripted Sales Call (DE) - Short Route Verification

Use this for PR-77 language routing smoke test with `CALL_LANGUAGE=de`.

Set:

```bash
sed -i '' 's/^CALL_LANGUAGE=.*/CALL_LANGUAGE=de/' .env
```

Dialog:

S: Danke fuer Ihre Zeit. Ich moechte verstehen, wo Ihr Team aktuell Reibung hat.
P: Uns fehlt Kapazitaet und der Angebotsprozess dauert zu lange.
[EXPECT] pain_point: kapazitaet, angebotsprozess

S: Wo ist der groesste operative Aufwand?
P: Wir arbeiten noch zu viel manuell und unsere Daten sind inkonsistent.
[EXPECT] pain_point: manuelle_arbeit, datenqualitaet

S: Wie funktioniert Ihr Reporting heute?
P: Berichte dauern zu lange und wir haben keinen klaren Ueberblick.
[EXPECT] pain_point: reporting

S: Was haelt Sie von einer Umsetzung ab?
P: Der Preis ist aktuell zu hoch.
[EXPECT] objection: preis

S: Ist auch der Zeitpunkt schwierig?
P: Ja, wir wollen das erst im naechsten Quartal angehen.
[EXPECT] objection: timing

S: Waere ein kleiner Pilot fuer Sie denkbar?
P: Ja, aber meine Vorgesetzten und Finance muessen zustimmen.
[EXPECT] objection: entscheidungsbefugnis

S: Dann planen wir die naechsten Schritte gemeinsam.
[EXPECT] phase: closing

Pass criteria:
- DE categories appear (not NL or EN).
- Response templates are in German.
- Transcriber `Language:` line shows `de`.
