#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
else
    # Worktree: geen eigen .venv — val terug op de venv van de hoofd-checkout.
    MAIN_DIR="$(dirname "$(git -C "$ROOT_DIR" rev-parse --path-format=absolute --git-common-dir)")"
    if [[ -x "$MAIN_DIR/.venv/bin/python" ]]; then
        PYTHON_BIN="${PYTHON_BIN:-$MAIN_DIR/.venv/bin/python}"
    else
        PYTHON_BIN="${PYTHON_BIN:-python}"
    fi
fi

# Test altijd de source van DEZE checkout, niet de editable install van de
# hoofd-checkout (relevant zodra PYTHON_BIN uit een andere checkout komt).
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

echo "=== Live Sales Copilot — CI Gate ==="

# Gate 1: Lint
echo "--- [1/6] Ruff lint ---"
"$PYTHON_BIN" -m ruff check src/ tests/ $(git ls-files 'scripts/*.py' 'scripts/**/*.py')

# Gate 2: Tests
echo "--- [2/6] Pytest ---"
"$PYTHON_BIN" -m pytest tests/ -q

# Gate 3: License-worker TS suite (vitest + tsc)
echo "--- [3/6] License-worker TS suite (vitest + tsc) ---"
if command -v npm &>/dev/null; then
    cd "$ROOT_DIR/server/license-worker"
    npm ci
    npm test
    npx tsc --noEmit
    cd "$ROOT_DIR"
else
    echo "[WARN] npm not found — skipping license-worker TS suite"
fi

# Gate 4: Lockfile consistency (uv.lock must match pyproject.toml)
echo "--- [4/6] Lockfile consistency ---"
if command -v uv &>/dev/null; then
    uv lock --locked
    echo "uv.lock is consistent with pyproject.toml"
else
    echo "[WARN] uv not found — skipping lockfile consistency check"
fi

# Gate 4: Dependency vulnerability audit
echo "--- [5/6] Dependency audit ---"
# PYSEC-2026-3447 (setuptools 81.0.0): macOS-sdist MANIFEST.in Unicode-normalisatie
# packaging-bug, fixed in setuptools>=83.0.0. Geen runtime- of Windows-install-risico.
# Upgrade geblokkeerd door de torch<82-pin uit de diarization-extra. Precedent:
# CVE-2025-3000 hieronder. Accept + monitor (zie claudedocs/2026-06-12-dependency-audit.md).
"$PYTHON_BIN" -m pip_audit --skip-editable --ignore-vuln CVE-2025-3000 --ignore-vuln PYSEC-2026-3447

# Gate 5: SBOM generation
# cyclonedx-py CLI not installed. Install via: pip install cyclonedx-bom
# Then run: cyclonedx-py environment --of JSON -o sbom.json
# See claudedocs/2026-06-12-dependency-audit.md § SBOM for instructions.
echo "--- [6/6] SBOM ---"
if command -v cyclonedx-py &>/dev/null; then
    cyclonedx-py environment --of JSON -o sbom.json
    echo "SBOM written to sbom.json"
else
    echo "[SKIP] cyclonedx-py not installed — SBOM generation skipped (see claudedocs/2026-06-12-dependency-audit.md)"
fi

# Gate 6: PRO_UPGRADE_URL single source of truth
echo "--- [+] PRO_UPGRADE_URL single-source-of-truth ---"
PRO_URL_LITERAL="salescopilot.app/pro"
PRO_URL_ALLOWED_FILE="dashboard/js/constants.js"
pro_url_hits=$(git grep -n --fixed-strings "$PRO_URL_LITERAL" -- . ":(exclude)$PRO_URL_ALLOWED_FILE" ":(exclude)scripts/ci.sh" 2>/dev/null || true)
if [[ -n "$pro_url_hits" ]]; then
    echo "[FAIL] '$PRO_URL_LITERAL' literal found outside $PRO_URL_ALLOWED_FILE:"
    echo "$pro_url_hits"
    exit 1
fi
echo "PRO_UPGRADE_URL single source of truth OK ($PRO_URL_ALLOWED_FILE)"

# Gate 7: Import sanity
echo "--- [+] Import sanity ---"
"$PYTHON_BIN" - <<'PY'
import importlib

modules = [
    "sales_copilot.core.config",
    "sales_copilot.audio.capture",
    "sales_copilot.websocket.hub",
    "sales_copilot.modules.talk_time",
]

for module in modules:
    importlib.import_module(module)

print("Import checks passed")
PY

# Gate 8: Release-build auth hardening
# Simulates a release artifact and proves the dev/test key is absent, an
# untrusted-pubkey key is rejected even with SCP_DEV_BUILD=1, and a
# production-signed key still verifies. Injects a TEST prod pubkey via env, so
# it does not depend on the real _PROD_PUBKEY_HEX being embedded. set -e fails
# the gate on any non-zero exit.
echo "--- [+] Auth hardening (release-build key trust) ---"
"$PYTHON_BIN" scripts/ci_auth_hardening_check.py

echo "=== CI gate passed ==="
