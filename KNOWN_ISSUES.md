# Known Issues

An honest snapshot of what works and what doesn't right now. If you're in the pilot, this is where things stand.

## Platform status

**macOS (Apple Silicon)** is the primary, stable track. Audio capture via AudioTee (whole-system tap), transcription via whisper.cpp, pain-point and objection detection, talk-time coaching, and post-call reports all work on Apple Silicon Macs. This is what the pilot was built for first.

**Windows** works for video calls. WASAPI loopback capture and whisper.cpp GPU transcription (cuBLAS) have been confirmed on real physical hardware (see [#148](https://github.com/Vinix24/OnCue/issues/148)), but the install path is manual and a live-call verification with pain-point detection hasn't happened yet. A second physical-hardware field test on 2026-09-06/07 found and fixed five real-install defects ([#223](https://github.com/Vinix24/OnCue/pull/223)) and a native crash on start ([#224](https://github.com/Vinix24/OnCue/pull/224)); none of those fixes has been re-verified on real Windows hardware since landing. Telephony (phone-relay) audio was measured recordable on that same test via Windows' Stereo Mix, but whether it ever reaches the render pipeline OnCue's WASAPI tap reads from — let alone whether OnCue's own tap catches it — is still unconfirmed; see [INSTALL.md](INSTALL.md#windows) ("Known Windows caveats") for the exact, dated evidence and the one test that would settle it. The detailed steps are in [INSTALL.md](INSTALL.md#windows).

**Linux and iPad** are not supported. There is no capture path, no install doc, and no test coverage for either platform.

## Known limitations

- **Windows install has no script.** The macOS `scripts/install.sh` doesn't run on Windows; every dependency and binary must be installed by hand. The steps that worked on real hardware are documented in [INSTALL.md](INSTALL.md#windows).
- **Pain-point detection is unverified on Windows in a live call.** Capture and transcription work on real hardware ([#148](https://github.com/Vinix24/OnCue/issues/148)), but detection hasn't been tested with a real conversation and an active LLM provider on Windows yet.
- **Corporate TLS inspection breaks model downloads.** Machines behind a TLS-terminating proxy (enterprise AV, corporate middlebox) can hit `CERTIFICATE_VERIFY_FAILED` or `CRYPT_E_NO_REVOCATION_CHECK` errors when curl, pip, or the embedding-model downloader tries to fetch models. The workaround that worked is documented in [#148](https://github.com/Vinix24/OnCue/issues/148).
- **Phone/FaceTime-relay capture as a process-isolated tap is macOS-only.** The Apple Continuity path needs the `avconferenced` process tap, which has no Windows equivalent — Windows has no way to exclude telephony audio from the rest of the system mix. Whether Windows can capture telephony audio *at all*, through OnCue's whole-endpoint WASAPI tap, is a separate and still-open question: measured 2026-09-07, the relayed audio is recordable via Stereo Mix, but whether that audio ever reaches the render pipeline WASAPI loopback taps — and whether OnCue's own tap catches it — is unconfirmed. See [INSTALL.md](INSTALL.md#windows) ("Telephony capture") for the mechanism and the one test that would resolve it.
- **A native crash on Windows start, found 2026-09-06, is fixed in code but not re-verified on real hardware.** `soundcard`'s WASAPI recorder is a COM object bound to whichever thread opens it; the reader thread that read it never joined that COM apartment, causing a native crash roughly a second after start on every audio configuration tested. Fixed in [#224](https://github.com/Vinix24/OnCue/pull/224) (single COM-joined reader thread owns the recorder's whole lifecycle); a follow-up adversarial review ([#227](https://github.com/Vinix24/OnCue/pull/227)) found and fixed five further reader-thread lifecycle defects in that same fix — two of them regressions #224 itself introduced (a recorder handle leak on open-failure, and a `start()`/`stop()` race after a timed-out join). Both fixes are verified from source only — no Windows host was available to confirm either on real hardware.
- **Auto-arm (automatic call start) from a detected video-meeting app works on Windows, unverified on real hardware.** The autostart monitor's video-meeting candidate list used to hold macOS process names only (`Google Chrome`, `Microsoft Teams`, `zoom.us`), matched by exact name with no fuzzy fallback, so on Windows — where the same apps run as `chrome.exe`, `Teams.exe`/`ms-teams.exe`, `Zoom.exe` — it polled cleanly but never auto-armed. The candidate list is now platform-aware ([#228](https://github.com/Vinix24/OnCue/pull/228)) and covered by tests exercising both platforms, so the matching bug itself is closed. What tests can't cover: no physical Windows host has been available since the 2026-09-06/07 field test, which predates this fix, so a real detected-app-triggers-auto-arm run on hardware hasn't happened yet. Telephony-based auto-arm remains moot on Windows regardless, since there is no `avconferenced` equivalent. Set `AUTOSTART_MONITOR_ENABLED=false` to skip polling if you'd rather start calls manually.
- **mlx-whisper is an experimental fallback.** whisper.cpp is the default transcription backend and the one the pipeline is tuned for. mlx-whisper works as an Apple Silicon alternative but gets less test coverage.

## Deliberately not built yet (pilot)

**No install wizard.** This is a pilot. Setup means a terminal, manual steps, and reading [INSTALL.md](INSTALL.md). A packaged installer with a GUI is planned but not yet built. The priority right now is making sure the core pipeline works reliably on real calls.

## How to report something

Open a [GitHub issue](https://github.com/Vinix24/OnCue/issues). Tell us your OS, your audio setup (headset, speakers, meeting app), and what you saw versus what you expected.

Issue [#148](https://github.com/Vinix24/OnCue/issues/148) is the detailed Windows physical-hardware test report with the exact config that produced a working run. If you're on Windows, start there.

All feedback is welcome. That's what the pilot is for.
