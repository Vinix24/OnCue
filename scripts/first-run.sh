#!/bin/bash
set -euo pipefail
set -o errtrace
trap 'echo "[FAILED] at line $LINENO"; exit 1' ERR

DRY_RUN=false
MODEL="large-v3-turbo"
AUTO_YES=false
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info()  { echo -e "\033[34m▸\033[0m $1"; }
ok()    { echo -e "\033[32m✓\033[0m $1"; }
warn()  { echo -e "\033[33m⚠\033[0m $1"; }
fail()  { echo -e "\033[31m✗\033[0m $1"; }

usage() {
  cat <<EOF
Usage: bash scripts/first-run.sh [options]

Options:
  --dry-run            Show all steps without executing changes.
  --model <name>       Whisper model alias: tiny|base|medium|large-v3-turbo|large-v3.
  --yes                Non-interactive mode where possible.
  -h, --help           Show this help message.
EOF
}

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] $*"
    return 0
  fi
  eval "$@"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Required command missing: $1"
    exit 1
  fi
}

check_macos_version() {
  info "Pre-flight: macOS + architecture + disk"
  require_command sw_vers
  local version
  version="$(sw_vers -productVersion)"
  local major minor
  major="${version%%.*}"
  minor="$(echo "$version" | cut -d. -f2)"

  if [[ "$major" -lt 14 ]] || { [[ "$major" -eq 14 ]] && [[ "$minor" -lt 2 ]]; }; then
    if [[ "$DRY_RUN" == "true" ]]; then
      warn "[dry-run] macOS 14.2+ required, found $version"
    else
      fail "macOS 14.2+ required, found $version"
      exit 1
    fi
  fi

  if [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]]; then
    if [[ "$DRY_RUN" == "true" ]]; then
      warn "[dry-run] Apple Silicon required (arm64)"
    else
      fail "Apple Silicon required (arm64)."
      exit 1
    fi
  fi

  local available_kb
  available_kb="$(df -Pk "$ROOT_DIR" | awk 'NR==2 {print $4}')"
  if [[ "$available_kb" -lt 5242880 ]]; then
    if [[ "$DRY_RUN" == "true" ]]; then
      warn "[dry-run] At least 5GB free disk space is required"
    else
      fail "At least 5GB free disk space is required."
      exit 1
    fi
  fi

  ok "Pre-flight checks passed"
}

install_homebrew() {
  info "Homebrew"
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found; installing via official script"
    run_cmd '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

    if [[ "$DRY_RUN" == "true" ]]; then
      ok "[dry-run] Homebrew install scripted"
      return 0
    fi

    if [[ -x "/opt/homebrew/bin/brew" ]]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
  fi

  require_command brew
  run_cmd "brew update"
  ok "Homebrew ready"
}

brew_install_formula() {
  local formula="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] brew install $formula"
    return 0
  fi
  if brew list --formula "$formula" >/dev/null 2>&1; then
    ok "brew formula already installed: $formula"
    return 0
  fi
  run_cmd "brew install $formula"
  ok "Installed formula: $formula"
}

brew_install_cask() {
  local cask="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] brew install --cask $cask"
    return 0
  fi
  if brew list --cask "$cask" >/dev/null 2>&1; then
    ok "brew cask already installed: $cask"
    return 0
  fi
  run_cmd "brew install --cask $cask"
  ok "Installed cask: $cask"
}

# blackhole-2ch backs the BlackHole fallback route (see create_audio_device
# below). The default AUDIO_CAPTURE_METHOD=audiotee auto-detects the active
# meeting app and needs no device routing at all, but it requires the
# bin/audiotee binary (OSS, built from vendor/audiotee by scripts/setup.sh or
# scripts/install.sh — not built by this first-run script) plus the macOS
# Core Audio process-tap permission. Without that binary, set
# AUDIO_CAPTURE_METHOD=blackhole in .env to use the fallback this step installs.
install_system_deps() {
  info "Installing system dependencies"
  brew_install_formula "python@3.11"
  brew_install_formula "switchaudio-osx"
  brew_install_formula "cmake"
  brew_install_formula "ffmpeg"
  brew_install_cask "blackhole-2ch"
}

setup_python_env() {
  info "Python virtual environment"
  run_cmd '"$(brew --prefix python@3.11)/bin/python3.11" -m venv "$ROOT_DIR/.venv"'
  run_cmd '"$ROOT_DIR/.venv/bin/python" -m pip install --upgrade pip'
  run_cmd '"$ROOT_DIR/.venv/bin/pip" install -e "$ROOT_DIR"'
  ok "Python environment ready"
}

