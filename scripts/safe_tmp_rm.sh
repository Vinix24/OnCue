#!/bin/bash
# Guarded removal of a temp folder, sourced by build_dmg.sh and smoke_app.sh.
#
#   safe_tmp_rm <dir>   removes <dir> only when its resolved path (pwd -P) is a directory strictly
#                       inside the one root the callers create their folder in: the resolved
#                       "${TMPDIR:-/tmp}" (/tmp only when TMPDIR is unset or empty). No other
#                       root is accepted. Refuses, says so on stderr and returns 1 for an empty
#                       path, /, $HOME, the root itself, a missing directory or anything outside
#                       the root. Nothing is removed then.

safe_tmp_rm() {
    local target="${1:-}" resolved home_real root root_real ok=0
    if [[ -z "$target" ]]; then
        echo "safe_tmp_rm: refusing an empty path" >&2
        return 1
    fi
    if [[ ! -d "$target" ]]; then
        echo "safe_tmp_rm: refusing $target: not a directory" >&2
        return 1
    fi
    resolved="$(cd "$target" 2>/dev/null && pwd -P)" || { echo "safe_tmp_rm: cannot resolve $target" >&2; return 1; }
    home_real="$(cd "${HOME:-/nonexistent}" 2>/dev/null && pwd -P || true)"
    if [[ -z "$resolved" || "$resolved" == "/" || ( -n "$home_real" && "$resolved" == "$home_real" ) ]]; then
        echo "safe_tmp_rm: refusing $resolved" >&2
        return 1
    fi
    # The only allowed root is the one the callers pass to mktemp: "${TMPDIR:-/tmp}".
    root="${TMPDIR:-/tmp}"
    root_real="$(cd "$root" 2>/dev/null && pwd -P || true)"
    if [[ -n "$root_real" && "$root_real" != "/" ]]; then
        # Strictly below the root: never the root itself.
        case "$resolved" in "$root_real"/?*) ok=1 ;; esac
    fi
    if ((! ok)); then
        echo "safe_tmp_rm: refusing $resolved: not under the temp directory" >&2
        return 1
    fi
    rm -rf -- "$resolved"
}
