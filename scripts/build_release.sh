#!/usr/bin/env bash
# Build OnCue release ZIP for distribution.
# Usage:
#   bash scripts/build_release.sh [version]     # e.g. v0.1.0-beta
#   bash scripts/build_release.sh v0.1.0-beta --release  # also create gh draft
set -euo pipefail

# ── Colour helpers ──────────────────────────────────────────────────────────
_blue()  { printf "\033[34m▸\033[0m %s\n" "$*"; }
_green() { printf "\033[32m✓\033[0m %s\n" "$*"; }
_warn()  { printf "\033[33m⚠\033[0m %s\n" "$*" >&2; }
_fail()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

# ── Determine project root ───────────────────────────────────────────────────
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ── Parse arguments ──────────────────────────────────────────────────────────
VERSION="${1:-}"
CREATE_RELEASE=false
for arg in "$@"; do
    [[ "$arg" == "--release" ]] && CREATE_RELEASE=true
done

if [[ -z "$VERSION" || "$VERSION" == "--release" ]]; then
    printf "Version (e.g. v0.1.0-beta): "
    read -r VERSION
fi

if [[ -z "$VERSION" ]]; then
    _fail "Version is required."
fi

# Normalise: strip leading 'v' for directory name, keep 'v' in tag
TAG="$VERSION"
[[ "$TAG" != v* ]] && TAG="v$TAG"
CLEAN_VERSION="${TAG#v}"

DIST_NAME="sales-copilot-${TAG}"
DIST_DIR="$ROOT_DIR/dist/$DIST_NAME"
ZIP_PATH="$ROOT_DIR/dist/${DIST_NAME}.zip"

echo ""
echo "OnCue — Release builder"
echo "==============================="
echo "Version : $TAG"
echo "Output  : dist/${DIST_NAME}.zip"
echo ""

# ── Preflight checks ─────────────────────────────────────────────────────────
[[ -d "$ROOT_DIR/scripts/launcher/Start OnCue.app" ]] || _fail "Start OnCue.app not found"
[[ -d "$ROOT_DIR/scripts/launcher/Setup OnCue.app" ]] || _fail "Setup OnCue.app not found"
[[ -f "$ROOT_DIR/release/INSTRUCTIES.html" ]]                 || _fail "release/INSTRUCTIES.html not found"
[[ -f "$ROOT_DIR/release/QUICK-START.txt" ]]                  || _fail "release/QUICK-START.txt not found"
[[ -f "$ROOT_DIR/scripts/install.sh" ]]                       || _fail "scripts/install.sh not found"

# ── Prepare dist directory ───────────────────────────────────────────────────
if [[ -d "$DIST_DIR" ]]; then
    _warn "Removing existing $DIST_DIR"
    rm -rf "$DIST_DIR"
fi
mkdir -p "$DIST_DIR"
_blue "Created dist directory: $DIST_DIR"

# ── Copy launcher apps ───────────────────────────────────────────────────────
_blue "Copying launcher apps..."
cp -R "$ROOT_DIR/scripts/launcher/Start OnCue.app"  "$DIST_DIR/"
cp -R "$ROOT_DIR/scripts/launcher/Setup OnCue.app"  "$DIST_DIR/"

# Ensure setup script is executable (lost when git checkout doesn't set perms)
if [[ -f "$DIST_DIR/Setup OnCue.app/Contents/MacOS/setup" ]]; then
    chmod +x "$DIST_DIR/Setup OnCue.app/Contents/MacOS/setup"
    _green "chmod +x on Setup .app/Contents/MacOS/setup"
fi

# ── Copy user-facing files ───────────────────────────────────────────────────
_blue "Copying user-facing files..."
cp "$ROOT_DIR/release/INSTRUCTIES.html" "$DIST_DIR/"
cp "$ROOT_DIR/release/QUICK-START.txt"  "$DIST_DIR/"
cp "$ROOT_DIR/INSTALL.md"               "$DIST_DIR/"