# Sets up the BlackHole fallback device (Multi-Output "OnCue Output").
# Only needed for the BlackHole fallback route, or when the default AudioTee
# auto-detect path can't resolve exactly one running meeting app — the default
# audiotee route itself needs no manual audio routing.
create_audio_device() {
  info "Creating OnCue Multi-Output device (BlackHole fallback)"
  if run_cmd '"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/create_audio_setup.py"'; then
    ok "Audio setup helper completed"
    return 0
  fi

  warn "Automatic audio setup failed. Manual setup is required."
  cat <<EOF
Manual steps:
1. Open Audio MIDI Setup.
2. Create a Multi-Output Device.
3. Rename it to: OnCue Output.
4. Enable your speakers + BlackHole 2ch.
5. Set macOS output to OnCue Output.
EOF
}

download_whisper_model() {
  info "Downloading Whisper model: $MODEL"
  run_cmd '"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/install_whisper_model.py" --model "$MODEL"'
  ok "Model download step completed"
}

wait_for_enter() {
  local prompt="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] prompt: $prompt"
    return 0
  fi
  if [[ "$AUTO_YES" == "true" ]]; then
    warn "--yes enabled; skipping prompt: $prompt"
    return 0
  fi
  read -r -p "$prompt"
}

check_mic_permission() {
  info "Microphone permission check"
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] would capture 3s from default mic"
    return 0
  fi

  "$ROOT_DIR/.venv/bin/python" - <<'PY'
import sounddevice as sd

try:
    sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    print("Mic access check: OK")
except Exception as exc:
    raise SystemExit(f"Mic access check failed: {exc}")
PY
}

setup_permissions() {
  info "Opening required macOS permission panes"

  run_cmd 'open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"'
  wait_for_enter "Enable Terminal/shell mic permission, then press Enter to continue... "

  run_cmd 'open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"'
  wait_for_enter "Enable Terminal/shell screen recording permission if prompted, then press Enter... "

  check_mic_permission
  ok "Permission setup step completed"
}

setup_env_file() {
  info "Environment configuration"
  if [[ ! -f "$ROOT_DIR/.env" ]]; then
    run_cmd 'cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"'
    ok "Created .env from .env.example"
  else
    ok ".env already exists"
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] would prompt for GEMINI_API_KEY and set WHISPER_MODEL"
    return 0
  fi

  local api_key=""
  if [[ "$AUTO_YES" != "true" ]]; then
    read -r -p "GEMINI_API_KEY (enter to skip): " api_key
  fi

  if [[ -n "$api_key" ]]; then
    if grep -q '^GEMINI_API_KEY=' "$ROOT_DIR/.env"; then
      sed -i '' "s|^GEMINI_API_KEY=.*|GEMINI_API_KEY=$api_key|" "$ROOT_DIR/.env"
    else
      echo "GEMINI_API_KEY=$api_key" >> "$ROOT_DIR/.env"
    fi
  fi

  if grep -q '^WHISPER_MODEL=' "$ROOT_DIR/.env"; then
    sed -i '' 's|^WHISPER_MODEL=.*|WHISPER_MODEL=large-v3-turbo|' "$ROOT_DIR/.env"
  else
    echo 'WHISPER_MODEL=large-v3-turbo' >> "$ROOT_DIR/.env"
  fi

  ok ".env configured"
}

ask_launchd_agent() {
  info "Optional autostart agent"

  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] would prompt for launchd autostart choice"
    return 0
  fi

  local answer="N"
  if [[ "$AUTO_YES" != "true" ]]; then
    read -r -p "Auto-start OnCue on login? (y/N) " answer
  fi

  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    ok "Skipped launchd autostart"
    return 0
  fi

  local plist="$HOME/Library/LaunchAgents/nl.vnx.sales-copilot.plist"
  if [[ "$DRY_RUN" == "true" ]]; then
    info "[dry-run] would write $plist"
    return 0
  fi

  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>nl.vnx.sales-copilot</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT_DIR/.venv/bin/python</string>
    <string>-m</string>
    <string>sales_copilot</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$ROOT_DIR/data/logs/runtime.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT_DIR/data/logs/runtime.log</string>
</dict>
</plist>
EOF

  launchctl unload "$plist" >/dev/null 2>&1 || true
  launchctl load "$plist"
  ok "Launch agent installed"
}

verify_audio() {
  info "Running audio verification"
  run_cmd '"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/verify_audio.py"'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        DRY_RUN=true
        shift
        ;;
      --model)
        MODEL="${2:-}"
        if [[ -z "$MODEL" ]]; then
          fail "--model requires a value"
          exit 1
        fi
        shift 2
        ;;
      --yes)
        AUTO_YES=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        fail "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  info "OnCue first-run installer"
  check_macos_version
  install_homebrew
  install_system_deps
  setup_python_env
  create_audio_device
  download_whisper_model
  setup_permissions
  setup_env_file
  ask_launchd_agent
  verify_audio

  ok "Setup complete. Launch with: bash scripts/start.sh"
}

main "$@"
