#!/bin/bash
# OnCue one-line installer for macOS. Paste this in Terminal:
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)"
#
# It checks the Mac, downloads OnCue into ~/OnCue and runs scripts/install.sh,
# which ends with an app in ~/Applications/OnCue.app. Everything is logged to
# ~/Library/Logs/OnCue/install-<timestamp>.log.
#
# Two routes:
#   release  (default) Downloads the latest published release: the source, the
#            prebuilt speech engine and audio-capture programs (checksum
#            verified) and a private copy of Python (uv, kept inside the OnCue
#            folder). Needs no Apple command line tools, no Homebrew and no
#            password: nothing is compiled.
#   build    Clones the git repo, installs Apple's command line tools and
#            Homebrew when missing and compiles everything. Used for an existing
#            git checkout (developers) and as the fallback when a release has no
#            prebuilt programs or they cannot be downloaded.
#
# Flags:
#   --dry-run   print every step it would take, change nothing
#
# Environment (all optional):
#   ONCUE_DIR            where OnCue is installed          (default ~/OnCue)
#   ONCUE_ROUTE          release|build: force a route      (default: choose)
#   ONCUE_RELEASE        release tag to install            (default: the latest release)
#   ONCUE_API_BASE       GitHub API base for release info  (default https://api.github.com/repos/Vinix24/OnCue)
#   ONCUE_SOURCE_URL_BASE  base of <tag>.tar.gz source archives
#                        (default https://github.com/Vinix24/OnCue/archive/refs/tags)
#   ONCUE_ASSET_URL_BASE   base of <tag>/<asset> release downloads
#                        (default https://github.com/Vinix24/OnCue/releases/download)
#   ONCUE_UV_VERSION     uv version that is installed privately (default pinned below)
#   ONCUE_UV_INSTALLER_URL  uv installer script            (default https://astral.sh/uv/<version>/install.sh)
#   ONCUE_REPO_URL       git URL to clone from (build route) (default the public OnCue repo)
#   ONCUE_REF            branch to clone / update (build route) (default: the repo's default branch)
#   ONCUE_SKIP_INSTALL   1 = stop before install.sh (used by the tests)
#   ONCUE_LOG_DIR        log directory                     (default ~/Library/Logs/OnCue)
#   ONCUE_ZPROFILE       file that gets `brew shellenv`    (build route, default ~/.zprofile)
#   ONCUE_CLT_TIMEOUT    seconds to wait for the command line tools (build route, default 1800)
#   ONCUE_CLT_POLL       seconds between checks            (build route, default 5)
#   ONCUE_MACOS_VERSION  override the detected macOS version (testing only)
#
# The release route must never call git, swift, cmake, make, cc or xcrun: on a Mac
# without the command line tools those are shims that open Apple's install window.
# tests/test_install_release_route.py greps every function named release_* for them.
#
# macOS ships bash 3.2: keep this file free of bash 4 features.
set -euo pipefail

TOTAL_STEPS=5
DRY_RUN=false
LOG_FILE=""
ROUTE=""
RELEASE_TAG=""
RELEASE_NOTE=""
DL_DIR=""
STAGING_DIR=""
MAC_SUMMARY=""

ONCUE_GH_REPO="Vinix24/OnCue"
ONCUE_ARCHIVE="oncue-macos-arm64.tar.gz"
ONCUE_UV_PINNED_VERSION="0.9.0"
RELEASE_MARKER=".oncue-release"
BINARIES_MARKER=".oncue-binaries"

# ── Output helpers ───────────────────────────────────────────────────────────
# Colour only when stdout is a terminal. The decision is made before output is
# piped through tee, and handed to install.sh through ONCUE_COLOR.
if [[ -z "${ONCUE_COLOR:-}" ]]; then
    if [[ -t 1 ]]; then ONCUE_COLOR=1; else ONCUE_COLOR=0; fi
fi
export ONCUE_COLOR

_paint() {
    local code="$1" glyph="$2"
    shift 2
    if [[ "$ONCUE_COLOR" == 1 ]]; then
        printf '\033[%sm%s\033[0m %s\n' "$code" "$glyph" "$*"
    else
        printf '%s %s\n' "$glyph" "$*"
    fi
}
_blue()  { _paint 34 "▸" "$@"; }
_green() { _paint 32 "✓" "$@"; }
_warn()  { _paint 33 "⚠" "$@" >&2; }
_fail()  { _paint 31 "✗" "$@" >&2; exit 1; }
_plan()  { printf '  [dry-run] %s\n' "$*"; }
_step()  { printf '\n[%s/%s] %s\n' "$1" "$TOTAL_STEPS" "$2"; }

