#!/usr/bin/env bash
# Build OnCue's native macOS programs (whisper-cli, whisper-server, audiotee)
# once, portable, and package them as a release asset.
#
# Usage: scripts/build_macos_binaries.sh <out_dir>
#
# Output in <out_dir>:
#   oncue-macos-arm64.tar.gz          whisper-cli, whisper-server, audiotee, BUILD_INFO.txt
#   oncue-macos-arm64.tar.gz.sha256   `shasum -a 256` format
#   build.log                         full build output
#   work/                             build trees, verification copy and the tiny test model
#
# This script is the single source of truth for the release build: the
# release-macos-binaries workflow calls it, and it runs the same way locally.
# Requirements: Apple Silicon Mac, Xcode command line tools, cmake.

set -euo pipefail

DEPLOYMENT_TARGET="14.2"
ARCHIVE_NAME="oncue-macos-arm64.tar.gz"
EXPECTED_PHRASE="ask not what your country can do for you"

# Only system libraries may be linked dynamically. Anything else (@rpath,
# /opt/homebrew, a build directory) means the binary does not run elsewhere.
ALLOWED_DEP_PREFIXES=("/usr/lib/" "/System/")

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHISPER_DIR="$ROOT_DIR/vendor/whisper.cpp"
AUDIOTEE_DIR="$ROOT_DIR/vendor/audiotee"

LOG_FILE=""

progress() {
  printf '==> %s\n' "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  if [[ -n "$LOG_FILE" ]]; then
    printf 'Full output: %s\n' "$LOG_FILE" >&2
  fi
  exit 1
}

# Run a command with all output going to the log file.
run_logged() {
  local label="$1"
  shift
  {
    printf '\n##### %s\n$ %s\n' "$label" "$*"
  } >>"$LOG_FILE"
  if ! "$@" >>"$LOG_FILE" 2>&1; then
    fail "$label failed"
  fi
}

# check_no_external_deps <binary>
# Fails (return 1, reason on stderr) unless `otool -L` lists at least one
# dependency and every one is under an allowed system prefix.
check_no_external_deps() {
  local binary="$1"
  local listing dep prefix allowed count=0

  if [[ ! -f "$binary" ]]; then
    printf 'dependency check: %s does not exist\n' "$binary" >&2
    return 1
  fi
  if ! listing="$(otool -L "$binary" 2>&1)"; then
    printf 'dependency check: otool -L failed on %s: %s\n' "$binary" "$listing" >&2
    return 1
  fi

  # First line is "<binary>:"; the rest are "\t<path> (compatibility ...)".
  while IFS= read -r dep; do
    dep="${dep#"${dep%%[![:space:]]*}"}"
    dep="${dep%% (*}"
    [[ -n "$dep" ]] || continue
    count=$((count + 1))
    allowed=0
    for prefix in "${ALLOWED_DEP_PREFIXES[@]}"; do
      if [[ "$dep" == "$prefix"* ]]; then
        allowed=1
        break
      fi
    done
    if ((allowed == 0)); then
      printf 'dependency check: %s links non-system library %s\n' "$binary" "$dep" >&2
      return 1
    fi
  done < <(printf '%s\n' "$listing" | tail -n +2)

  if ((count == 0)); then
    printf 'dependency check: no dependencies parsed from otool -L of %s\n' "$binary" >&2
    return 1
  fi
  return 0
}

ensure_whisper_sources() {
  if [[ -f "$WHISPER_DIR/CMakeLists.txt" ]]; then
    return 0
  fi
  # Private repo: submodule. The public snapshot ships a plain tree, so this
  # only runs when the sources are absent.
  progress "whisper.cpp sources missing, initialising submodule"
  git -C "$ROOT_DIR" submodule update --init vendor/whisper.cpp >>"$LOG_FILE" 2>&1 \
    || fail "vendor/whisper.cpp is empty and the submodule could not be initialised"
  [[ -f "$WHISPER_DIR/CMakeLists.txt" ]] || fail "vendor/whisper.cpp has no CMakeLists.txt after init"
}

whisper_version_string() {
  local sha=""
  if [[ -e "$WHISPER_DIR/.git" ]]; then
    sha="$(git -C "$WHISPER_DIR" rev-parse HEAD 2>/dev/null || true)"
  fi
  if [[ -z "$sha" ]]; then
    sha="$(git -C "$ROOT_DIR" ls-tree HEAD vendor/whisper.cpp 2>/dev/null | awk '{print $3}' || true)"
  fi
  local cmake_version
  cmake_version="$(sed -n 's/^project("whisper.cpp" VERSION \([0-9.]*\)).*/\1/p; s/^project(whisper.cpp VERSION \([0-9.]*\)).*/\1/p' "$WHISPER_DIR/CMakeLists.txt" | head -n 1)"
  if [[ -n "$sha" ]]; then
    printf '%s (version %s)' "$sha" "${cmake_version:-unknown}"
  else
    printf 'plain source tree, version %s' "${cmake_version:-unknown}"
  fi
}

audiotee_source_hash() {
  (
    cd "$AUDIOTEE_DIR"
    find Package.swift Sources -type f | LC_ALL=C sort | while IFS= read -r f; do
      shasum -a 256 "$f"
    done | shasum -a 256 | awk '{print $1}'
  )
}

oncue_version() {
  sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -n 1
}

