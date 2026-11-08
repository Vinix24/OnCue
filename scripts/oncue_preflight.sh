#!/usr/bin/env bash
# Shared install preflight checks, sourced by scripts/install.sh.
#
# scripts/bootstrap.sh runs before any checkout exists, so it carries its own
# inline copy of oncue_clt_present and oncue_macos_supported. Keep them in sync:
# tests/test_install_scripts.py runs both against the same fake toolchain.
#
# Not meant to be executed directly.

ONCUE_MIN_MACOS_MAJOR=14
ONCUE_MIN_MACOS_MINOR=2

# True only when the real Xcode command line tools are present. `command -v git`
# is not enough: on a Mac without the tools /usr/bin/git and /usr/bin/swift are
# shims that pop up Apple's install dialog. `xcode-select -p` honours
# DEVELOPER_DIR, which is also how the tests point it at a fake toolchain.
oncue_clt_present() {
    local dev
    dev="$(xcode-select -p 2>/dev/null)" || return 1
    [[ -n "$dev" && -x "$dev/usr/bin/git" ]] || return 1
    # Command line tools keep swift in usr/bin, a full Xcode.app keeps it in its toolchain.
    [[ -x "$dev/usr/bin/swift" || -x "$dev/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift" ]]
}

# True when the given macOS version (e.g. 14.2.1, 15.0, 26) is 14.2 or newer.
# A missing or malformed version is never supported.
oncue_macos_supported() {
    local version="${1:-}" major minor
    [[ "$version" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] || return 1
    major="${version%%.*}"
    minor=0
    if [[ "$version" == *.* ]]; then
        minor="${version#*.}"
        minor="${minor%%.*}"
    fi
    if (( major > ONCUE_MIN_MACOS_MAJOR )); then
        return 0
    fi
    (( major == ONCUE_MIN_MACOS_MAJOR && minor >= ONCUE_MIN_MACOS_MINOR ))
}

# Plain-language sentence for an unsupported macOS version.
oncue_macos_message() {
    printf 'You have macOS %s. OnCue needs macOS %s.%s or newer (system-audio capture does not work on older versions). Update macOS in System Settings > General > Software Update, then run this again.' \
        "${1:-unknown}" "$ONCUE_MIN_MACOS_MAJOR" "$ONCUE_MIN_MACOS_MINOR"
}