_on_exit() {
    local code="$1"
    # Scratch space of the release route. A swapped-in staging dir no longer
    # exists under this name, so only an unfinished one is removed here.
    if [[ -n "$DL_DIR" && -d "$DL_DIR" ]]; then rm -rf "$DL_DIR"; fi
    if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then rm -rf "$STAGING_DIR"; fi
    if [[ "$code" -ne 0 && -n "$LOG_FILE" ]]; then
        printf '\nThe install did not finish. Log: %s\nSend this file when you ask for help.\n' "$LOG_FILE" >&2
    fi
}

# Runs a command with all its output appended to the log and a "still working"
# line every 30 seconds, so a long step never looks frozen. Same helper as in
# scripts/install.sh; this file runs before any checkout exists, so it is copied.
_run_logged() {
    local beat=30 started="$SECONDS" next rc=0 pid
    printf '\n$ %s\n' "$*" >> "$LOG_FILE"
    "$@" >> "$LOG_FILE" 2>&1 &
    pid=$!
    next="$beat"
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if (( SECONDS - started >= next )); then
            printf '  ... still working (%s min %s s)\n' $(( (SECONDS - started) / 60 )) $(( (SECONDS - started) % 60 ))
            next=$(( next + beat ))
        fi
    done
    wait "$pid" || rc=$?
    return "$rc"
}

# ── Checks shared with scripts/oncue_preflight.sh ────────────────────────────
# Inline copy: this script runs before any checkout exists. Keep in sync.
oncue_clt_present() {
    local dev
    dev="$(xcode-select -p 2>/dev/null)" || return 1
    [[ -n "$dev" && -x "$dev/usr/bin/git" ]] || return 1
    # Command line tools keep swift in usr/bin, a full Xcode.app keeps it in its toolchain.
    [[ -x "$dev/usr/bin/swift" || -x "$dev/Toolchains/XcodeDefault.xctoolchain/usr/bin/swift" ]]
}

oncue_macos_supported() {
    local version="${1:-}" major minor
    [[ "$version" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]] || return 1
    major="${version%%.*}"
    minor=0
    if [[ "$version" == *.* ]]; then
        minor="${version#*.}"
        minor="${minor%%.*}"
    fi
    if (( major > 14 )); then
        return 0
    fi
    (( major == 14 && minor >= 2 ))
}

# ── Homebrew helpers ─────────────────────────────────────────────────────────
_brew_bin() {
    if [[ -x /opt/homebrew/bin/brew ]]; then
        printf '%s\n' /opt/homebrew/bin/brew
    elif [[ -x /usr/local/bin/brew ]]; then
        printf '%s\n' /usr/local/bin/brew
    fi
}

# Appends the line Homebrew's own "Next steps" tell you to add, once. Safe to
# call repeatedly: it does nothing when the file already runs `brew shellenv`.
persist_brew_shellenv() {
    local brew_bin="$1" profile="${ONCUE_ZPROFILE:-$HOME/.zprofile}"
    if [[ -f "$profile" ]] && grep -q 'brew shellenv' "$profile"; then
        return 0
    fi
    mkdir -p "$(dirname "$profile")"
    {
        printf '\n'
        printf 'eval "$(%s shellenv)"\n' "$brew_bin"
    } >> "$profile"
}

# ── Steps ────────────────────────────────────────────────────────────────────
# Fails with a plain message when this Mac cannot run OnCue; prints nothing on
# success (the step header comes after the route is known, see main).
check_mac() {
    if [[ "$(uname)" != "Darwin" ]]; then
        _fail "OnCue installs on macOS only. This computer runs $(uname)."
    fi
    if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
        _fail "OnCue needs a Mac with Apple Silicon (M1 or newer). This Mac has an Intel processor: the speech engine OnCue depends on (MLX) does not run on it."
    fi
    local version="${ONCUE_MACOS_VERSION:-$(sw_vers -productVersion)}"
    if ! oncue_macos_supported "$version"; then
        _fail "You have macOS $version. OnCue needs macOS 14.2 or newer (system-audio capture does not work on older versions). Update macOS in System Settings > General > Software Update, then run this again."
    fi
    MAC_SUMMARY="macOS $version on Apple Silicon"
}

step_checks() {
    _step 1 "Checking your Mac"
    _green "$MAC_SUMMARY"
}

