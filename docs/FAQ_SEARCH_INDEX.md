# FAQ Search Index

Quick scan list (Cmd+F): click through to the full Q/A in [FAQ.md](FAQ.md).

## Category 1 — Installation & first run

- [The install script fails on BlackHole ("unidentified developer")](FAQ.md#faq-het-installatiescript-faalt-op-blackhole-unidentified-developer)
- [Homebrew command asks for a password (is that safe?)](FAQ.md#faq-homebrew-commando-vraagt-om-password-is-dat-veilig)
- ["Python 3.11 not found" during setup](FAQ.md#faq-python-3-11-not-found-tijdens-setup)
- [Script hangs at "Downloading Whisper model"](FAQ.md#faq-script-hangt-bij-downloading-whisper-model)
- [After installation I can no longer hear sound from my Mac](FAQ.md#faq-na-installatie-kan-ik-geen-geluid-meer-horen-van-mijn-mac)
- [My work Mac refuses the installation (corporate MDM)](FAQ.md#faq-mijn-werk-mac-weigert-de-installatie-corporate-mdm)
## Category 2 — Audio problems

> By default, OnCue taps the entire system output mix via AudioTee (`tap_all`), without audio routing. BlackHole/Multi-Output is the manual fallback — the BlackHole items below cover that fallback.

- [Bluetooth headset + iPhone-relay call produces no prospect transcript](FAQ.md#faq-bluetooth-headset-en-telefoongesprek-geen-transcript)
- [My microphone is not detected by OnCue](FAQ.md#faq-mijn-microfoon-wordt-niet-gedetecteerd-door-sales-copilot)
- [I hear my own voice doubled / echo during the call](FAQ.md#faq-ik-hoor-mijn-eigen-stem-dubbel-echo-tijdens-de-call)
- [I can't hear the prospect through my headphones](FAQ.md#faq-ik-hoor-de-prospect-niet-door-mijn-koptelefoon)
- [My external USB microphone doesn't work (AirPods, Jabra, Shure MV7, etc.)](FAQ.md#faq-mijn-externe-usb-microfoon-werkt-niet-airpods-jabra-shure-mv7-etc)
- [Bluetooth headphones work at first, then stop working](FAQ.md#faq-bluetooth-koptelefoon-werkt-eerst-wel-dan-niet-meer)
- [Audio crackles / poor quality on BlackHole](FAQ.md#faq-audio-crackles-slechte-kwaliteit-op-blackhole)
- [YouTube/Spotify sound doesn't come through BlackHole](FAQ.md#faq-youtube-spotify-geluid-komt-niet-door-blackhole)
- [Which multi-output device should I choose in Audio MIDI Setup?](FAQ.md#faq-welk-multi-output-device-moet-ik-kiezen-in-audio-midi-setup)
- [What if I have a headset with microphone and speakers in one device?](FAQ.md#faq-wat-als-ik-headset-met-microfoon-en-speakers-in-een-device-heb)
- [How do I test whether audio is routed correctly BEFORE a real call?](FAQ.md#faq-hoe-test-ik-of-audio-correct-gerouteerd-is-voor-een-echte-call)
## Category 3 — Dashboard & UI

- [Dashboard doesn't open when I double-click the HTML file (file://)](FAQ.md#faq-dashboard-opent-niet-via-file-dubbel-klik)
- [Dashboard shows "Connected to backend" but nothing happens](FAQ.md#faq-dashboard-toont-verbonden-met-backend-maar-er-gebeurt-niks)
- ["Start Call" button gives an error](FAQ.md#faq-start-call-knop-geeft-een-foutmelding)
- [Talk-time bar doesn't move during the call](FAQ.md#faq-talk-time-balk-beweegt-niet-tijdens-de-call)
- [Timer stays at 00:00](FAQ.md#faq-timer-blijft-op-00-00-staan)
- [Transcription doesn't appear (or only after 30 seconds)](FAQ.md#faq-transcriptie-verschijnt-niet-of-pas-na-30-seconden)
- [Browser tab freezes during the call](FAQ.md#faq-browser-tab-freezet-tijdens-de-call)
- [Dashboard loses connection after the Mac wakes from sleep](FAQ.md#faq-dashboard-verliest-verbinding-na-mac-uit-slaap-komt)
- [Pain points panel is empty even though the prospect mentions them](FAQ.md#faq-pijnpunten-paneel-is-leeg-ondanks-dat-prospect-ze-noemt)
- [The presentation panel doesn't update during the call](FAQ.md#faq-slide-verschijnt-niet-ondanks-gedetecteerd-pijnpunt)
- [End Call returns to setup instead of showing the report](FAQ.md#faq-end-call-gaat-terug-naar-setup-in-plaats-van-rapport-tonen)
## Category 4 — Speaker recognition (YouTube test scenario)

- [YouTube audio is tagged as "YOU" while I'm not talking](FAQ.md#faq-youtube-audio-wordt-getagged-als-jij-terwijl-ik-niet-praat)
- [Prospect and I are swapped in talk-time](FAQ.md#faq-prospect-en-ik-worden-omgedraaid-in-talk-time)
- [Two speakers in a YouTube video are seen as one](FAQ.md#faq-twee-sprekers-in-youtube-video-worden-als-een-gezien)
- [Diarization works poorly with Flemish/English/accents](FAQ.md#faq-diarization-werkt-slecht-bij-vlaams-engels-accenten)
- [How do I test whether the single-stream fallback is active?](FAQ.md#faq-hoe-test-ik-of-single-stream-fallback-actief-is)
## Category 5 — Whisper & transcription

- [mlx-whisper backend is running but no transcripts appear](FAQ.md#faq-mlx-whisper-levert-geen-transcripten-tijdens-call)
- [Whisper warmup takes too long (30 seconds+)](FAQ.md#faq-whisper-warmup-duurt-te-lang-30-seconden)
- [Transcription misses the first sentence of the call](FAQ.md#faq-transcriptie-mist-de-eerste-zin-van-de-call)
- [Transcription contains "hallucinations" (invented sentences during silence)](FAQ.md#faq-transcriptie-bevat-hallucinaties-uitgevonden-zinnen-tijdens-stilte)
- [Which Whisper model should I choose: base / medium / large-v3 / turbo?](FAQ.md#faq-welk-whisper-model-moet-ik-kiezen-base-medium-large-v3-turbo)
- [How do I switch from mlx-whisper to whisper.cpp (or vice versa)?](FAQ.md#faq-hoe-switch-ik-van-mlx-whisper-naar-whisper-cpp-of-andersom)
- [Model download fails (no internet / HuggingFace rate limit)](FAQ.md#faq-model-download-faalt-geen-internet-huggingface-rate-limit)
- [Transcription is slow (lagging behind audio)](FAQ.md#faq-transcriptie-is-traag-lagging-achter-audio)
- [English is transcribed while the conversation is in Dutch](FAQ.md#faq-engels-wordt-getranscribeerd-terwijl-gesprek-in-nederlands-is)
## Category 6 — LLM problems

- [Gemini API call fails (401 unauthorized)](FAQ.md#faq-gemini-api-call-faalt-401-unauthorized)
- [LLM detection is slow / timeout after 3s](FAQ.md#faq-llm-detectie-is-traag-timeout-na-3s)
- [I want to use a local LLM via Ollama — how?](FAQ.md#faq-ik-wil-lokale-llm-via-ollama-gebruiken-hoe)
- [Gemma / Qwen / Llama, which works best for Dutch?](FAQ.md#faq-gemma-qwen-llama-welke-werkt-het-best-voor-nederlands)
- [LLM costs are adding up — how do I monitor this?](FAQ.md#faq-llm-kosten-lopen-op-hoe-monitor-ik-dit)
- [Which models in the dropdown are better for Dutch?](FAQ.md#faq-welke-modellen-uit-de-dropdown-zijn-beter-voor-nederlands)
## Category 7 — Pre-call setup

- [I have to enter the same settings every time — can't that be smarter?](FAQ.md#faq-ik-moet-elke-keer-dezelfde-instellingen-invullen-kan-dat-niet-slimmer)
- [Uploading context docs fails](FAQ.md#faq-context-docs-uploaden-faalt)
- [I want to save a preset for "discovery calls" vs "demo calls"](FAQ.md#faq-ik-wil-een-preset-opslaan-voor-discovery-calls-vs-demo-calls)
- [How do I add my own case slides?](FAQ.md#faq-hoe-voeg-ik-eigen-case-slides-toe)
- [How do I add my own pain points (on top of the default 12)?](FAQ.md#faq-hoe-voeg-ik-eigen-pijnpunten-toe-bovenop-de-standaard-12)
- [LLM Provider + Model are two fields (should be one)](FAQ.md#faq-llm-provider-model-zijn-twee-velden-zou-een-moeten-zijn)
## Category 8 — Privacy & security

- [Does my audio go to a cloud?](FAQ.md#faq-gaat-mijn-audio-naar-een-cloud)
- [Can I run 100% locally (no internet)?](FAQ.md#faq-kan-ik-100-lokaal-draaien-geen-internet)
- [Is this GDPR-compliant for Dutch customers?](FAQ.md#faq-is-dit-avg-compliant-voor-nederlandse-klanten)
- [How do I inform the prospect about recording/transcription practices?](FAQ.md#faq-hoe-informeer-ik-de-prospect-over-opname-transcriptie-praktijken)
- [Can I share call data with my CRM without a cloud?](FAQ.md#faq-kan-ik-call-data-delen-met-mijn-crm-zonder-cloud)
## Category 9 — Productivity & integration

- [How do I integrate this with HubSpot / Pipedrive?](FAQ.md#faq-hoe-integreer-ik-dit-met-hubspot-pipedrive-verwijs-naar-pro)
- [Does this work with Zoom / Teams / Meet / BlueJeans?](FAQ.md#faq-werkt-dit-met-zoom-teams-meet-bluejeans)
- [Does this work on Windows / Linux / iPad?](FAQ.md#faq-werkt-dit-op-windows-linux-ipad)
- [Can I do multiple calls per day without restarting each time?](FAQ.md#faq-kan-ik-meerdere-calls-per-dag-doen-zonder-telkens-opnieuw-opstarten)
- [How do I export call reports to Notion / Obsidian / email?](FAQ.md#faq-hoe-export-ik-call-rapporten-naar-notion-obsidian-email)
## Category 10 — Error messages & diagnostics

- ["Port 8760 already in use" — what now?](FAQ.md#faq-port-8760-already-in-use-wat-nu)
- [`data/logs/runtime.log` is empty / contains errors](FAQ.md#faq-data-logs-runtime-log-is-leeg-bevat-errors)
- [Websocket connection keeps closing](FAQ.md#faq-websocket-connection-keeps-closing)
- ["Model switch failed" at startup](FAQ.md#faq-model-switch-failed-bij-opstarten)
- [How do I reset everything to a clean state?](FAQ.md#faq-hoe-reset-ik-alles-naar-een-schone-staat)
- [gpt-5.2-codex model is not supported (ChatGPT account)](FAQ.md#faq-gpt-5-2-codex-model-is-not-supported-chatgpt-account)
- [Model-switch loop after /new](FAQ.md#faq-model-switch-loop-na-new)
## Category 11 — Upgrade & maintenance

- [How do I update to a new version?](FAQ.md#faq-hoe-update-ik-naar-een-nieuwe-versie)
- [Where is my data stored? (for backups)](FAQ.md#faq-waar-staat-mijn-data-opgeslagen-voor-backups)
- [Can I clean up old calls to save space?](FAQ.md#faq-kan-ik-oude-calls-opschonen-om-ruimte-te-besparen)
- [How does the Whisper submodule update work?](FAQ.md#faq-hoe-werkt-de-whisper-submodule-update)
