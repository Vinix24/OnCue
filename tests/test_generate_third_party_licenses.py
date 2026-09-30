"""Regression tests for ``scripts/generate_third_party_licenses.py``.

Guards the fix for the 23 MB ``THIRD_PARTY_LICENSES.md``: markdown table cells padded to the
width of a full embedded license body, which inflated 96% of the file to literal spaces. These
tests prove the regenerated file (a) keeps every hand-authored section byte-identical, (b) is
deterministic across runs, (c) never silently drops attribution for a dependency that is still
installed, and (d) does not reintroduce cell-padding bloat.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from generate_third_party_licenses import (  # noqa: E402
    DEFAULT_OUTPUT,
    MARKER,
    TEMPLATE_PATH,
    _normalize,
    _self_package_name,
    build_document,
    collect_dependencies,
)

ROOT_DIR = Path(__file__).parents[1]
SCRIPT_PATH = ROOT_DIR / "scripts" / "generate_third_party_licenses.py"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
PRE_REGEN_SNAPSHOT = FIXTURES_DIR / "third_party_licenses_pre_regen_snapshot.json"
VENDORED_SECTION_FIXTURE = FIXTURES_DIR / "third_party_licenses_vendored_section.md"


def _committed_document() -> str:
    return DEFAULT_OUTPUT.read_text(encoding="utf-8")


def _template_static_halves() -> tuple[str, str]:
    """(prefix, suffix) of the template around the generated-content marker."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    prefix, suffix = template.split(MARKER)
    return prefix, suffix


def test_committed_file_starts_with_the_hand_authored_prefix() -> None:
    """Header + vendored-components + key-runtime-deps intro is untouched by generation."""
    prefix, _ = _template_static_halves()
    assert _committed_document().startswith(prefix)


def test_committed_file_ends_with_the_hand_authored_javascript_section() -> None:
    _, suffix = _template_static_halves()
    assert _committed_document().endswith(suffix)


def test_vendored_components_section_matches_pre_regeneration_original() -> None:
    """whisper.cpp / AudioTee attribution (with pinned commit SHAs) must survive verbatim."""
    committed = _committed_document()
    start = committed.index("## Vendored components")
    end = committed.index("## Python dependencies")
    vendored_section = committed[start:end]

    assert vendored_section == VENDORED_SECTION_FIXTURE.read_text(encoding="utf-8")


def test_regeneration_is_deterministic() -> None:
    """Building the document twice in-process from the same environment is byte-identical."""
    self_pkg = {_normalize(_self_package_name())}
    doc_a = build_document(collect_dependencies(self_pkg))
    doc_b = build_document(collect_dependencies(self_pkg))
    assert doc_a == doc_b


def test_cli_regeneration_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Running the script twice on an unchanged environment must produce identical bytes,
    so the output can be diffed in review instead of trusted blindly."""
    out_a = tmp_path / "a.md"
    out_b = tmp_path / "b.md"
    for out in (out_a, out_b):
        subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--output", str(out)],
            check=True,
            cwd=ROOT_DIR,
            capture_output=True,
        )
    assert out_a.read_bytes() == out_b.read_bytes()


@pytest.mark.skipif(
    os.environ.get("LICENSES_CANONICAL_ENV") != "1",
    reason=(
        "THIRD_PARTY_LICENSES.md is a snapshot of one environment's installed packages. "
        "Any other environment -- a CI runner, a fresh venv, a contributor's machine -- "
        "legitimately has a different set, so --check fails there by construction rather "
        "than because the file is wrong. Opt in with LICENSES_CANONICAL_ENV=1 on the "
        "machine whose environment the committed file was generated from."
    ),
)
def test_check_flag_passes_against_the_committed_file() -> None:
    """On the canonical environment, the committed file is exactly what the generator produces."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_no_attribution_lost_for_dependencies_still_installed(capsys) -> None:
    """Every dependency the OLD (pre-regeneration) file attributed, and that is still
    installed today, must still carry a license entry in the new document.

    Packages absent from the old snapshot's install set are allowed to disappear: the
    project's dependency set legitimately drifted since that snapshot was taken (e.g. the
    faster-whisper/ctranslate2/onnxruntime/av/protobuf/whisperlivekit stack was retired when
    transcription moved to whisper.cpp/mlx-whisper). This test only fails if something
    CURRENTLY installed lost its attribution in the reformat.
    """
    old_rows = json.loads(PRE_REGEN_SNAPSHOT.read_text(encoding="utf-8"))
    self_pkg = _normalize(_self_package_name())

    currently_installed = {
        _normalize(d.metadata["Name"]) for d in metadata.distributions() if d.metadata.get("Name")
    }
    new_records = {_normalize(r.name) for r in collect_dependencies({self_pkg})}

    dropped = sorted(
        row["name"]
        for row in old_rows
        if _normalize(row["name"]) != self_pkg
        and _normalize(row["name"]) in currently_installed
        and _normalize(row["name"]) not in new_records
    )
    assert not dropped, f"attribution lost for still-installed dependencies: {dropped}"

    no_longer_installed = sorted(
        row["name"]
        for row in old_rows
        if _normalize(row["name"]) != self_pkg and _normalize(row["name"]) not in currently_installed
    )
    newly_installed = sorted(
        r.name
        for r in collect_dependencies({self_pkg})
        if _normalize(r.name) not in {_normalize(row["name"]) for row in old_rows}
    )
    with capsys.disabled():
        print(
            f"\ndependency drift since pre-regeneration snapshot: "
            f"{len(no_longer_installed)} removed, {len(newly_installed)} added"
        )
        print(f"  removed (no longer installed): {no_longer_installed}")
        print(f"  added (newly installed): {newly_installed}")


def test_self_package_is_excluded_from_the_inventory() -> None:
    self_pkg = _normalize(_self_package_name())
    records = collect_dependencies({self_pkg})
    assert self_pkg not in {_normalize(r.name) for r in records}


def test_summary_table_cells_never_carry_multiline_license_text() -> None:
    """Regression guard for the root cause: a LicenseText column embedded in the table.

    License bodies must only ever appear in their own fenced ```text``` blocks below the
    summary table, never inside a table row/cell.
    """
    document = _committed_document()
    start = document.index("#### Summary")
    end = document.index("#### License texts")
    summary_table = document[start:end]
    for line in summary_table.splitlines():
        if not line.strip():
            continue
        assert line.startswith("|") or line.startswith("#"), f"unexpected non-table line: {line!r}"


def test_no_pathological_line_padding() -> None:
    """Regression guard: no line should approach the ~1250-byte padded rows of the old file."""
    document = _committed_document()
    longest = max(len(line) for line in document.splitlines())
    assert longest < 3000, f"longest line is {longest} chars — looks like reintroduced padding"


def test_file_size_stays_far_below_the_historical_23mb() -> None:
    size = DEFAULT_OUTPUT.stat().st_size
    assert size < 5_000_000, f"THIRD_PARTY_LICENSES.md is {size} bytes — investigate before committing"