step_command_line_tools() {
    _step 2 "Apple command line tools"
    if oncue_clt_present; then
        _green "Command line tools already installed"
        return 0
    fi
    if [[ "$DRY_RUN" == true ]]; then
        _plan "command line tools missing: would run 'xcode-select --install' and wait until they are installed"
        return 0
    fi
    _blue "Apple's command line tools are missing. A window from Apple opens now: click \"Install\" and wait (about 5-15 minutes)."
    xcode-select --install >/dev/null 2>&1 || true
    local timeout="${ONCUE_CLT_TIMEOUT:-1800}" poll="${ONCUE_CLT_POLL:-5}" waited=0 next_note=60
    while ! oncue_clt_present; do
        if (( waited >= timeout )); then
            local limit="$timeout seconds"
            if (( timeout >= 60 )); then limit="$((timeout / 60)) minutes"; fi
            _fail "The command line tools were not installed within $limit. Finish Apple's installation window (or run 'xcode-select --install' yourself), then run this command again."
        fi
        sleep "$poll"
        waited=$((waited + poll))
        if (( waited >= next_note )); then
            _blue "Still waiting for Apple's installation to finish ($((waited / 60)) min)..."
            next_note=$((next_note + 60))
        fi
    done
    _green "Command line tools installed"
}

step_homebrew() {
    _step 3 "Homebrew"
    local brew_bin
    if command -v brew >/dev/null 2>&1; then
        _green "Homebrew found: $(command -v brew)"
        return 0
    fi
    brew_bin="$(_brew_bin)"
    if [[ -n "$brew_bin" ]]; then
        if [[ "$DRY_RUN" == true ]]; then
            _plan "Homebrew is installed at $brew_bin but not on PATH: would add it for this run and to ${ONCUE_ZPROFILE:-$HOME/.zprofile}"
            return 0
        fi
        eval "$("$brew_bin" shellenv)"
        persist_brew_shellenv "$brew_bin"
        _green "Homebrew found: $brew_bin (added to your PATH)"
        return 0
    fi
    if [[ "$DRY_RUN" == true ]]; then
        _plan "Homebrew missing: would run the official installer (asks for your Mac password), then put brew on PATH and in ${ONCUE_ZPROFILE:-$HOME/.zprofile}"
        return 0
    fi
    _blue "Installing Homebrew (the standard Mac package manager). It asks you to press RETURN and then for your Mac password: the one you log in with. You do not see the password while typing, that is normal. This takes 5-10 minutes."
    local url="https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    if [[ -t 0 ]]; then
        /bin/bash -c "$(curl -fsSL "$url")" || _warn "The Homebrew installer reported a problem. Checking whether it finished anyway."
    else
        NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL "$url")" || _warn "The Homebrew installer reported a problem. Checking whether it finished anyway."
    fi
    brew_bin="$(_brew_bin)"
    if [[ -z "$brew_bin" ]]; then
        _fail "Homebrew did not install. Install it from https://brew.sh, then run this command again."
    fi
    eval "$("$brew_bin" shellenv)"
    persist_brew_shellenv "$brew_bin"
    _green "Homebrew installed: $brew_bin"
}

_is_oncue_checkout() {
    local dir="$1"
    [[ -d "$dir/.git" && -f "$dir/src/sales_copilot/__main__.py" && -f "$dir/scripts/install.sh" ]]
}

_dir_is_empty() {
    [[ -d "$1" && -z "$(ls -A "$1")" ]]
}

