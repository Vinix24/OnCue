#!/usr/bin/env bash
# Prepare a built, notarized OnCue .dmg for the GitHub release (ADR-006 point 9).
#
#   bash scripts/release_dmg.sh <path-to-dmg>
#
# Checks the file name (OnCue-<version>-arm64.dmg, version from pyproject.toml),
# refuses a .dmg that is not stapled or fails Gatekeeper assessment, writes
# <dmg>.sha256 and prints the `gh release upload` command. It never uploads:
# publishing is done by the operator.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_REPO="Vinix24/OnCue"

die() {
  echo "release_dmg: $*" >&2
  exit 1
}

[ "$#" -eq 1 ] || die "usage: scripts/release_dmg.sh <path-to-dmg>"
DMG="$1"
[ -f "$DMG" ] || die "not a file: $DMG"

VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${REPO_ROOT}/pyproject.toml" | head -n 1)"
[ -n "$VERSION" ] || die "cannot read the version from ${REPO_ROOT}/pyproject.toml"

EXPECTED="OnCue-${VERSION}-arm64.dmg"
NAME="$(basename "$DMG")"
if [ "$NAME" != "$EXPECTED" ]; then
  die "wrong file name '${NAME}': pyproject.toml says version ${VERSION}, so the file must be named ${EXPECTED}"
fi

if ! xcrun stapler validate "$DMG" >/dev/null 2>&1; then
  die "xcrun stapler validate failed for ${NAME}: the .dmg is not notarized and stapled. Not releasing."
fi
if ! spctl --assess --type open --context context:primary-signature "$DMG" >/dev/null 2>&1; then
  die "spctl --assess failed for ${NAME}: Gatekeeper does not accept it. Not releasing."
fi

DMG_DIR="$(cd "$(dirname "$DMG")" && pwd)"
SUM_FILE="${DMG_DIR}/${NAME}.sha256"
SUM_OUT="$(cd "$DMG_DIR" && shasum -a 256 "$NAME")" || die "shasum failed for ${NAME}"
if ! printf '%s\n' "$SUM_OUT" | grep -Eq '^[0-9a-f]{64}  [^ ]'; then
  die "unexpected shasum output: ${SUM_OUT}"
fi
printf '%s\n' "$SUM_OUT" > "${SUM_FILE}.tmp"
mv "${SUM_FILE}.tmp" "$SUM_FILE"

echo "OK: ${NAME} is notarized and accepted by Gatekeeper."
echo "Checksum written to ${SUM_FILE}"
echo
echo "Upload it next to the tarball (not run by this script):"
printf 'gh release upload %q %q %q -R %q\n' "v${VERSION}" "${DMG_DIR}/${NAME}" "${SUM_FILE}" "${PUBLIC_REPO}"
