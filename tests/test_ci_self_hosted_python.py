"""The self-hosted venv step must use a pinned, versioned interpreter."""

import re
from pathlib import Path

import pytest
import yaml

CI_YML = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Prepare cached venv (self-hosted)"


def _steps():
    return yaml.safe_load(CI_YML.read_text())["jobs"]["test"]["steps"]


@pytest.fixture(scope="module")
def step():
    found = [s for s in _steps() if s.get("name") == STEP_NAME]
    assert len(found) == 1, f"expected exactly one step named {STEP_NAME!r}"
    return found[0]


def test_interpreter_env_var_names_versioned_python(step):
    value = step["env"]["SELF_HOSTED_PYTHON"]
    assert re.fullmatch(r"python3\.\d+", value), value


def test_step_has_no_bare_python3(step):
    run = step["run"]
    assert not re.search(r"(?<![\w.$\"/])python3(?![\w.])", run), "bare python3 in run script"
    assert "python3 -m venv" not in run
    assert '"$SELF_HOSTED_PYTHON" -m venv' in run
    assert '"$SELF_HOSTED_PYTHON" --version' in run


def test_step_fails_loud_when_interpreter_missing(step):
    run = step["run"]
    assert 'command -v "$SELF_HOSTED_PYTHON"' in run
    assert "ontbreekt op de runner" in run


def test_github_hosted_path_untouched():
    setup = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1
    assert setup[0]["with"]["python-version"] == "3.11"
