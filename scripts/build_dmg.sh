#!/bin/bash
# Build a signed and notarized OnCue .dmg from the .app bundle.
#
# Usage:
#     bash scripts/build_dmg.sh
#
# The script expects the operator to set the following environment variables
# before running:
#     SALES_COPILOT_CERT_ID      Apple Developer ID Application identity
#                                (e.g. "Developer ID Application: Your Name (TEAMID)")
#     SALES_COPILOT_APPLE_ID     Apple ID for notarization
#     SALES_COPILOT_TEAM_ID      Apple Developer Team ID
#     SALES_COPILOT_NOTARY_KEY   App-specific password or path to App Store Connect key
#
# When any of these is missing the script still builds the unsigned .app and .dmg
# and prints the exact operator command needed to sign + notarize. This keeps the
# CI/build step machine-independent; Vincent provides the Apple credentials.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_NAME="OnCue"
APP_BUNDLE="${APP_NAME}.app"
DIST_DIR="${REPO_ROOT}/dist"
DMG_DIR="${REPO_ROOT}/dmg"
DMG_NAME="${APP_NAME}-$(date +%Y%m%d).dmg"
VOL_NAME="${APP_NAME} Installer"

# ---------------------------------------------------------------------------
# 0. Validate build environment
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH." >&2
    exit 1
fi

if [[ "$(uname -m)" != "arm64" && "$(uname -m)" != "x86_64" ]]; then
    echo "WARNING: unexpected architecture $(uname -m)." >&2
fi

# ---------------------------------------------------------------------------
# 1. Install/refresh build dependencies and full runtime
# ---------------------------------------------------------------------------
echo "=== Installing build dependencies ==="
cd "${REPO_ROOT}"
python3 -m pip install --quiet py2app
python3 -m pip install --quiet -e ".[full]"

# ---------------------------------------------------------------------------
# 2. Build the .app bundle
# ---------------------------------------------------------------------------
echo "=== Building .app bundle with py2app ==="
python3 setup_app.py py2app

if [[ ! -d "${DIST_DIR}/${APP_BUNDLE}" ]]; then
    echo "ERROR: py2app did not produce ${DIST_DIR}/${APP_BUNDLE}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Optional operator hook: codesign
# ---------------------------------------------------------------------------
_sign_app() {
    local identity="${SALES_COPILOT_CERT_ID:-}"
    if [[ -z "${identity}" ]]; then
        return 0
    fi

    echo "=== Codesigning .app with identity: ${identity} ==="
    codesign --force --options runtime --deep --sign "${identity}" \
        "${DIST_DIR}/${APP_BUNDLE}"
}

_sign_app

# ---------------------------------------------------------------------------
# 4. Create .dmg
# ---------------------------------------------------------------------------
echo "=== Creating .dmg ==="
rm -rf "${DMG_DIR}"
mkdir -p "${DMG_DIR}/.background"

# Copy signed (or unsigned) app into the staging directory.
cp -a "${DIST_DIR}/${APP_BUNDLE}" "${DMG_DIR}/"

# Optional: add a background image or symlink to /Applications if the operator
# placed assets under packaging/dmg/.
if [[ -d "${REPO_ROOT}/packaging/dmg" ]]; then
    cp -R "${REPO_ROOT}/packaging/dmg/." "${DMG_DIR}/"
fi

# Create the DMG. hdiutil will clobber an existing file.
hdiutil create \
    -volname "${VOL_NAME}" \
    -srcfolder "${DMG_DIR}" \
    -ov \
    -format UDZO \
    "${DIST_DIR}/${DMG_NAME}"

echo "=== DMG created: ${DIST_DIR}/${DMG_NAME} ==="

# ---------------------------------------------------------------------------
# 5. Optional operator hook: notarize
# ---------------------------------------------------------------------------
_notarize_dmg() {
    local apple_id="${SALES_COPILOT_APPLE_ID:-}"
    local team_id="${SALES_COPILOT_TEAM_ID:-}"
    local notary_key="${SALES_COPILOT_NOTARY_KEY:-}"

    if [[ -z "${apple_id}" || -z "${team_id}" || -z "${notary_key}" ]]; then
        return 0
    fi

    echo "=== Notarizing .dmg ==="
    xcrun notarytool submit "${DIST_DIR}/${DMG_NAME}" \
        --apple-id "${apple_id}" \
        --team-id "${team_id}" \
        --password "${notary_key}" \
        --wait

    echo "=== Stapling notarization ticket ==="
    xcrun stapler staple "${DIST_DIR}/${DMG_NAME}"
}

_notarize_dmg

# ---------------------------------------------------------------------------
# 6. Report
# ---------------------------------------------------------------------------
echo ""
echo "Build complete."
echo "  App:  ${DIST_DIR}/${APP_BUNDLE}"
echo "  DMG:  ${DIST_DIR}/${DMG_NAME}"

if [[ -z "${SALES_COPILOT_CERT_ID:-}" ]]; then
    echo ""
    echo "Operator action: the bundle is unsigned. To sign and notarize, run:"
    echo "  export SALES_COPILOT_CERT_ID='Developer ID Application: ...'"
    echo "  export SALES_COPILOT_APPLE_ID='your@apple.id'"
    echo "  export SALES_COPILOT_TEAM_ID='TEAMID'"
    echo "  export SALES_COPILOT_NOTARY_KEY='your-app-specific-password'"
    echo "  bash scripts/build_dmg.sh"
fi
