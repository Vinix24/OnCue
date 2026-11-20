# ADR-006: macOS .dmg packaging, build source, signing and updates

Status: Accepted.

Date: 2026-11-18

## Context

OnCue is installed today with a one-line install (a tarball and a launcher). People who are not developers need an app they can drag to Applications.

The track native-dmg-packaging put the building blocks on main: `setup_app.py` (py2app) and `scripts/build_dmg.sh`. What is not yet decided is which tree the app is built from, who signs it, how it is notarized, how the model and updates arrive, and how it lives next to the one-line install.

The .dmg is a public artefact. OnCue is AGPL, so a binary must come with the corresponding source. The private main contains code that is not part of the public export (`FORBIDDEN_PATHS` in `scripts/export_public.sh`, and the private deep-lane).

## Decision

1. **Bundler: py2app.**
   - `setup_app.py` and `scripts/build_dmg.sh` stay the way to build `OnCue.app`.
   - py2app is mature, macOS-native and builds for Apple Silicon (from the `setup_app.py` docstring).
   - Docker is rejected: it does not produce a macOS app, and the app needs the microphone and the system-audio tap of the host.
   - PyInstaller is rejected: py2app already builds the bundle with the resources, the AudioTee binary and the plist keys this app needs, and a second bundler would have to redo all of it.

2. **Build source: the public export.**
   - `OnCue.app` is built from the tree that `scripts/export_public.sh` produces, never from the private main.
   - The .dmg must carry exactly the public code. That also gives the corresponding source that AGPL requires for a binary.
   - Before signing, a gate scans the whole bundle, bytecode included, for every `FORBIDDEN_PATHS` entry of the export and for the private deep-lane. One hit fails the build. In the public tree, where the export script is absent, the same list comes from `scripts/bundle_forbidden_paths.txt`.

3. **Entry point: the notetaker.**
   - The app opens the same start page as the rc.6 launcher, with "record and write out" as the default (ADR-004, ADR-005).
   - It does not start `sales_copilot.__main__` directly.

4. **Signing only on the operator's Mac, never in CI.**
   - The Developer ID key never leaves that Mac.
   - Signing goes inside-out, with hardened runtime and a secure timestamp. `--deep` is not used.

5. **Notarization through a keychain profile.**
   - Configuration carries names only: `SALES_COPILOT_CERT_ID` (the identity name) and `SALES_COPILOT_NOTARY_PROFILE` (a profile made with `notarytool store-credentials`).
   - The password route (`SALES_COPILOT_NOTARY_KEY`) is removed.

6. **The model comes at first start.**
   - It is downloaded into `~/Library/Application Support/SalesCopilot`, with progress and resume.
   - It is not inside the .dmg.

7. **Updates: a "Check for updates" menu item only.**
   - There is no automatic outbound call. This keeps the promise that OnCue without a key makes no contact with its maker.

8. **Coexistence with the one-line install.**
   - If an OnCue already listens on port 8760, the app does not start a second one and says which install runs.
   - No data is moved between `~/OnCue` and the Application Support folder.

9. **Artefact identity.**
   - The file is `OnCue-<version>-arm64.dmg`, with the version from `pyproject.toml`.
   - Its SHA-256 sits next to it on the same GitHub release as the tarball.
   - A release whose .dmg is not notarized, or fails `spctl --assess`, does not go out.

10. **Bundle id. Status: Accepted (operator, 2026-11-18).**
    - `CFBundleIdentifier` is `nl.vincentvandeth.oncue` (option B).
    - Why: OnCue is the product name, the domain is one the maker controls, and choosing it before the first public release costs no user anything.
    - macOS ties the microphone and audio-capture permissions to this id, so it must not change after the first public release. Dev builds that used the old id ask for permissions once more.
    - Rejected, option A: keep `nl.vincentvandeth.sales-copilot`, which would leave the old product name in the public app for good.

11. **The recording bar is part of the .app. Status: Accepted.**
    - `OnCue Bar.app` (ADR-001, `native/oncue-bar`) is built from the same tree as the app and embedded in `OnCue.app/Contents/Helpers/`, next to `Contents/Resources`. Helpers is where Apple expects nested helper programs; py2app only fills Resources and Frameworks, so it cannot disturb the copy. `scripts/embed_bar.py` does the copy, before the bundle gate.
    - When `OnCue.app` starts the notetaker it starts the bar with `--port <the notetaker's port>` (and `--hotkey` when `BAR_HOTKEY` is set), logs to `data/logs/launcher-bar.log` in Application Support, and stops it when the notetaker returns. A bar that is missing or fails to start is logged and the notetaker keeps running.
    - `scripts/sign_app.py` signs the bar as a bundle, inside-out before the outer app, with hardened runtime and no entitlements: it reads the port over localhost and needs no audio access. `sign_app.py app ... --dry-run` prints the signing order, bar included.
    - `scripts/smoke_app.sh` checks that the bar process starts within the timeout and is gone after the app stops (`SMOKE_REQUIRE_BAR=1` fails a bundle without a bar).

## Alternatives considered

**Build from the private main and strip afterwards.** The bundle would be right only if the strip is complete. Building from the export makes the public tree the only input.

**Sign in CI.** The Developer ID key would have to be stored in a CI secret store. Signing on the operator's Mac keeps it on one machine.

**Notarize with an Apple ID password in the environment.** A secret in configuration. A keychain profile keeps only a name in configuration.

**Bundle the model in the .dmg.** The download grows by gigabytes for every update of the app, and the model choice depends on the Mac.

**Automatic update check.** It is an outbound call without a person asking for it, which breaks the promise in point 7.

## Consequences

- Harder: no signing in CI, so every release has a manual step on the operator's Mac.
- Harder: every release needs the export first, then the build, then the bundle scan, then signing and notarization, in that order.
- Ruled out: a build from the private main.
- Ruled out: an automatic update call, and a notarization password in configuration.
- Stays: the one-line install and its tarball, on the same release as the .dmg.
- A user of the app and a user of the one-line install keep separate data. Moving between them is a manual step.
- Decided: point 10 is accepted; it no longer blocks the first public .dmg.
