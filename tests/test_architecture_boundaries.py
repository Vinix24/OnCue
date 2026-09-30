"""Tests for scripts/check_architecture_boundaries.py.

Each test creates a minimal repo tree and proves that the deterministic gate
fails on a single violation while the real repo passes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_architecture_boundaries.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def _valid_ci(root: Path) -> None:
    ci = root / ".github" / "workflows" / "ci.yml"
    ci.parent.mkdir(parents=True, exist_ok=True)
    ci.write_text("run: python scripts/check_architecture_boundaries.py\n")


def test_clean_repository_passes() -> None:
    """The current checkout must satisfy every boundary check."""
    result = _run(ROOT)
    assert result.returncode == 0, f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"


@pytest.mark.parametrize(
    ("violation_file", "expected_marker"),
    [
        ("src/bad_provider.py", "[provider-sdk-confinement]"),
        ("src/bad_network.py", "[network-allowlist]"),
        ("src/bad_vnx.py", "[vnx-data-isolation]"),
    ],
)
def test_src_violations_fail(violation_file: str, expected_marker: str, tmp_path: Path) -> None:
    """A new src file that breaks a rule is rejected."""
    root = tmp_path / "repo"
    src = root / "src"
    src.mkdir(parents=True)
    _valid_ci(root)

    # Enough source structure to satisfy other checks.
    (src / "sales_copilot" / "core" / "llm_client.py").parent.mkdir(parents=True)
    (src / "sales_copilot" / "core" / "llm_client.py").write_text("# ok\n")

    (src / Path(violation_file).name).write_text(
        {
            "src/bad_provider.py": "import openai\n",
            "src/bad_network.py": "import requests\n",
            "src/bad_vnx.py": 'DEFAULT = ".vnx-data/leaks.db"\n',
        }[violation_file]
    )

    result = _run(root)
    assert result.returncode != 0
    assert expected_marker in result.stderr


def test_anthropic_anywhere_fails(tmp_path: Path) -> None:
    """Anthropic SDK is forbidden even outside src/scripts."""
    root = tmp_path / "repo"
    tools = root / "tools"
    tools.mkdir(parents=True)
    _valid_ci(root)
    (tools / "bad.py").write_text("import anthropic\n")

    result = _run(root)
    assert result.returncode != 0
    assert "[anthropic-forbidden]" in result.stderr


def test_dashboard_framework_fails(tmp_path: Path) -> None:
    """A framework file in dashboard/ is rejected."""
    root = tmp_path / "repo"
    dashboard = root / "dashboard"
    dashboard.mkdir(parents=True)
    _valid_ci(root)
    (dashboard / "package.json").write_text('{"name": "bad"}\n')

    result = _run(root)
    assert result.returncode != 0
    assert "[dashboard-vanilla]" in result.stderr


def test_hub_non_localhost_fails(tmp_path: Path) -> None:
    """A non-localhost bind address in the hub is rejected."""
    root = tmp_path / "repo"
    ws = root / "src" / "sales_copilot" / "websocket"
    ws.mkdir(parents=True)
    _valid_ci(root)
    (ws / "hub.py").write_text(
        'def run_hub(host: str, port: int) -> None:\n'
        '    if host not in {"localhost", "127.0.0.1"}:\n'
        '        raise ValueError("The local WebSocket hub must bind to localhost")\n'
        '    uvicorn.run(app, host="0.0.0.0", port=port)\n'
        '    response.headers["Content-Security-Policy"] = '
        '"connect-src \'self\' http://localhost:8760 http://127.0.0.1:8760 '
        'ws://localhost:8760 ws://127.0.0.1:8760;"\n'
    )

    result = _run(root)
    assert result.returncode != 0
    assert "[hub-localhost]" in result.stderr


def test_ci_step_missing_fails(tmp_path: Path) -> None:
    """A workflow that does not invoke the architecture check is rejected."""
    root = tmp_path / "repo"
    ci = root / ".github" / "workflows" / "ci.yml"
    ci.parent.mkdir(parents=True)
    ci.write_text("run: pytest\n")

    result = _run(root)
    assert result.returncode != 0
    assert "[ci-step]" in result.stderr


def test_deep_insight_lane_outbound_class_documented() -> None:
    """The deep-insight lane (second outbound class) must be documented in the
    invariant doc, the privacy doc, and the boundary-checker docstring.

    PR-D0 (2026-08-04) introduced this as a deliberate, documented revision of
    invariant 1 — not a footnote exception. This test keeps it from being
    silently removed in a future edit.
    """
    boundaries = ROOT / "docs" / "ARCHITECTURE_BOUNDARIES.md"
    privacy = ROOT / "docs" / "PRIVACY.md"
    check = ROOT / "scripts" / "check_architecture_boundaries.py"

    boundaries_text = boundaries.read_text(encoding="utf-8")
    privacy_text = privacy.read_text(encoding="utf-8")
    check_text = check.read_text(encoding="utf-8")

    # The revision is documented as a named section, not buried in a footnote.
    assert "Deep-insight lane outbound class" in boundaries_text
    assert "2026-08-04" in boundaries_text

    # The three hard conditions are spelled out in the invariant doc.
    assert "Opt-in per conversation" in boundaries_text
    assert "Default off" in boundaries_text
    assert "outbound_policy.py" in boundaries_text

    # The privacy doc nuances the "full transcripts" line.
    assert "deep-insight lane" in privacy_text.lower()
    assert "default off" in privacy_text.lower()

    # The checker docstring references the lane so reviewers know it exists.
    assert "deep-insight lane" in check_text.lower()
    assert "2026-08-04" in check_text


def test_trigger_delivery_outbound_class_documented() -> None:
    """The trigger-delivery outbound class (third outbound class) must be
    documented in the invariant doc, the privacy doc, the boundary-checker
    docstring, and the network allow-list -- same discipline as the
    deep-insight lane above, so it cannot be silently removed in a future edit.
    """
    boundaries = ROOT / "docs" / "ARCHITECTURE_BOUNDARIES.md"
    privacy = ROOT / "docs" / "PRIVACY.md"
    module4 = ROOT / "docs" / "MODULE4.md"
    check = ROOT / "scripts" / "check_architecture_boundaries.py"

    boundaries_text = boundaries.read_text(encoding="utf-8")
    privacy_text = privacy.read_text(encoding="utf-8")
    module4_text = module4.read_text(encoding="utf-8")
    check_text = check.read_text(encoding="utf-8")

    # The revision is documented as a named section, not buried in a footnote.
    assert "Trigger delivery outbound class" in boundaries_text
    assert "2026-09-06" in boundaries_text

    # It explicitly does NOT go through the LLM redaction seam, and explains why.
    assert "does NOT go through" in boundaries_text
    assert "REPORT_REDACT_PII" in boundaries_text

    # The privacy doc covers the same destination, opt-in/default-off, and the
    # unredacted-by-default posture (a real difference from the deep-insight lane).
    assert "report delivery" in privacy_text.lower()
    assert "REPORT_REDACT_PII" in privacy_text

    # The checker docstring and network allow-list both reference the new module.
    assert "trigger delivery" in check_text.lower()
    assert "src/sales_copilot/modules/reports/delivery.py" in check_text

    # The full contract lives in MODULE4.md ("Report Delivery").
    assert "## Report Delivery" in module4_text
    assert "REPORT_DELIVERY_DIR" in module4_text
    assert "REPORT_DELIVERY_ENDPOINT" in module4_text
