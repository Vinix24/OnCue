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
[[ -f "$ICNS_PATH" ]] || _fail "Icon not found at $ICNS_PATH — run 'python3 scripts/launcher/icon/generate_icon.py' first."

TMP_SCRIPT="$(mktemp -t oncue_launcher_source)"
trap 'rm -f "$TMP_SCRIPT"' EXIT

# A thin layer: show the mode choice, then delegate the actual launch work to
# oncue-launch.sh (scripts/launcher/oncue-launch.sh) so this AppleScript and
# start-oncue.command are no longer two copies of the same launch logic.
cat > "$TMP_SCRIPT" <<'APPLESCRIPT'
on run
	try
		set userChoice to button returned of (display dialog "Wat wil je doen?

Meteen opnemen — alleen opnemen, met twee volume-meters en een timer. Je ziet direct of beide kanten binnenkomen. Geen dashboard.

Dashboard — de volledige versie: live transcriptie, detectie, coaching en presentatie." buttons {"Annuleer", "Dashboard", "Meteen opnemen"} default button "Meteen opnemen" with title "OnCue")

		if userChoice is "Annuleer" then
			return
		end if

		if userChoice is "Dashboard" then
			set launchMode to "dashboard"
		else
			set launchMode to "record"
		end if

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
			set chosenFolder to POSIX path of (choose folder with prompt "Kies de OnCue-projectmap")
			set candidate to chosenFolder & "scripts/launcher/oncue-launch.sh"
			if (do shell script "[[ -f " & quoted form of candidate & " ]] && echo yes || echo no") is "yes" then
				set launcherScript to candidate
				do shell script "mkdir -p ~/.oncue && printf '%s' " & quoted form of chosenFolder & " | sed 's:/$::' > ~/.oncue/repo-path"
			else
				display alert "OnCue" message "Geen geldige OnCue-installatie gevonden in " & chosenFolder
				return
			end if
		end if

		do shell script "bash " & quoted form of launcherScript & " " & launchMode

		if launchMode is "dashboard" then
			display notification "Dashboard + presentatie open. Klaar voor call." with title "OnCue Ready" sound name "Glass"
		else
			display notification "Opnemen klaar. Twee meters en de timer staan open." with title "OnCue Ready" sound name "Glass"
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

_blue "Installing icon ..."
cp "$ICNS_PATH" "$APP_PATH/Contents/Resources/applet.icns"
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
