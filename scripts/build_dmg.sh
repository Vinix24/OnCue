#!/bin/bash
# Build OnCue.app and OnCue-<version>-arm64.dmg from the PUBLIC export (ADR-006).
#
# Usage:
#   bash scripts/build_dmg.sh [--source DIR] [--binaries DIR] [--out DIR]
#
#   --source DIR    build from this export tree. Without it the export runs into a fresh
#                   temp directory first. The private checkout is never built directly.
#                   In the public export (no scripts/export_public.sh) the current tree IS the
#                   export, so without --source the script builds from it.
#   --binaries DIR  directory holding whisper-cli, whisper-server and audiotee (the contents
#                   of oncue-macos-arm64.tar.gz). Without it scripts/build_macos_binaries.sh
#                   builds them from the source tree.
#   --out DIR       where the .dmg lands (default: ./dist)
#
# The recording bar (OnCue Bar.app) is embedded in Contents/Helpers before the gate (scripts/embed_bar.py).
# The bundle gate (scripts/check_bundle_gate.py) and the zip Mach-O check (scripts/check_zip_macho.py)
# run after py2app and before signing.
# Without SALES_COPILOT_CERT_ID the signature is ad-hoc (codesign --sign -).
# With it (ADR-006 points 4, 5, 9) the app is signed inside-out with a Developer ID, the .dmg is
# signed, notarized and stapled, and the result is verified; scripts/sign_app.py does the work.
#
# The temp folder ($WORK, about 6 GB, removed on exit by a trap on success, failure and interrupt,
# once the .dmg is in --out) holds the venv, the binaries, the bar build, the staging folder and,
# without --source, the export. py2app writes the app to $SOURCE/dist/OnCue.app: that is inside the
# temp folder only when the export ran there; with --source (or in a public tree) it lands in that
# tree's dist/ and is NOT removed by the trap. ONCUE_KEEP_BUILD_TMP=1 keeps the temp folder and
# prints its path.
#   SALES_COPILOT_CERT_ID         identity name, e.g. "Developer ID Application: Name (TEAMID)"
#   SALES_COPILOT_NOTARY_PROFILE  notarytool keychain profile (xcrun notarytool store-credentials)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE="" BINARIES="" OUT_DIR="$PWD/dist"

die() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --source) SOURCE="${2:?--source needs a directory}"; shift 2 ;;
        --binaries) BINARIES="${2:?--binaries needs a directory}"; shift 2 ;;
        --out) OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

CERT_ID="${SALES_COPILOT_CERT_ID:-}"
# Refuse before the long build: a Developer ID build without a notarization profile cannot ship.
[[ -z "$CERT_ID" || -n "${SALES_COPILOT_NOTARY_PROFILE:-}" ]] \
    || die "SALES_COPILOT_CERT_ID is set but SALES_COPILOT_NOTARY_PROFILE is not (a notarytool keychain profile name)"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || die "this build needs an Apple Silicon Mac"

# shellcheck source=scripts/safe_tmp_rm.sh
source "$SCRIPT_DIR/safe_tmp_rm.sh"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/oncue-build.XXXXXX")"
cleanup_work() {
    local status=$?
    trap - EXIT
    if [[ "${ONCUE_KEEP_BUILD_TMP:-0}" == "1" ]]; then
        echo "=== Build temp folder kept: $WORK ===" >&2
    else
        safe_tmp_rm "$WORK" || echo "WARNING: build temp folder $WORK was not removed" >&2
    fi
    exit "$status"
}
trap cleanup_work EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo "=== Work directory: $WORK ==="

# 1. Build source: always an export tree.
# The public export leaves export_public.sh out: its absence marks the public tree.
PUBLIC_TREE=0
[[ -f "$REPO_ROOT/scripts/export_public.sh" ]] || PUBLIC_TREE=1
if [[ -z "$SOURCE" ]]; then
    if ((PUBLIC_TREE)); then
        echo "=== Public tree: building from $REPO_ROOT ==="
        SOURCE="$REPO_ROOT"
    else
        echo "=== Running the public export ==="
        bash "$REPO_ROOT/scripts/export_public.sh" "$WORK/export"
        SOURCE="$WORK/export"
    fi
fi
SOURCE="$(cd "$SOURCE" && pwd -P)"
if ((! PUBLIC_TREE)); then
    [[ "$SOURCE" != "$(cd "$REPO_ROOT" && pwd -P)" ]] || die "refusing to build from the private checkout; pass an export tree"
fi
[[ -f "$SOURCE/setup_app.py" && -d "$SOURCE/src/sales_copilot" ]] || die "$SOURCE is not an export tree (no setup_app.py or src/sales_copilot)"
[[ ! -e "$SOURCE/.vnx" && ! -e "$SOURCE/claudedocs" ]] || die "$SOURCE holds private paths; it is not the public export"
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$SOURCE/pyproject.toml" | head -n 1)"
[[ -n "$VERSION" ]] || die "no version in $SOURCE/pyproject.toml"

