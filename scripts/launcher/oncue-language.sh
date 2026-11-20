#!/usr/bin/env bash
# Prints the UI language of an OnCue install for the launcher dialog.
#
# Usage: bash scripts/launcher/oncue-language.sh [repo-root]
#
# Reads LANGUAGE from the settings file (.env) in the install and prints it
# lowercased; whitespace around the = is fine, like in the app's dotenv loader
# and scripts/migrate_env_language.sh. Prints "en" when there is no such file or no such key: English is
# the default. The last LANGUAGE line wins, like when the file is sourced.
# repo-root defaults to the repo this script lives in, so the launcher never
# needs a hardcoded path.
set -uo pipefail

LAUNCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-$(cd "$LAUNCHER_DIR/../.." && pwd)}"
SETTINGS_FILE="$REPO_ROOT/.env"

value=""
if [[ -f "$SETTINGS_FILE" ]]; then
    value="$(sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}LANGUAGE[[:space:]]*=//p' "$SETTINGS_FILE" 2>/dev/null | tail -n 1)"
fi

# Drop CR, spaces, quotes and a trailing comment, then lowercase.
value="${value%%#*}"
value="$(printf '%s' "$value" | tr -d ' \t\r"'"'" | tr '[:upper:]' '[:lower:]')"

printf '%s\n' "${value:-en}"
