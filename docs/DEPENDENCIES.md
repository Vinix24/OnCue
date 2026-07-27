# Vendored Dependencies

This project vendors selected third-party source dependencies under `vendor/`,
either as pinned Git submodules or as tracked source files.

## Pinned submodules

- `vendor/whisper.cpp`
  - Upstream: `https://github.com/ggml-org/whisper.cpp`
  - Pinned commit: `95ea8f9bfb03a15db08a8989966fd1ae3361e20d`
  - Nearest tag: `v1.8.4`

## Vendored source (tracked files, not a submodule)

- `vendor/audiotee`
  - Upstream: `https://github.com/makeusabrew/audiotee`
  - Vendored commit: `56ac954369a09318e46b88a6eec33c2d2b0d32a3`
  - License: MIT (`vendor/audiotee/LICENSE`)
  - Tracked as plain source files (`Package.swift`, `Sources/`, `Tests/`, `LICENSE`)
    rather than a submodule, so the OSS export and a plain `git clone` both get
    the source with no extra `git submodule update` step. `vendor/audiotee/.build/`
    (SwiftPM's build output) and the compiled `bin/audiotee` binary are gitignored
    and built locally by `scripts/setup.sh` / `scripts/install.sh`
    (`swift build -c release --package-path vendor/audiotee`).

## Updating a vendored dependency

1. Update the desired dependency:
- Submodule: `git submodule update --remote vendor/whisper.cpp`
- Tracked source (`vendor/audiotee`): fetch the target commit from upstream and
  replace `Package.swift` / `Sources/` / `Tests/` / `LICENSE`, then update the
  "Vendored commit" hash above.
2. For submodules, check out the exact target commit/tag.
3. Run quality gates:
- `ruff check src/`
- `python -m pytest tests/ -v`
- `scripts/install_whisper_cpp.sh` (for whisper.cpp updates)
- `swift build -c release --package-path vendor/audiotee` (for AudioTee updates, macOS only)
4. Include test evidence and pinned hash changes in the PR.

Any version bump requires a PR with validation evidence before merge.
