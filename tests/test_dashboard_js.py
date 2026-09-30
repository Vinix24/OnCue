"""Run the committed dashboard-JS harness (tests/js/) from the Python suite.

There was no test runner for dashboard/js/*.js in this repo: no package.json,
nothing. #206 verified its module-checkbox choke point with a throwaway Node
harness -- node:vm plus a hand-rolled DOM stub, no jsdom, no dependency -- and
then had to discard it, so none of that behaviour was protected.

The harness is now committed under tests/js/ in the same shape, and this module
is how it enters the test surface CI already runs: `pytest tests/` shells out to
tests/js/run_harness.mjs. That keeps the "no build step, no framework" invariant
that scripts/check_architecture_boundaries.py enforces -- there is still no
package.json, no node_modules and no bundler anywhere in the repo.

**No reporter output is parsed.** The first version of this wrapper matched
`node --test`'s TAP summary, which is what the pinned Node 22.22.3 writes into a
pipe locally; the CI runner's Node wrote the `spec` reporter into the same pipe
and the wrapper reported a fully green harness as a failure (PR #214). The
reporter is an environment-dependent presentation layer, so run_harness.mjs
consumes node:test's structured event stream instead and prints a summary in a
shape this repo owns. This wrapper asserts on its exit code plus those counts.

The harness is skipped when Node is absent, matching how scripts/ci.sh already
treats its npm-dependent gate. tests/js/ uses only node:test, node:assert,
node:vm, node:fs, node:path and node:url, all built in since Node 18; .nvmrc
pins 22.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_JS_TESTS = _ROOT / "tests" / "js"
_DASHBOARD = _ROOT / "dashboard"
_DEMO = _ROOT / "demo"
_RUNNER = _JS_TESTS / "run_harness.mjs"

_SUPPORT_FILES = ("dom_stub.mjs", "dashboard_fixture.mjs", "run_harness.mjs")

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    _NODE is None,
    reason="node not found -- the dashboard JS harness needs a Node runtime (see .nvmrc)",
)


def _test_files() -> list[Path]:
    """Glob the harness's test files independently of the runner's own glob."""
    return sorted(_JS_TESTS.glob("*.test.mjs"))


def _run_harness() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [_NODE, str(_RUNNER)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _report(result: subprocess.CompletedProcess[str]) -> str:
    """Everything a reader needs to diagnose this without re-running CI."""
    return (
        f"exit code: {result.returncode}\n"
        f"--- stdout ---\n{result.stdout or '(empty)'}\n"
        f"--- stderr ---\n{result.stderr or '(empty)'}"
    )


# Real import statements only, anchored to the start of a line. A loose
# `from "..."` match also hits prose inside a comment (it did, on this file's
# own first draft) and would fail the dependency check on English, not on code.
_IMPORT_RES = (
    re.compile(r'^\s*(?:import|export)\b[^;\n]*?\bfrom\s+"([^"]+)"', re.MULTILINE),
    re.compile(r'^\s*import\s+"([^"]+)"', re.MULTILINE),
    re.compile(r'\bimport\(\s*"([^"]+)"\s*\)'),
)


def _imported_specifiers(source: str) -> set[str]:
    return {match for pattern in _IMPORT_RES for match in pattern.findall(source)}


def _counts(stdout: str) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in re.findall(r"^HARNESS_(FILES|PASS|FAIL) (\d+)$", stdout, re.MULTILINE)
    }


def test_harness_files_are_present() -> None:
    """A missing harness file must fail loudly, not silently run fewer tests."""
    for name in _SUPPORT_FILES:
        assert (_JS_TESTS / name).is_file(), f"tests/js/{name} is missing"
    assert _test_files(), "tests/js/ contains no *.test.mjs files"


def test_dashboard_js_harness_passes() -> None:
    """Exit code plus the runner's own counts -- never a reporter's prose."""
    result = _run_harness()

    assert result.returncode == 0, f"dashboard JS harness failed:\n{_report(result)}"

    counts = _counts(result.stdout)
    assert counts.keys() == {"FILES", "PASS", "FAIL"}, (
        f"run_harness.mjs did not print its summary:\n{_report(result)}"
    )
    assert counts["FAIL"] == 0, f"harness reported failures:\n{_report(result)}"

    # A green harness must be distinguishable from one that ran nothing: every
    # test file has to have contributed, and at least one assertion per file.
    expected_files = len(_test_files())
    assert counts["FILES"] == expected_files, (
        f"run_harness.mjs ran {counts['FILES']} file(s) but tests/js/ holds "
        f"{expected_files}:\n{_report(result)}"
    )
    assert counts["PASS"] >= expected_files, (
        f"harness collected only {counts['PASS']} test(s) across {expected_files} "
        f"file(s):\n{_report(result)}"
    )


def test_harness_reports_the_node_it_ran_on() -> None:
    """The reporter mismatch that broke this wrapper was invisible because
    nothing recorded which Node ran. It does now, in every run's output."""
    result = _run_harness()
    assert re.search(r"^HARNESS_NODE v\d+\.", result.stdout, re.MULTILINE), (
        f"run_harness.mjs did not report its Node version:\n{_report(result)}"
    )


def test_no_build_step_was_introduced() -> None:
    """The harness must stay dependency-free -- no package manifest, no bundler."""
    forbidden = ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "node_modules")
    for name in forbidden:
        assert not (_ROOT / name).exists(), f"{name} at repo root breaks the no-build-step invariant"
        assert not (_JS_TESTS / name).exists(), f"tests/js/{name} breaks the no-build-step invariant"
    for path in _JS_TESTS.glob("*.mjs"):
        source = path.read_text(encoding="utf-8")
        for spec in _imported_specifiers(source):
            assert spec.startswith("node:") or spec.startswith("./"), (
                f"{path.name} imports {spec!r}: the harness may only use node: built-ins "
                "and its own local modules"
            )


def test_fixture_selectors_exist_in_the_real_shells() -> None:
    """The hand-built fixture must not drift from the page it stands in for.

    tests/js/dashboard_fixture.mjs declares every id/class/data-attribute it
    reproduces; each one has to occur in both HTML shells, or the harness would
    be testing a DOM the dashboard does not ship.
    """
    fixture = (_JS_TESTS / "dashboard_fixture.mjs").read_text(encoding="utf-8")
    block = re.search(r"FIXTURE_SELECTORS = \[(.*?)\];", fixture, re.DOTALL)
    assert block is not None, "FIXTURE_SELECTORS list not found in dashboard_fixture.mjs"
    selectors = re.findall(r"'([^']+)'", block.group(1))
    assert len(selectors) >= 10, "FIXTURE_SELECTORS looks truncated"

    for html_path in (_DASHBOARD / "index.html", _DEMO / "index.html"):
        html = html_path.read_text(encoding="utf-8")
        for selector in selectors:
            assert selector in html, f"{selector!r} missing from {html_path.name}"


def test_fixture_module_keys_match_the_real_checkboxes() -> None:
    """MODULE_KEYS must be exactly the data-module values in the real shell."""
    fixture = (_JS_TESTS / "dashboard_fixture.mjs").read_text(encoding="utf-8")
    block = re.search(r"MODULE_KEYS = \[(.*?)\];", fixture, re.DOTALL)
    assert block is not None
    fixture_keys = set(re.findall(r'"([^"]+)"', block.group(1)))

    html = (_DASHBOARD / "index.html").read_text(encoding="utf-8")
    html_keys = set(re.findall(r'data-module="([^"]+)"', html))
    assert fixture_keys == html_keys, (
        f"fixture module keys {sorted(fixture_keys)} != dashboard {sorted(html_keys)}"
    )