# 2. Prebuilt programs.
if [[ -z "$BINARIES" ]]; then
    echo "=== Building whisper.cpp and AudioTee ==="
    bash "$SOURCE/scripts/build_macos_binaries.sh" "$WORK/binaries"
    BINARIES="$WORK/binaries/work/verify"
fi
BINARIES="$(cd "$BINARIES" && pwd -P)"
for tool in whisper-cli whisper-server audiotee; do
    [[ -x "$BINARIES/$tool" ]] || die "$BINARIES/$tool is missing or not executable"
done

# 3. py2app.
echo "=== Building OnCue.app with py2app ==="
# 3.12: the newest Python all the extras (semantic-router, mlx-whisper) support; override with ONCUE_BUILD_PYTHON.
"${ONCUE_BUILD_PYTHON:-python3.12}" -m venv "$WORK/venv"
"$WORK/venv/bin/python" -m pip install --quiet --upgrade pip py2app
"$WORK/venv/bin/python" -m pip install --quiet "$SOURCE[full]"
(cd "$SOURCE" && ONCUE_BINARIES_DIR="$BINARIES" "$WORK/venv/bin/python" setup_app.py py2app)
APP="$SOURCE/dist/OnCue.app"
[[ -d "$APP" ]] || die "py2app did not produce $APP"

# 3b. The recording bar goes into Contents/Helpers (ADR-006, point 11). It comes from the binaries
# directory when that holds one, else it is built from this same source tree.
BAR_APP="$BINARIES/OnCue Bar.app"
if [[ ! -d "$BAR_APP" ]]; then
    echo "=== Building OnCue Bar from $SOURCE ==="
    mkdir -p "$WORK/bar"
    bash "$SOURCE/scripts/build_oncue_bar.sh" "$WORK/bar"
    BAR_APP="$WORK/bar/OnCue Bar.app"
fi
python3 "$SOURCE/scripts/embed_bar.py" "$APP" "$BAR_APP"

# 4. Bundle gate: before any signing. Uses the forbidden list of the checkout this script lives in.
echo "=== Bundle gate ==="
if ((PUBLIC_TREE)); then
    python3 "$REPO_ROOT/scripts/check_bundle_gate.py" "$APP" --forbidden-file "$REPO_ROOT/scripts/bundle_forbidden_paths.txt"
else
    python3 "$REPO_ROOT/scripts/check_bundle_gate.py" "$APP" --export-script "$REPO_ROOT/scripts/export_public.sh"
fi

# 4b. Zip Mach-O check: codesign cannot reach a Mach-O inside python312.zip, so none may be left there.
echo "=== Zip Mach-O check ==="
python3 "$REPO_ROOT/scripts/check_zip_macho.py" "$APP"

# 5. Signing. py2app leaves padding and a stale signature on copied libraries; trim them first.
python3 "$REPO_ROOT/scripts/normalize_bundle_macho.py" "$APP"
if [[ -n "$CERT_ID" ]]; then
    echo "=== Developer ID signing (inside-out, hardened runtime) ==="
    python3 "$REPO_ROOT/scripts/sign_app.py" app "$APP" --identity "$CERT_ID" \
        --entitlements "$REPO_ROOT/scripts/oncue.entitlements"
else
    echo "=== Ad-hoc signing ==="
    codesign --force --deep --sign - "$APP"
fi
codesign --verify --strict "$APP"

# 6. .dmg
DMG="$OUT_DIR/OnCue-${VERSION}-arm64.dmg"
mkdir -p "$OUT_DIR" "$WORK/stage"
cp -a "$APP" "$WORK/stage/"
ln -s /Applications "$WORK/stage/Applications"
hdiutil create -volname "OnCue" -srcfolder "$WORK/stage" -ov -format UDZO "$DMG" >/dev/null
echo "=== DMG: $DMG ==="
if [[ -n "$CERT_ID" ]]; then
    echo "=== Signing and notarizing the .dmg ==="
    python3 "$REPO_ROOT/scripts/sign_app.py" dmg "$DMG" --identity "$CERT_ID" \
        --profile "$SALES_COPILOT_NOTARY_PROFILE"
    echo "=== Verification ==="
    python3 "$REPO_ROOT/scripts/sign_app.py" verify "$APP" "$DMG"
fi

# 7. Size.
echo "DMG size: $(du -h "$DMG" | awk '{print $1}')"
echo "App size: $(du -sh "$APP" | awk '{print $1}')"
echo "Largest components of the bundle:"
du -sk "$APP"/Contents/Resources/lib/python*/* "$APP"/Contents/Frameworks/* \
    "$APP"/Contents/Resources/{config,dashboard,presentation,vendor,bin,scripts} 2>/dev/null \
    | sort -rn | head -3 | awk '{printf "  %.1f MB  %s\n", $1/1024, $2}'
echo "App: $APP"