step_download() {
    _step 4 "Downloading OnCue to $ONCUE_DIR"
    local dir="$ONCUE_DIR" url="${ONCUE_REPO_URL:-https://github.com/Vinix24/OnCue.git}" ref="${ONCUE_REF:-}"

    if [[ ! -e "$dir" ]] || _dir_is_empty "$dir"; then
        if [[ "$DRY_RUN" == true ]]; then
            _plan "would run: git clone ${ref:+--branch $ref }$url $dir"
            return 0
        fi
        mkdir -p "$(dirname "$dir")"
        if [[ -n "$ref" ]]; then
            git clone --quiet --branch "$ref" "$url" "$dir" || _fail "Could not download OnCue from $url (branch $ref). Check your internet connection and run this command again."
        else
            git clone --quiet "$url" "$dir" || _fail "Could not download OnCue from $url. Check your internet connection and run this command again."
        fi
        _green "OnCue downloaded"
        return 0
    fi

    if _is_oncue_checkout "$dir"; then
        if [[ "$DRY_RUN" == true ]]; then
            _plan "existing OnCue checkout: would update with a fast-forward pull${ref:+ of $ref}; .env and data/ stay untouched"
            return 0
        fi
        _blue "OnCue is already downloaded: updating it (your .env and data/ stay untouched)"
        if [[ -n "$ref" ]]; then
            git -C "$dir" fetch --quiet origin "$ref" || _fail "Could not fetch branch $ref in $dir."
            if [[ "$(git -C "$dir" rev-parse --abbrev-ref HEAD)" != "$ref" ]]; then
                git -C "$dir" checkout --quiet "$ref" || _fail "Could not switch $dir to branch $ref. Your local changes in it are untouched."
            fi
            git -C "$dir" merge --quiet --ff-only FETCH_HEAD || _fail "$dir cannot be fast-forwarded to $ref: it has local commits or changes that conflict. Nothing was changed."
        else
            git -C "$dir" pull --quiet --ff-only || _fail "$dir cannot be fast-forwarded: it has local commits or changes that conflict. Nothing was changed."
        fi
        _green "OnCue is up to date"
        return 0
    fi

    if [[ -f "$dir/$RELEASE_MARKER" ]]; then
        _fail "$dir was installed from a release and the build route cannot update it. Try again later, or choose another place: ONCUE_DIR=/other/path (then run this command again)."
    fi
    _fail "$dir already exists and is not an OnCue folder, so I leave it alone. Move it away or choose another place: ONCUE_DIR=/other/path (then run this command again)."
}

step_install() {
    _step 5 "Installing OnCue (this takes about 5-15 minutes the first time)"
    if [[ "$DRY_RUN" == true ]]; then
        _plan "would run: bash $ONCUE_DIR/scripts/install.sh --auto-yes (output goes to the log; the app ends up in ~/Applications/OnCue.app)"
        return 0
    fi
    if [[ "${ONCUE_SKIP_INSTALL:-}" == 1 ]]; then
        _blue "ONCUE_SKIP_INSTALL=1: stopping before install.sh"
        return 0
    fi
    bash "$ONCUE_DIR/scripts/install.sh" --auto-yes
}

# ── Release route (no compiler) ──────────────────────────────────────────────
# Every function named release_* or step_release_* belongs to the route that
# must work on a Mac without Apple's command line tools: no git, swift, cmake,
# make, cc or xcrun, no Homebrew, no password. Only tools macOS itself ships are
# used: curl, tar, shasum, cp, mv, sed, grep. tests/test_install_release_route.py
# greps these functions for the forbidden commands.

release_api_base()    { printf '%s' "${ONCUE_API_BASE:-https://api.github.com/repos/$ONCUE_GH_REPO}"; }
release_source_base() { printf '%s' "${ONCUE_SOURCE_URL_BASE:-https://github.com/$ONCUE_GH_REPO/archive/refs/tags}"; }
release_asset_base()  { printf '%s' "${ONCUE_ASSET_URL_BASE:-https://github.com/$ONCUE_GH_REPO/releases/download}"; }

# True when the JSON text $1 lists a release asset named exactly $2.
release_json_has_asset() {
    local json="$1" name="${2//./\\.}"
    grep -Eq "\"name\"[[:space:]]*:[[:space:]]*\"$name\"" <<< "$json"
}

# Looks up the release (the latest, or ONCUE_RELEASE) and sets RELEASE_TAG.
# Returns 1, with the reason in RELEASE_NOTE, unless the release carries both the
# prebuilt programs and their checksum. Unknown or malformed answers are a no.
release_resolve() {
    local url json tag
    if [[ -n "${ONCUE_RELEASE:-}" ]]; then
        url="$(release_api_base)/releases/tags/$ONCUE_RELEASE"
    else
        url="$(release_api_base)/releases/latest"
    fi
    if ! json="$(curl -fsSL --connect-timeout 15 --max-time 60 "$url" 2>/dev/null)"; then
        RELEASE_NOTE="the release information could not be read"
        return 1
    fi
    tag="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<< "$json")"
    tag="${tag%%$'\n'*}"
    if [[ ! "$tag" =~ ^[A-Za-z0-9._-]+$ ]]; then
        RELEASE_NOTE="the release information has no usable version tag"
        return 1
    fi
    if ! release_json_has_asset "$json" "$ONCUE_ARCHIVE" || ! release_json_has_asset "$json" "$ONCUE_ARCHIVE.sha256"; then
        RELEASE_NOTE="release $tag has no prebuilt programs"
        return 1
    fi
    RELEASE_TAG="$tag"
}

