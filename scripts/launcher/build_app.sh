#!/usr/bin/env bash
# Idempotently (re)builds the "Start OnCue.app" AppleScript-applet launcher
# used to place OnCue in the Dock, and stamps it with the OnCue icon.
#
# scripts/launcher/*.app/ is gitignored (see .gitignore), so this script is
# the reproducible source of truth for that bundle instead of a binary
# checked into git. Re-run any time start-oncue.command's launch behaviour
# or the icon changes.
#
# Usage:
#   bash scripts/launcher/build_app.sh
set -euo pipefail

_blue()  { printf "\033[34m▸\033[0m %s\n" "$*"; }
_green() { printf "\033[32m✓\033[0m %s\n" "$*"; }
_warn()  { printf "\033[33m⚠\033[0m %s\n" "$*" >&2; }
_fail()  { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$LAUNCHER_DIR/Start OnCue.app"
OLD_APP_PATH="$LAUNCHER_DIR/Start Sales Copilot.app"
ICNS_PATH="$LAUNCHER_DIR/icon/sales-copilot.icns"

command -v osacompile >/dev/null 2>&1 || _fail "osacompile not found — this script only runs on macOS."
HAVE_ICON=true
if [[ ! -f "$ICNS_PATH" ]]; then
    HAVE_ICON=false
    _warn "Icon not found at $ICNS_PATH — building the app with the default applet icon. Run 'python3 scripts/launcher/icon/generate_icon.py' to get the OnCue icon."
fi

TMP_SCRIPT="$(mktemp -t oncue_launcher_source)"
trap 'rm -f "$TMP_SCRIPT"' EXIT

# A thin layer: show the mode choice, then delegate the actual launch work to
# oncue-launch.sh (scripts/launcher/oncue-launch.sh) so this AppleScript and
# start-oncue.command are no longer two copies of the same launch logic.
cat > "$TMP_SCRIPT" <<'APPLESCRIPT'
on run
	try
		-- Find oncue-launch.sh: bundle-relative when the app lives inside the
		-- repo, otherwise the same override/remembered-path ladder the script
		-- itself uses. The script does the rest of the validation.
		set appPath to POSIX path of (path to me)
		set bundleRelative to appPath & "../oncue-launch.sh"
		set launcherScript to ""

		try
			set envRepo to (system attribute "ONCUE_REPO")
			if envRepo is not "" then
				set candidate to envRepo & "/scripts/launcher/oncue-launch.sh"
				if (do shell script "[[ -f " & quoted form of candidate & " ]] && echo yes || echo no") is "yes" then
					set launcherScript to candidate
				end if
			end if
		end try

		if launcherScript is "" then
			if (do shell script "[[ -f " & quoted form of bundleRelative & " ]] && echo yes || echo no") is "yes" then
				set launcherScript to bundleRelative
			end if
		end if

		if launcherScript is "" then
			set storedPath to do shell script "cat ~/.oncue/repo-path 2>/dev/null || true"
			if storedPath is not "" then
				set candidate to storedPath & "/scripts/launcher/oncue-launch.sh"
				if (do shell script "[[ -f " & quoted form of candidate & " ]] && echo yes || echo no") is "yes" then
					set launcherScript to candidate
				end if
			end if
		end if

		if launcherScript is "" then
			-- No install is known yet, so there is no language setting to read: English.
			set chosenFolder to POSIX path of (choose folder with prompt "Choose the OnCue project folder")
			set candidate to chosenFolder & "scripts/launcher/oncue-launch.sh"
			if (do shell script "[[ -f " & quoted form of candidate & " ]] && echo yes || echo no") is "yes" then
				set launcherScript to candidate
				do shell script "mkdir -p ~/.oncue && printf '%s' " & quoted form of chosenFolder & " | sed 's:/$::' > ~/.oncue/repo-path"
			else
				display alert "OnCue" message "No valid OnCue install found in " & chosenFolder
				return
			end if
		end if

		-- The dialog follows the install's language: LANGUAGE in the settings file
		-- of the install this app launches (English unless it says nl). Read at
		-- runtime from the located install, never from a hardcoded path.
		set languageScript to (do shell script "dirname " & quoted form of launcherScript) & "/oncue-language.sh"
		set repoRoot to do shell script "cd \"$(dirname " & quoted form of launcherScript & ")/../..\" && pwd"
		set uiLanguage to do shell script "bash " & quoted form of languageScript & " " & quoted form of repoRoot

		if uiLanguage is "nl" then
			set dialogText to "Wat wil je doen?

Opnemen en uitschrijven: neemt beide kanten van het gesprek op en geeft je daarna een leesbaar transcript. Alles blijft op deze computer.

Live coach: de volledige versie van het dashboard met live transcriptie, detectie en coaching."
			set cancelLabel to "Annuleer"
			set coachLabel to "Live coach"
			set recordLabel to "Opnemen en uitschrijven"
			set dashboardNote to "Dashboard + presentatie open. Klaar voor call."
			set recordNote to "Opnemen klaar. Twee meters en de timer staan open."
		else
			set dialogText to "What do you want to do?

Record and transcribe: records both sides of the call and gives you a readable transcript afterwards. Everything stays on this computer.

Live coach: the full dashboard with live transcription, detection and coaching."
			set cancelLabel to "Cancel"
			set coachLabel to "Live coach"
			set recordLabel to "Record and transcribe"
			set dashboardNote to "Dashboard + presentation open. Ready for the call."
			set recordNote to "Recording ready. Two meters and the timer are open."
		end if

		set userChoice to button returned of (display dialog dialogText buttons {cancelLabel, coachLabel, recordLabel} default button recordLabel with title "OnCue")

		if userChoice is cancelLabel then
			return
		end if

		if userChoice is coachLabel then
			set launchMode to "dashboard"
		else
			set launchMode to "record"
		end if

		do shell script "bash " & quoted form of launcherScript & " " & launchMode

		if launchMode is "dashboard" then
			display notification dashboardNote with title "OnCue Ready" sound name "Glass"
		else
			display notification recordNote with title "OnCue Ready" sound name "Glass"
		end if
	on error errMsg number errNum
		if errNum is -128 then
			-- User cancelled the dialog or the folder picker — do nothing.
			return
		end if
		display alert "OnCue Start Failed" message errMsg
	end try
end run
APPLESCRIPT

_blue "Compiling $APP_PATH ..."
rm -rf "$APP_PATH"
osacompile -l AppleScript -o "$APP_PATH" "$TMP_SCRIPT"

_blue "Setting CFBundleName to OnCue ..."
/usr/libexec/PlistBuddy -c "Set :CFBundleName OnCue" "$APP_PATH/Contents/Info.plist"

if [[ "$HAVE_ICON" == true ]]; then
    _blue "Installing icon ..."
    cp "$ICNS_PATH" "$APP_PATH/Contents/Resources/applet.icns"
fi
touch "$APP_PATH"

_blue "Re-signing bundle (ad-hoc) ..."
# osacompile ondertekent ad-hoc tijdens het compileren; de PlistBuddy-
# aanpassing en de icoon-kopie hierboven verbreken dat zegel. Re-signen moet
# daarom de laatste bundle-mutatie zijn.
codesign --force --sign - "$APP_PATH"
codesign --verify --strict "$APP_PATH" || _fail "codesign verify failed for $APP_PATH"

if [[ -d "$OLD_APP_PATH" ]]; then
    _warn "Removing stale $OLD_APP_PATH"
    rm -rf "$OLD_APP_PATH"
fi

_green "Built $APP_PATH"
