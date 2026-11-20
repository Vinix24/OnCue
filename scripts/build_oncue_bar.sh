#!/usr/bin/env bash
# Builds native/oncue-bar in release mode and assembles "OnCue Bar.app" in <out_dir>.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT_DIR/native/oncue-bar"
BUNDLE_ID="nl.vnxdigital.oncue.bar"
MIN_MACOS="14.2"

die() { echo "build_oncue_bar: $*" >&2; exit 1; }

[[ $# -eq 1 && -n "$1" ]] || die "usage: scripts/build_oncue_bar.sh <out_dir>"
OUT_DIR="$1"
[[ -d "$OUT_DIR" ]] || mkdir -p "$OUT_DIR" || die "cannot create $OUT_DIR"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -n 1)"
[[ -n "$version" ]] || die "no version found in pyproject.toml"
short_version="$(printf '%s' "$version" | sed -n 's/^\([0-9][0-9.]*\).*/\1/p')"
short_version="${short_version%.}"
[[ -n "$short_version" ]] || die "cannot derive a numeric version from '$version'"

swift build -c release --package-path "$PACKAGE_DIR"
bin_dir="$(swift build -c release --package-path "$PACKAGE_DIR" --show-bin-path)"
[[ -x "$bin_dir/OnCueBar" ]] || die "swift build produced no OnCueBar executable in $bin_dir"

app="$OUT_DIR/OnCue Bar.app"
mkdir -p "$app/Contents/MacOS"
cp "$bin_dir/OnCueBar" "$app/Contents/MacOS/OnCueBar"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>OnCueBar</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>OnCue Bar</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>CFBundleShortVersionString</key><string>$short_version</string>
  <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

codesign --force --sign - "$app"
codesign --verify --strict "$app"
echo "Built $app (version $version)"