# Prints new | update | git | foreign for the install directory.
release_dir_state() {
    local dir="$ONCUE_DIR"
    if [[ ! -e "$dir" ]] || _dir_is_empty "$dir"; then
        printf 'new'
    elif [[ -d "$dir/.git" ]]; then
        printf 'git'
    elif [[ -f "$dir/$RELEASE_MARKER" && -f "$dir/src/sales_copilot/__main__.py" && -f "$dir/scripts/install.sh" ]]; then
        printf 'update'
    else
        printf 'foreign'
    fi
}

# True when $1 holds the prebuilt programs of RELEASE_TAG, all executable.
release_binaries_current() {
    local root="$1"
    [[ -f "$root/$BINARIES_MARKER" && "$(<"$root/$BINARIES_MARKER")" == "$RELEASE_TAG" ]] || return 1
    [[ -x "$root/vendor/whisper.cpp/build/bin/whisper-cli" \
        && -x "$root/vendor/whisper.cpp/build/bin/whisper-server" \
        && -x "$root/bin/audiotee" ]]
}

release_fetch() {
    _run_logged curl -fsSL --retry 2 --connect-timeout 15 -o "$2" "$1"
}

# Downloads what is needed into a scratch dir and verifies it; changes nothing
# in ONCUE_DIR. $1 = 1 when the source is needed, $2 = 1 when the programs are.
# Returns 1 (reason in RELEASE_NOTE) when a download fails, so the caller can
# fall back, and 2 when the programs fail their checksum or are incomplete: that
# is never retried by another route.
release_download() {
    local need_code="$1" need_bins="$2" asset_url name
    DL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oncue-download.XXXXXX")" || { RELEASE_NOTE="no scratch space for the download"; return 1; }
    if [[ "$need_code" == 1 ]]; then
        if ! release_fetch "$(release_source_base)/$RELEASE_TAG.tar.gz" "$DL_DIR/source.tar.gz"; then
            RELEASE_NOTE="the OnCue source of $RELEASE_TAG could not be downloaded"
            return 1
        fi
    fi
    if [[ "$need_bins" == 1 ]]; then
        asset_url="$(release_asset_base)/$RELEASE_TAG"
        if ! release_fetch "$asset_url/$ONCUE_ARCHIVE" "$DL_DIR/$ONCUE_ARCHIVE" \
            || ! release_fetch "$asset_url/$ONCUE_ARCHIVE.sha256" "$DL_DIR/$ONCUE_ARCHIVE.sha256"; then
            RELEASE_NOTE="the prebuilt programs of $RELEASE_TAG could not be downloaded"
            return 1
        fi
        if ! (cd "$DL_DIR" && shasum -a 256 -c "$ONCUE_ARCHIVE.sha256") >> "$LOG_FILE" 2>&1; then
            RELEASE_NOTE="the downloaded programs do not match their checksum"
            return 2
        fi
        mkdir -p "$DL_DIR/bins"
        if ! tar -xzf "$DL_DIR/$ONCUE_ARCHIVE" -C "$DL_DIR/bins" >> "$LOG_FILE" 2>&1; then
            RELEASE_NOTE="the downloaded programs could not be unpacked"
            return 2
        fi
        for name in whisper-cli whisper-server audiotee; do
            if [[ ! -s "$DL_DIR/bins/$name" ]]; then
                RELEASE_NOTE="the downloaded programs are incomplete ($name is missing)"
                return 2
            fi
        done
    fi
}

# Puts the verified programs where the code looks for them (TranscriberConfig:
# vendor/whisper.cpp/build/bin/, AudioTee: bin/audiotee) inside $1.
release_place_binaries() {
    local root="$1" name bin_dir="$1/vendor/whisper.cpp/build/bin"
    local why="Could not place the OnCue programs in $root (is the disk full or the folder locked?). Run this command again."
    mkdir -p "$bin_dir" "$root/bin" || _fail "$why"
    for name in whisper-cli whisper-server; do
        cp "$DL_DIR/bins/$name" "$bin_dir/$name.part" || _fail "$why"
        chmod 755 "$bin_dir/$name.part" || _fail "$why"
        mv "$bin_dir/$name.part" "$bin_dir/$name" || _fail "$why"
    done
    cp "$DL_DIR/bins/audiotee" "$root/bin/audiotee.part" || _fail "$why"
    chmod 755 "$root/bin/audiotee.part" || _fail "$why"
    mv "$root/bin/audiotee.part" "$root/bin/audiotee" || _fail "$why"
    if [[ -f "$DL_DIR/bins/BUILD_INFO.txt" ]]; then
        cp "$DL_DIR/bins/BUILD_INFO.txt" "$root/bin/BUILD_INFO.txt" || _fail "$why"
    fi
    printf '%s\n' "$RELEASE_TAG" > "$root/$BINARIES_MARKER" || _fail "$why"
}

