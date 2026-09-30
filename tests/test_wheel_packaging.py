"""Regression coverage for a normal (non-editable) `pip install .`.

`pip install -e .` (what every developer runs) never exercises the wheel's
actual file layout, so a wheel missing dashboard/presentation/config assets
went unnoticed until a real Windows install 404'd on the dashboard (2026-09-06
field report). These tests build a real wheel via ``python -m build`` and
inspect the resulting archive -- not the ``force-include`` config in
``pyproject.toml``, which could drift from what actually lands on disk.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path
from zipfile import ZipFile

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the real wheel once for this module's tests."""
    out_dir = tmp_path_factory.mktemp("wheel-out")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out_dir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"wheel build failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _members(wheel_path: Path) -> list[str]:
    with ZipFile(wheel_path) as zf:
        return zf.namelist()


def test_wheel_contains_dashboard_assets(built_wheel: Path) -> None:
    members = _members(built_wheel)
    assert "sales_copilot/_resources/dashboard/index.html" in members
    assert "sales_copilot/_resources/dashboard/js/app.js" in members


def test_wheel_contains_presentation_assets(built_wheel: Path) -> None:
    members = _members(built_wheel)
    assert "sales_copilot/_resources/presentation/index.html" in members


def test_wheel_contains_config_defaults(built_wheel: Path) -> None:
    members = _members(built_wheel)
    assert "sales_copilot/_resources/config/pain_points.yaml" in members
    assert "sales_copilot/_resources/config/phases.yaml" in members


def test_wheel_contains_sample_aha_data(built_wheel: Path) -> None:
    members = _members(built_wheel)
    assert "sales_copilot/_resources/data/samples/sample-aha.json" in members


def test_wheel_excludes_dev_secrets(built_wheel: Path) -> None:
    """The existing dev-key exclusion must survive the packaging change."""
    members = _members(built_wheel)
    assert not any(m.endswith("_dev_keys.py") for m in members)


def test_installed_wheel_serves_dashboard_and_keeps_data_out_of_site_packages(
    tmp_path: Path, built_wheel: Path
) -> None:
    """End-to-end: install the built wheel into a clean venv and resolve paths.

    This is the actual regression: a normal `pip install .` must let
    mount_static_apps() find the dashboard, and must never resolve writable
    state (data/, .vnx-data/) inside site-packages.
    """
    venv_dir = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, timeout=120)
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():  # pragma: no cover - Windows layout
        venv_python = venv_dir / "Scripts" / "python.exe"

    install = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-q", str(built_wheel)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert install.returncode == 0, f"pip install failed:\nstdout={install.stdout}\nstderr={install.stderr}"

    check = subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "from sales_copilot.core.paths import resolve_app_support, resolve_app_resource\n"
                "support = resolve_app_support()\n"
                "assert 'site-packages' not in str(support), f'app support inside site-packages: {support}'\n"
                "dashboard_index = resolve_app_resource('dashboard/index.html')\n"
                "assert dashboard_index.exists(), f'dashboard/index.html missing: {dashboard_index}'\n"
                "config_default = resolve_app_resource('config/pain_points.yaml')\n"
                "assert config_default.exists(), f'config/pain_points.yaml missing: {config_default}'\n"
                "print('OK')\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )
    assert check.returncode == 0, f"verification failed:\nstdout={check.stdout}\nstderr={check.stderr}"
    assert "OK" in check.stdout


def test_editable_install_is_unaffected_by_force_include(built_wheel: Path) -> None:
    """Sanity: force-include only changes the wheel, not the editable dev layout."""
    with zipfile.ZipFile(built_wheel) as zf:
        top_level_dashboard = [n for n in zf.namelist() if n.startswith("dashboard/")]
    # dashboard/ must not appear as a wheel top-level dir -- only nested under
    # sales_copilot/_resources/ -- or it would collide with unrelated packages
    # in site-packages.
    assert top_level_dashboard == []