build_whisper() {
  local build_dir="$1"
  progress "Building whisper.cpp (whisper-cli, whisper-server; static, Metal embedded)"
  run_logged "whisper.cpp configure" cmake -S "$WHISPER_DIR" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_NATIVE=OFF \
    -DWHISPER_BUILD_TESTS=OFF \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_OSX_DEPLOYMENT_TARGET="$DEPLOYMENT_TARGET"
  run_logged "whisper.cpp build" cmake --build "$build_dir" --config Release \
    --target whisper-cli whisper-server -j "$(sysctl -n hw.ncpu)"
}

build_audiotee() {
  local scratch="$1"
  progress "Building AudioTee (swift, release, arm64)"
  run_logged "audiotee build" swift build -c release --arch arm64 \
    --package-path "$AUDIOTEE_DIR" --scratch-path "$scratch"
}

verify_binaries() {
  local dir="$1" model_dir="$2"
  local bin

  progress "Check: only system libraries are linked"
  for bin in whisper-cli whisper-server audiotee; do
    check_no_external_deps "$dir/$bin" || fail "$bin links non-system libraries"
  done

  progress "Check: code signature"
  for bin in whisper-cli whisper-server audiotee; do
    run_logged "codesign --verify $bin" codesign --verify --strict "$dir/$bin"
  done

  progress "Check: --help exits cleanly"
  run_logged "whisper-cli --help" "$dir/whisper-cli" --help
  run_logged "whisper-server --help" "$dir/whisper-server" --help
  run_logged "audiotee --help" "$dir/audiotee" --help

  progress "Check: end-to-end transcription of jfk.wav (ggml-tiny)"
  mkdir -p "$model_dir"
  run_logged "download ggml-tiny" bash "$WHISPER_DIR/models/download-ggml-model.sh" tiny "$model_dir"
  [[ -f "$model_dir/ggml-tiny.bin" ]] || fail "ggml-tiny.bin was not downloaded"
  local transcript
  transcript="$("$dir/whisper-cli" -m "$model_dir/ggml-tiny.bin" -f "$WHISPER_DIR/samples/jfk.wav" -nt 2>>"$LOG_FILE")" \
    || fail "whisper-cli transcription failed"
  printf '\n##### transcript\n%s\n' "$transcript" >>"$LOG_FILE"
  local flat
  flat="$(printf '%s' "$transcript" | tr '\n' ' ' | tr -s ' ' | tr '[:upper:]' '[:lower:]')"
  if [[ "$flat" != *"$EXPECTED_PHRASE"* ]]; then
    fail "transcript does not contain '$EXPECTED_PHRASE': $transcript"
  fi
  printf '    transcript: %s\n' "$(printf '%s' "$transcript" | tr '\n' ' ' | tr -s ' ')"
}

main() {
  if (($# != 1)); then
    printf 'Usage: %s <out_dir>\n' "$0" >&2
    exit 2
  fi
  [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] \
    || { printf 'ERROR: this build needs an Apple Silicon Mac\n' >&2; exit 1; }

  mkdir -p "$1"
  local out_dir
  out_dir="$(cd "$1" && pwd)"
  LOG_FILE="$out_dir/build.log"
  : >"$LOG_FILE"
  local work="$out_dir/work"
  mkdir -p "$work/stage" "$work/verify"

  local started=$SECONDS

  ensure_whisper_sources
  build_whisper "$work/whisper-build"
  build_audiotee "$work/swift-build"

  progress "Packaging"
  local stage="$work/stage"
  cp "$work/whisper-build/bin/whisper-cli" "$stage/whisper-cli"
  cp "$work/whisper-build/bin/whisper-server" "$stage/whisper-server"
  local audiotee_bin
  audiotee_bin="$(swift build -c release --arch arm64 --package-path "$AUDIOTEE_DIR" \
    --scratch-path "$work/swift-build" --show-bin-path 2>>"$LOG_FILE")/audiotee"
  [[ -f "$audiotee_bin" ]] || fail "audiotee binary not found at $audiotee_bin"
  cp "$audiotee_bin" "$stage/audiotee"
  chmod 755 "$stage/whisper-cli" "$stage/whisper-server" "$stage/audiotee"

  {
    printf 'whisper.cpp:        %s\n' "$(whisper_version_string)"
    printf 'audiotee source:    sha256 %s\n' "$(audiotee_source_hash)"
    printf 'oncue version:      %s\n' "$(oncue_version)"
    printf 'build date:         %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'deployment target:  macOS %s\n' "$DEPLOYMENT_TARGET"
    printf 'architecture:       arm64\n'
  } >"$stage/BUILD_INFO.txt"

  local archive="$out_dir/$ARCHIVE_NAME"
  tar -czf "$archive" -C "$stage" whisper-cli whisper-server audiotee BUILD_INFO.txt

  # Verify what users will actually download: a fresh extraction, not the build tree.
  tar -xzf "$archive" -C "$work/verify"
  verify_binaries "$work/verify" "$work/models"

  (cd "$out_dir" && shasum -a 256 "$ARCHIVE_NAME" >"$ARCHIVE_NAME.sha256")

  local elapsed=$((SECONDS - started))
  progress "Done in ${elapsed}s"
  printf '    %s (%s)\n' "$archive" "$(du -h "$archive" | awk '{print $1}')"
  printf '    %s\n' "$(cat "$archive.sha256")"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