# Unpacks the source next to the install dir, carries over what is yours
# (.env, data/, the private tools), adds the programs and swaps it in. The old
# install becomes <dir>.previous. $1 = state from release_dir_state.
release_install_code() {
    local state="$1" dir="$ONCUE_DIR" item
    STAGING_DIR="$dir.staging"
    if [[ -e "$STAGING_DIR" ]]; then rm -rf "$STAGING_DIR"; fi
    mkdir -p "$STAGING_DIR" || _fail "Could not create a working folder next to $dir. Nothing was changed."
    if ! tar -xzf "$DL_DIR/source.tar.gz" -C "$STAGING_DIR" --strip-components=1 >> "$LOG_FILE" 2>&1; then
        _fail "The OnCue download is damaged and could not be unpacked. Nothing was changed. Run this command again."
    fi
    if [[ ! -f "$STAGING_DIR/src/sales_copilot/__main__.py" || ! -f "$STAGING_DIR/scripts/install.sh" ]]; then
        _fail "The download for $RELEASE_TAG is not an OnCue release (files are missing). Nothing was changed."
    fi
    if [[ "$state" == update ]]; then
        for item in .env data .tools; do
            if [[ -e "$dir/$item" ]]; then
                rm -rf "${STAGING_DIR:?}/$item"
                # -c clones on APFS: instant and no second copy on disk. Plain copy otherwise.
                if ! cp -Rc "$dir/$item" "$STAGING_DIR/$item" 2>> "$LOG_FILE"; then
                    rm -rf "${STAGING_DIR:?}/$item"
                    cp -R "$dir/$item" "$STAGING_DIR/$item" || _fail "Could not carry $item over to the new version. Nothing was changed."
                fi
            fi
        done
    fi
    release_place_binaries "$STAGING_DIR"
    printf '%s\n' "$RELEASE_TAG" > "$STAGING_DIR/$RELEASE_MARKER" || _fail "Could not write the version marker for $RELEASE_TAG. Nothing was changed."
    mkdir -p "$(dirname "$dir")" || _fail "Could not create the folder for $dir. Nothing was changed."
    if [[ "$state" == update ]]; then
        if [[ -e "$dir.previous" ]]; then rm -rf "$dir.previous"; fi
        mv "$dir" "$dir.previous" || _fail "Could not move the current install aside. Nothing was changed."
        if ! mv "$STAGING_DIR" "$dir"; then
            mv "$dir.previous" "$dir"
            _fail "Could not put the new version in place. The old one is back."
        fi
    else
        if [[ -d "$dir" ]]; then rmdir "$dir" || _fail "Could not put OnCue in $dir."; fi
        mv "$STAGING_DIR" "$dir" || _fail "Could not put OnCue in $dir."
    fi
    STAGING_DIR=""
}

# Never call this as an `if` condition: bash switches errexit off for the whole
# call tree of a condition, so a failed cp, chmod or mv would go unnoticed and
# still end in a green line. It returns 0 unless it stopped through _fail; a
# fallback to the build route is reported through ROUTE instead.
step_release_download() {
    _step 2 "Downloading OnCue $RELEASE_TAG (about 25 MB)"
    local state need_code=1 need_bins=1 rc=0
    state="$(release_dir_state)"
    case "$state" in
        git) _fail "$ONCUE_DIR is a git checkout of OnCue and the release route would overwrite it, so I leave it alone. Update it with ONCUE_ROUTE=build, or choose another place: ONCUE_DIR=/other/path (then run this command again)." ;;
        foreign) _fail "$ONCUE_DIR already exists and is not an OnCue folder, so I leave it alone. Move it away or choose another place: ONCUE_DIR=/other/path (then run this command again)." ;;
    esac
    if [[ "$state" == update && "$(<"$ONCUE_DIR/$RELEASE_MARKER")" == "$RELEASE_TAG" ]]; then
        need_code=0
        if release_binaries_current "$ONCUE_DIR"; then need_bins=0; fi
    fi
    if [[ "$DRY_RUN" == true ]]; then
        if [[ "$need_code" == 0 && "$need_bins" == 0 ]]; then
            _plan "OnCue $RELEASE_TAG is already installed: nothing to download"
        elif [[ "$state" == update ]]; then
            _plan "release install found: would download $RELEASE_TAG and its prebuilt programs (checksum verified), keep .env, data/ and .tools, and move the old version to $ONCUE_DIR.previous"
        else
            _plan "would download the source of $RELEASE_TAG and its prebuilt programs (checksum verified) into $ONCUE_DIR"
        fi
        return 0
    fi
    if [[ "$need_code" == 0 && "$need_bins" == 0 ]]; then
        _green "OnCue $RELEASE_TAG is already downloaded"
        return 0
    fi
    release_download "$need_code" "$need_bins" || rc=$?
    case "$rc" in
        0) ;;
        2) _fail "Stopped: $RELEASE_NOTE. Nothing was changed. Run this command again; if it keeps happening, send the log file." ;;
        *)
            if [[ "${ONCUE_ROUTE:-}" == release ]]; then
                _fail "ONCUE_ROUTE=release, but $RELEASE_NOTE."
            fi
            _warn "Prebuilt download failed ($RELEASE_NOTE): using the build route, which installs Apple's command line tools and Homebrew."
            ROUTE=build
            TOTAL_STEPS=5
            return 0
            ;;
    esac
    if [[ "$need_code" == 1 ]]; then
        release_install_code "$state"
        _green "OnCue $RELEASE_TAG downloaded to $ONCUE_DIR"
    else
        release_place_binaries "$ONCUE_DIR"
        _green "Prebuilt programs of $RELEASE_TAG installed"
    fi
}

