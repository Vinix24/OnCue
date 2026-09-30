"""Fail-closed guard: the global transcript normalization config must never
carry a real client, company, or person name.

Fix-forward on #253 (dispatch D-3cbcb3ba). The original D2 config
(`config/transcript_normalization.yaml`) shipped with real client/company/
person names (a customer's company name and its misspellings, its parent
company's name, and the customer of this project itself). Loaded once per
process and applied to EVERY client's calls, those names leaked into every
other client's normalization pass, and the file also ships in the public
export (`scripts/export_public.sh`) whose own fail-closed name check did not
happen to cover all of them. Client- or call-specific terms belong
per-conversation instead, under `termen` in that client's `klant.yaml` (see
docs/CONFIG.md), never in this global file.

This test's denylist has two independent sources:

- Real client folder names under `context_docs.UPLOAD_ROOT` (``KLANTEN_ROOT``),
  when they happen to exist on the machine running the test. Empty in CI and
  on any machine without local client data -- by design, this test never
  *depends* on real names to prove the scan works.
- A purely synthetic name (never a real client), used to prove the scan
  mechanism actually flags a match instead of being a silent no-op. No real
  client name is ever written into this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core import context_docs

NORMALIZATION_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "transcript_normalization.yaml"

# Purely synthetic -- never a real client, company, or person name -- used
# only to prove the denylist scan actually flags a match.
_SYNTHETIC_KLANT_NAME = "wattelaar turbines"


def _scan_for_denylisted_names(text: str, denylist: set[str]) -> list[str]:
    """Case-insensitive substring scan; returns the denylist entries that hit."""
    lowered = text.lower()
    return sorted(name for name in denylist if name and name.lower() in lowered)


def _klant_denylist_from_local_folders() -> set[str]:
    """Real client folder names under KLANTEN_ROOT, if any exist on this machine.

    Every folder name is a real client's slug when local client data is
    present (an operator's own machine); nothing here is committed to the
    repo, and the set is empty wherever KLANTEN_ROOT / the default
    data/clients app-support path doesn't exist (every CI runner, every
    fresh checkout).
    """
    root = context_docs.UPLOAD_ROOT
    if not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}


class TestNoRealClientNamesInGlobalConfig:
    def test_global_config_has_no_local_klant_folder_names(self) -> None:
        denylist = _klant_denylist_from_local_folders()
        if not denylist:
            pytest.skip("no local KLANTEN_ROOT client folders on this machine")
        text = NORMALIZATION_CONFIG_PATH.read_text(encoding="utf-8")
        hits = _scan_for_denylisted_names(text, denylist)
        assert hits == [], f"klant-namen gevonden in globale normalisatielijst: {hits}"

    def test_scan_flags_a_synthetic_klant_name_when_present(self, tmp_path: Path) -> None:
        """Proves the scan is not a silent no-op: a poisoned copy must fail."""
        poisoned = tmp_path / "poisoned.yaml"
        base = NORMALIZATION_CONFIG_PATH.read_text(encoding="utf-8")
        poisoned.write_text(
            base + f"\nvariants:\n  Wattlaar: {_SYNTHETIC_KLANT_NAME.title()}\n",
            encoding="utf-8",
        )
        hits = _scan_for_denylisted_names(
            poisoned.read_text(encoding="utf-8"), {_SYNTHETIC_KLANT_NAME}
        )
        assert hits == [_SYNTHETIC_KLANT_NAME]

    def test_scan_is_clean_on_the_real_shipped_config(self) -> None:
        """The actual shipped file, scanned against the synthetic name, is clean."""
        text = NORMALIZATION_CONFIG_PATH.read_text(encoding="utf-8")
        hits = _scan_for_denylisted_names(text, {_SYNTHETIC_KLANT_NAME})
        assert hits == []
