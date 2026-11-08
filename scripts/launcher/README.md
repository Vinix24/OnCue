# OnCue Launcher

## Starten

### Na de installatie
De installer (`scripts/bootstrap.sh`, de regel uit [INSTALL.md](../../INSTALL.md)) zet de app in `~/Applications/OnCue.app`. Open hem via Launchpad of dubbelklik in Finder. Er is geen Gatekeeper-stap: de app wordt op je eigen Mac gebouwd en niets komt via een browser binnen.

### Vanuit het Dock
Klik op `OnCue.app` (of `Start OnCue.app` in `scripts/launcher/` bij een checkout). Je krijgt een keuze:

- **Opnemen en uitschrijven** (standaard): neemt beide kanten van het gesprek
  op, met twee volume-meters en een meelopende timer, zodat je ziet dat zowel de
  tegenpartij als je eigen stem binnenkomt. Na Stop schrijft OnCue het gesprek
  lokaal uit naar `transcript.md` in de sessiemap (`data/sessions/<tijdstip>/`),
  met de regels `[mm:ss] Jij: ...` en `[mm:ss] Ander: ...`. De pagina toont
  "Open transcript" en op macOS "Toon in Finder". Geen dashboard, geen
  presentatie. Alles blijft op deze computer. Draait op poort 8780.
- **Live coach**: de volledige versie van het dashboard: live transcriptie,
  detectie, coaching en presentatie. Draait op poort 8760.

Beide modi kunnen tegelijk draaien — ze zitten op verschillende poorten. Start
je de ene terwijl de andere al draait, dan laat de launcher die staan en meldt
dat via een notificatie.

#### App in het Dock plaatsen
1. Open `scripts/launcher/` in Finder
2. Sleep `Start OnCue.app` naar het Dock
3. Klaar — één klik om te starten

### Dubbelklik vanuit Finder
Open `scripts/launcher/` en dubbelklik op `start-oncue.command`.
Terminal opent en start direct de dashboard-modus (poort 8760); dashboard en
presentatie openen automatisch in de browser. Voor de opname-modus gebruik je
`Start OnCue.app`.

Beide starters delegeren het echte werk aan `scripts/launcher/oncue-launch.sh`
(`bash scripts/launcher/oncue-launch.sh dashboard` of `... record`) — dat is
de enige plek waar de launch-logica staat.

### De app werkt ook buiten de repo
`Start OnCue.app` mag je verplaatsen naar bijvoorbeeld `/Applications` of het
Dock, los van de repo. De eerste keer dat hij de repo niet naast zich vindt,
vraagt hij eenmalig naar de OnCue-projectmap en onthoudt die in
`~/.oncue/repo-path`. Wil je dat overrulen, zet dan `ONCUE_REPO` naar het
repo-pad voordat je de app start.

## Stoppen

Klik op de rode **⏹ Stop** knop rechtsboven in het dashboard (http://localhost:8760/dashboard).
Server sluit graceful af. Browser-tab toont bevestiging.

## Server poorten

| Poort | Dienst |
|-------|--------|
| 8760  | Dashboard + API + WebSocket hub |
| 8761  | WhisperLiveKit transcriptie |
| 8780  | Opname-modus (twee volume-meters + timer, geen dashboard) |

## App-icon

Het Dock-icon is een eigen OnCue-beeldmerk: een gestileerde audio-golfvorm (witte staven)
op een oranje verloop (`#fb923c` → `#f97316`, de dashboard-merkkleuren), met een cyaan
statusstip (`#22d3ee`) rechtsonder. Volledig programmatisch getekend met PIL — geen emoji,
geen font-afhankelijkheid.

### Bronbestanden

| Bestand | Beschrijving |
|---------|-------------|
| `scripts/launcher/icon/icon_1024.png` | 1024×1024 PNG (master) |
| `scripts/launcher/icon/sales-copilot.iconset/` | Alle iconset-resoluties (16…512@2x) |
| `scripts/launcher/icon/sales-copilot.icns` | macOS icon-bundle (alle resoluties) |
| `scripts/launcher/icon/generate_icon.py` | Generator-script |
| `Start OnCue.app/Contents/Resources/applet.icns` | Geïnstalleerd icon in .app |

### Icoon regenereren

`generate_icon.py` is idempotent en doet de hele pipeline in één run: tekent de master-PNG,
resized naar alle iconset-formaten, en bouwt de `.icns` via `iconutil`.

Draai het met de **systeem-python3**, niet met de repo-venv: `generate_icon.py` heeft Pillow
nodig (geverifieerd met 11.3.0) en `.venv` heeft geen PIL geïnstalleerd.

```bash
python3 scripts/launcher/icon/generate_icon.py
```

Bouw daarna de `.app` opnieuw — dit installeert ook meteen het nieuwe icoon:
```bash
bash scripts/launcher/build_app.sh
```

`build_app.sh` is zelf ook idempotent: het compileert `Start OnCue.app` opnieuw uit de
AppleScript-launcher-bron, zet `CFBundleName` op "OnCue", kopieert het icoon erin, en ruimt
een eventuele oude `Start Sales Copilot.app` op.

Als Finder het oude icon blijft tonen: sleep de .app even uit het Dock en terug.

### Visueel bekijken

Dubbelklik `scripts/launcher/icon/icon_1024.png` in Finder voor Quick Look (spatie).

## Troubleshooting

### Microfoon-permissie
Bij eerste gebruik vraagt macOS om toestemming voor microfooningang.
Ga naar **Systeeminstellingen → Privacy & Beveiliging → Microfoon** en zet `applet` (de launcher, OnCue.app) op aan. Start je OnCue vanuit Terminal, dan hoort Terminal erbij.

### Audio-toestemming na herbouw
Herbouwen (`bash scripts/launcher/build_app.sh`) ondertekent de app opnieuw. Dat geeft een
nieuwe identiteit, en de macOS-toestemming voor systeemaudio-opname hangt aan die identiteit.
Die toestemming is na een herbouw dus weg en moet opnieuw worden toegekend via
**Systeeminstellingen → Privacy & beveiliging**, onder "Alleen systeemaudio-opname" (de
launcher staat daar als `applet`). Zonder die toestemming levert de audio-tap stilte zonder
foutmelding.

### Server start niet
Check de log (per modus een eigen bestand):
```bash
tail -50 data/logs/launcher-dashboard.log
tail -50 data/logs/launcher-record.log
```

### Stuck — server reageert niet
```bash
pkill -f "python -m sales_copilot"   # dashboard-modus
pkill -f "record_server.py"          # opname-modus
```
Start daarna opnieuw via de launcher.

### Venv niet gevonden
Draai de installatie opnieuw. Voor de meeste mensen is dat de regel uit [INSTALL.md](../../INSTALL.md); in een checkout `bash scripts/install.sh`.