# Runs uv with its cache, its Python installs and its config inside the OnCue
# folder, so deleting that folder deletes everything. only-managed keeps uv away
# from the system Python: /usr/bin/python3 is a command-line-tools shim.
release_uv() {
    local tools="$ONCUE_DIR/.tools"
    env \
        UV_CACHE_DIR="$tools/cache" \
        UV_PYTHON_INSTALL_DIR="$tools/python" \
        UV_PYTHON_BIN_DIR="$tools/python-bin" \
        UV_PYTHON_INSTALL_BIN=0 \
        UV_TOOL_DIR="$tools/tools" \
        UV_TOOL_BIN_DIR="$tools/tool-bin" \
        UV_PYTHON_PREFERENCE=only-managed \
        UV_NO_MODIFY_PATH=1 \
        XDG_CONFIG_HOME="$tools/config" \
        XDG_DATA_HOME="$tools/data" \
        XDG_CACHE_HOME="$tools/xdg-cache" \
        "$tools/bin/uv" "$@"
}

release_uv_version() { printf '%s' "${ONCUE_UV_VERSION:-$ONCUE_UV_PINNED_VERSION}"; }

release_uv_present() {
    local tools="$ONCUE_DIR/.tools"
    [[ -x "$tools/bin/uv" ]] && [[ "$("$tools/bin/uv" --version 2>/dev/null)" == "uv $(release_uv_version)"* ]]
}

# The official uv installer, pinned to one version, writing only into .tools.
release_install_uv() {
    local tools="$ONCUE_DIR/.tools" version url installer
    version="$(release_uv_version)"
    url="${ONCUE_UV_INSTALLER_URL:-https://astral.sh/uv/$version/install.sh}"
    mkdir -p "$tools/bin"
    installer="$tools/uv-installer.sh"
    _run_logged curl -fsSL --retry 2 --connect-timeout 15 -o "$installer" "$url" \
        || _fail "Could not download the Python tool (uv) from $url. Check your internet connection and run this command again."
    _run_logged env UV_INSTALL_DIR="$tools/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 \
        XDG_CONFIG_HOME="$tools/config" sh "$installer" \
        || _fail "The Python tool (uv) did not install. See the log: $LOG_FILE"
    rm -f "$installer"
    release_uv_present || _fail "The Python tool (uv) was installed but does not report version $version. See the log: $LOG_FILE"
}

release_venv_usable() {
    local py="$ONCUE_DIR/.venv/bin/python"
    [[ -x "$py" ]] && "$py" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null
}

