# Known Issues

An honest snapshot of what works and what doesn't right now. If you're in the pilot, this is where things stand.

## Platform status

**macOS (Apple Silicon)** is the primary, stable track. Audio capture via AudioTee (whole-system tap), transcription via whisper.cpp, pain-point and objection detection, talk-time coaching, and post-call reports all work on Apple Silicon Macs. This is what the pilot was built for first.

**Windows** is experimental. WASAPI loopback capture and whisper.cpp GPU transcription (cuBLAS) have been confirmed on real physical hardware (see [#148](https://github.com/Vinix24/OnCue/issues/148)), but the install path is manual and a live-call verification with pain-point detection hasn't happened yet. The detailed steps are in [INSTALL.md](INSTALL.md#windows).

**Linux and iPad** are not supported. There is no capture path, no install doc, and no test coverage for either platform.

## Known limitations

- **Windows install has no script.** The macOS `scripts/install.sh` doesn't run on Windows; every dependency and binary must be installed by hand. The steps that worked on real hardware are documented in [INSTALL.md](INSTALL.md#windows).
- **Pain-point detection is unverified on Windows in a live call.** Capture and transcription work on real hardware ([#148](https://github.com/Vinix24/OnCue/issues/148)), but detection hasn't been tested with a real conversation and an active LLM provider on Windows yet.
- **Corporate TLS inspection breaks model downloads.** Machines behind a TLS-terminating proxy (enterprise AV, corporate middlebox) can hit `CERTIFICATE_VERIFY_FAILED` or `CRYPT_E_NO_REVOCATION_CHECK` errors when curl, pip, or the embedding-model downloader tries to fetch models. The workaround that worked is documented in [#148](https://github.com/Vinix24/OnCue/issues/148).
- **Phone/FaceTime-relay capture is macOS-only.** The Apple Continuity path needs the `avconferenced` process tap and only works on macOS. Windows covers video-call platforms (Zoom, Teams, Meet, Webex) via WASAPI loopback but not telephony.
- **mlx-whisper is an experimental fallback.** whisper.cpp is the default transcription backend and the one the pipeline is tuned for. mlx-whisper works as an Apple Silicon alternative but gets less test coverage.

## Deliberately not built yet (pilot)

**No install wizard.** This is a pilot. Setup means a terminal, manual steps, and reading [INSTALL.md](INSTALL.md). A packaged installer with a GUI is planned but not yet built. The priority right now is making sure the core pipeline works reliably on real calls.

## How to report something

Open a [GitHub issue](https://github.com/Vinix24/OnCue/issues). Tell us your OS, your audio setup (headset, speakers, meeting app), and what you saw versus what you expected.

Issue [#148](https://github.com/Vinix24/OnCue/issues/148) is the detailed Windows physical-hardware test report with the exact config that produced a working run. If you're on Windows, start there.

All feedback is welcome. That's what the pilot is for.
