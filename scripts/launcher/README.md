# OnCue Launcher

## Starten

### Dubbelklik vanuit Finder
Open `scripts/launcher/` en dubbelklik op `start-sales-copilot.command`.
Terminal opent, server start op, dashboard en presentatie openen automatisch in de browser.
Het Terminal-venster sluit automatisch na 30 seconden.

### Vanuit het Dock
Dubbelklik op `Start OnCue.app`. Geen Terminal-venster — browser tabs openen direct.

#### App in het Dock plaatsen
1. Open `scripts/launcher/` in Finder
2. Sleep `Start OnCue.app` naar het Dock
3. Klaar — één klik om te starten

## Stoppen

Klik op de rode **⏹ Stop** knop rechtsboven in het dashboard (http://localhost:8760/dashboard).
Server sluit graceful af. Browser-tab toont bevestiging.

## Server poorten

| Poort | Dienst |
|-------|--------|
| 8760  | Dashboard + API + WebSocket hub |
| 8761  | WhisperLiveKit transcriptie |

## App-icon

Het Dock-icon is een VNX-stijl gradient (oranje → rood) met een microfoon-emoji (🎙).

### Bronbestanden

| Bestand | Beschrijving |
|---------|-------------|
| `scripts/launcher/icon/icon_1024.png` | 1024×1024 PNG (master) |
| `scripts/launcher/icon/sales-copilot.icns` | macOS icon-bundle (alle resoluties) |
| `scripts/launcher/icon/generate_icon.py` | Generator-script |
| `Start OnCue.app/Contents/Resources/applet.icns` | Geïnstalleerd icon in .app |

### Eigen logo gebruiken

1. Maak een 1024×1024 PNG met je eigen design
2. Genereer `.icns` met iconutil:
   ```bash
   # Maak iconset-map met alle resoluties (16…512@2x)
   mkdir -p mijn-icon.iconset
   # ... resize naar alle groottes
   iconutil -c icns mijn-icon.iconset -o mijn-icon.icns
   ```
3. Vervang het icon in de .app:
   ```bash
   cp mijn-icon.icns "scripts/launcher/Start OnCue.app/Contents/Resources/applet.icns"
   touch "scripts/launcher/Start OnCue.app"
   ```
4. Als Finder het oude icon blijft tonen: sleep de .app even uit het Dock en terug.

### Visueel bekijken

Dubbelklik `scripts/launcher/icon/icon_1024.png` in Finder voor Quick Look (spatie).

## Troubleshooting

### Microfoon-permissie
Bij eerste gebruik vraagt macOS om toestemming voor microfooningang.
Ga naar **Systeeminstellingen → Privacy & Beveiliging → Microfoon** en zet Terminal (of het .app) op aan.

### Server start niet
Check de log:
```bash
tail -50 data/logs/launcher-server.log
```

### Stuck — server reageert niet
```bash
pkill -f "python -m sales_copilot"
```
Start daarna opnieuw via de launcher.

### Venv niet gevonden
Run eerst de setup:
```bash
./scripts/setup.sh
```