# Copy README (trim to user-relevant sections if desired — copy as-is for now)
[[ -f "$ROOT_DIR/README.md" ]] && cp "$ROOT_DIR/README.md" "$DIST_DIR/"

# ── Copy installer and env example ──────────────────────────────────────────
_blue "Copying installer and config..."
mkdir -p "$DIST_DIR/scripts"
cp "$ROOT_DIR/scripts/install.sh" "$DIST_DIR/scripts/"
chmod +x "$DIST_DIR/scripts/install.sh"

cp "$ROOT_DIR/.env.example" "$DIST_DIR/.env.example"

# ── Copy dependency spec ─────────────────────────────────────────────────────
if [[ -f "$ROOT_DIR/pyproject.toml" ]]; then
    cp "$ROOT_DIR/pyproject.toml" "$DIST_DIR/"
elif [[ -f "$ROOT_DIR/requirements.txt" ]]; then
    cp "$ROOT_DIR/requirements.txt" "$DIST_DIR/"
fi

# ── Copy license files (AGPL-3.0 + commercial offer + third-party notices) ────
_blue "Copying license files..."
for _lic in LICENSE LICENSE-COMMERCIAL.md THIRD_PARTY_LICENSES.md; do
    [[ -f "$ROOT_DIR/$_lic" ]] && cp "$ROOT_DIR/$_lic" "$DIST_DIR/"
done

# ── Write VERSION file ───────────────────────────────────────────────────────
printf "%s\n" "$TAG" > "$DIST_DIR/VERSION"
_green "VERSION file written: $TAG"

# ── Create ZIP ───────────────────────────────────────────────────────────────
_blue "Creating ZIP..."
if [[ -f "$ZIP_PATH" ]]; then
    rm -f "$ZIP_PATH"
fi
(cd "$ROOT_DIR/dist" && zip -r --quiet "${DIST_NAME}.zip" "$DIST_NAME/")
_green "ZIP created: dist/${DIST_NAME}.zip"

# ── Print stats ──────────────────────────────────────────────────────────────
ZIP_SIZE=$(du -sh "$ZIP_PATH" | cut -f1)
SHA256=$(shasum -a 256 "$ZIP_PATH" | awk '{print $1}')

echo ""
echo "==============================="
_green "Build complete"
echo ""
echo "File     : dist/${DIST_NAME}.zip"
echo "Size     : $ZIP_SIZE"
echo "SHA256   : $SHA256"
echo ""

# ── Optional: create GitHub draft release ────────────────────────────────────
if [[ "$CREATE_RELEASE" == true ]]; then
    if ! command -v gh &>/dev/null; then
        _warn "gh CLI not found — skipping GitHub release creation."
        _warn "Install with: brew install gh && gh auth login"
    else
        _blue "Creating GitHub draft release $TAG..."
        gh release create "$TAG" \
            "$ZIP_PATH" \
            --draft \
            --title "OnCue $TAG" \
            --notes "$(cat <<EOF
## OnCue $TAG — Beta

Download het ZIP-bestand en volg de instructies in INSTRUCTIES.html.

### Installatie (non-tech)
1. Pak de ZIP uit
2. Dubbelklik \`Setup OnCue.app\`
3. Volg INSTRUCTIES.html

### Systeemvereisten
- macOS 13 (Ventura) of nieuwer
- Apple Silicon (M1/M2/M3/M4)
- 8 GB RAM minimum

### SHA256
\`$SHA256\`

Vragen of bugs? vincentvd@gmail.com
EOF
)"
        _green "GitHub draft release created: $TAG"
        echo ""
        echo "Bekijk en publiceer via:"
        echo "  gh release view $TAG --web"
    fi
else
    echo "Klaar voor GitHub Release. Draai:"
    echo "  bash scripts/build_release.sh $TAG --release"
    echo ""
    echo "Of maak handmatig een draft aan:"
    printf "  gh release create %s dist/%s.zip --draft --title \"OnCue %s\"\n" \
        "$TAG" "$DIST_NAME" "$TAG"
fi

echo ""
