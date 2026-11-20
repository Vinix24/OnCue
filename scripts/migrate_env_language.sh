#!/usr/bin/env bash
# Keeps an existing install in Dutch when it predates the English default.
#
# English became the default for new installs. An existing settings file that
# sets neither LANGUAGE nor CALL_LANGUAGE would silently switch language on
# update, so this appends LANGUAGE=nl and CALL_LANGUAGE=nl (once) and prints
# one line that says so and how to switch. A file that sets either key is left
# alone.
#
# Usage: bash scripts/migrate_env_language.sh <path-to-settings-file>
# Exit:  0 = nothing to do or migrated, 1 = the file is missing.
set -euo pipefail

SETTINGS_FILE="${1:-}"
if [[ -z "$SETTINGS_FILE" || ! -f "$SETTINGS_FILE" ]]; then
    echo "migrate_env_language: no settings file at '${SETTINGS_FILE}'" >&2
    exit 1
fi

# Commented-out keys (# LANGUAGE=...) do not count as set.
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?(LANGUAGE|CALL_LANGUAGE)[[:space:]]*=' "$SETTINGS_FILE"; then
    exit 0
fi

# Start on a fresh line when the file does not end in a newline.
if [[ -s "$SETTINGS_FILE" && -n "$(tail -c1 "$SETTINGS_FILE")" ]]; then
    printf '\n' >> "$SETTINGS_FILE"
fi
printf 'LANGUAGE=nl\nCALL_LANGUAGE=nl\n' >> "$SETTINGS_FILE"
echo "Existing settings kept in Dutch (LANGUAGE=nl and CALL_LANGUAGE=nl added). For English, set both to en in $SETTINGS_FILE."