step_release_python() {
    _step 3 "Python and the OnCue packages (about 1.3 GB, 1-8 minutes)"
    if [[ "$DRY_RUN" == true ]]; then
        _plan "would install uv $(release_uv_version) privately in $ONCUE_DIR/.tools (not on your PATH, your shell profile stays untouched)"
        _plan "would run: uv python install 3.11, uv venv --seed $ONCUE_DIR/.venv, uv pip install -e \".[smart]\" -c constraints.txt (all inside $ONCUE_DIR)"
        return 0
    fi
    if release_uv_present; then
        _green "uv $(release_uv_version) already installed"
    else
        _blue "Installing the Python tool uv $(release_uv_version) inside $ONCUE_DIR/.tools (not on your PATH)..."
        release_install_uv
    fi
    _run_logged release_uv python install 3.11 \
        || _fail "Could not install Python 3.11. Check your internet connection and run this command again. Log: $LOG_FILE"
    if release_venv_usable; then
        _green "Virtual environment already exists at .venv"
    else
        if [[ -e "$ONCUE_DIR/.venv" ]]; then rm -rf "$ONCUE_DIR/.venv"; fi
        # --seed puts pip in the venv for code and instructions that still call it.
        _run_logged release_uv venv --seed --python 3.11 "$ONCUE_DIR/.venv" \
            || _fail "Could not create the Python environment. Log: $LOG_FILE"
    fi
    _blue "Installing OnCue's Python packages (output goes to the log)..."
    _run_logged release_uv --directory "$ONCUE_DIR" pip install --python "$ONCUE_DIR/.venv/bin/python" \
        -e ".[smart]" -c constraints.txt \
        || _fail "Installing the Python packages failed. Check your internet connection and run this command again. Log: $LOG_FILE"
    _green "Python packages installed"
}

step_release_install() {
    _step 4 "Speech model (1.5 GB download) and the OnCue app (about 2-10 minutes)"
    if [[ "$DRY_RUN" == true ]]; then
        _plan "would run: bash $ONCUE_DIR/scripts/install.sh --auto-yes --prebuilt (output goes to the log; the app ends up in ~/Applications/OnCue.app)"
        return 0
    fi
    if [[ "${ONCUE_SKIP_INSTALL:-}" == 1 ]]; then
        _blue "ONCUE_SKIP_INSTALL=1: stopping before install.sh"
        return 0
    fi
    bash "$ONCUE_DIR/scripts/install.sh" --auto-yes --prebuilt
}

# Chooses ROUTE (release or build). A git checkout always stays on the build
# route. Without a usable release the build route is the fallback, with one line.
choose_route() {
    local forced="${ONCUE_ROUTE:-}"
    case "$forced" in
        ""|release|build) ;;
        *) _fail "ONCUE_ROUTE must be 'release' or 'build', not '$forced'." ;;
    esac
    if [[ "$forced" == build ]] || { [[ -z "$forced" ]] && _is_oncue_checkout "$ONCUE_DIR"; }; then
        ROUTE=build
        return 0
    fi
    if release_resolve; then
        ROUTE=release
        return 0
    fi
    if [[ "$forced" == release ]]; then
        _fail "ONCUE_ROUTE=release, but $RELEASE_NOTE."
    fi
    ROUTE=build
    _warn "No prebuilt OnCue available ($RELEASE_NOTE): using the build route, which installs Apple's command line tools and Homebrew."
}

main() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            --dry-run) DRY_RUN=true ;;
            -h|--help)
                printf 'Usage: bootstrap.sh [--dry-run]\n  --dry-run  print every step, change nothing\n'
                return 0
                ;;
            *) _fail "Unknown option: $arg (only --dry-run is supported)." ;;
        esac
    done

    ONCUE_DIR="${ONCUE_DIR:-$HOME/OnCue}"
    ONCUE_DIR="${ONCUE_DIR%/}"

    if [[ "$DRY_RUN" == true ]]; then
        printf 'OnCue installer (dry run: nothing is changed)\n'
    else
        local log_dir="${ONCUE_LOG_DIR:-$HOME/Library/Logs/OnCue}"
        mkdir -p "$log_dir"
        LOG_FILE="$log_dir/install-$(date +%Y%m%d-%H%M%S).log"
        : > "$LOG_FILE"
        export ONCUE_LOG_FILE="$LOG_FILE"
        # Everything printed from here on goes to the terminal AND the log.
        exec > >(tee -a "$LOG_FILE") 2>&1
        printf 'OnCue installer\nLog: %s\n' "$LOG_FILE"
    fi
    trap '_on_exit $?' EXIT

    check_mac
    choose_route
    if [[ "$ROUTE" == release ]]; then TOTAL_STEPS=4; else TOTAL_STEPS=5; fi
    step_checks
    if [[ "$ROUTE" == release ]]; then
        _blue "Installing release $RELEASE_TAG without compiling: no extra tools to install and no password."
        step_release_download
        if [[ "$ROUTE" == release ]]; then
            step_release_python
            step_release_install
            return 0
        fi
    fi
    step_command_line_tools
    step_homebrew
    step_download
    step_install
}

# The tests source this file to call single functions; a normal run executes main.
if [[ "${ONCUE_BOOTSTRAP_SOURCE_ONLY:-}" != 1 ]]; then
    main "$@"
fi
