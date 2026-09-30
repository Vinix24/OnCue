"""Tests for scripts/check_env_credential_scope.py.

The gate exists because a credential reached worker processes that had no consumer
for it. These tests pin the two properties that make it useful: it recognises the
shape, and it never reproduces a value.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_env_credential_scope", ROOT / "scripts" / "check_env_credential_scope.py"
)
assert _SPEC is not None and _SPEC.loader is not None
scope = importlib.util.module_from_spec(_SPEC)
sys.modules["check_env_credential_scope"] = scope
_SPEC.loader.exec_module(scope)


@pytest.mark.parametrize(
    "name",
    [
        "VNX_SMTP_PASS",
        "SOME_SERVICE_PASSWORD",
        "MY_APP_SECRET",
        "SHUTDOWN_TOKEN",
        "OPENAI_API_KEY",
        "AZURE_CREDENTIAL",
    ],
)
def test_credential_shaped_names_are_recognised(name: str) -> None:
    assert scope.is_credential_shaped(name)


@pytest.mark.parametrize(
    "name",
    [
        "PYTHONPATH",
        "VNX_PROJECT_ID",
        "LLM_PROVIDER",
        "HOME",
        "KEYBOARD_LAYOUT",  # contains KEY but not as a trailing segment
        "SSH_AUTH_SOCK",  # structurally credential-shaped, holds a socket path
    ],
)
def test_ordinary_names_are_not_credential_shaped(name: str) -> None:
    assert not scope.is_credential_shaped(name)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "change-me-32-chars-min-placeholder", "<GENERATE_ON_FIRST_RUN>", "your-key-here"],
)
def test_placeholders_are_not_treated_as_secrets(value: str) -> None:
    assert scope.is_placeholder(value)


def test_real_looking_value_is_not_a_placeholder() -> None:
    assert not scope.is_placeholder("qwer tyui opas dfgh")


def _write_repo(tmp_path: Path, settings_env: dict[str, str], env_example: str) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": settings_env}), encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text(env_example, encoding="utf-8")
    return tmp_path


def test_credential_literal_in_settings_env_block_is_a_finding(tmp_path: Path) -> None:
    root = _write_repo(
        tmp_path,
        {"VNX_CONTEXT_ROTATION_ENABLED": "1", "VNX_SMTP_PASS": "qwer tyui opas dfgh"},
        "GEMINI_API_KEY=\n",
    )
    findings = scope.check_repo_sources(root)
    assert len(findings) == 1
    assert "VNX_SMTP_PASS" in findings[0]
    assert "settings.json" in findings[0]


def test_settings_env_block_without_credentials_is_clean(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, {"VNX_CONTEXT_ROTATION_ENABLED": "1"}, "GEMINI_API_KEY=\n")
    assert scope.check_repo_sources(root) == []


def test_env_example_placeholders_are_clean(tmp_path: Path) -> None:
    root = _write_repo(
        tmp_path,
        {},
        "GEMINI_API_KEY=\nHUGGINGFACE_TOKEN=hf_PLACEHOLDER\nSHUTDOWN_TOKEN=\n",
    )
    assert scope.check_repo_sources(root) == []


def test_env_example_with_a_real_value_is_a_finding(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, {}, "GEMINI_API_KEY=\nSHUTDOWN_TOKEN=s3cret-actual-token-value\n")
    findings = scope.check_repo_sources(root)
    assert len(findings) == 1
    assert "SHUTDOWN_TOKEN" in findings[0]


def test_undeclared_credential_in_ambient_env_is_reported(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, {}, "GEMINI_API_KEY=\n")
    findings = scope.check_ambient_env(
        {"GEMINI_API_KEY": "declared-so-fine", "VNX_SMTP_PASS": "qwer tyui opas dfgh"}, root
    )
    assert len(findings) == 1
    assert "VNX_SMTP_PASS" in findings[0]


def test_declared_credential_in_ambient_env_is_accepted(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, {}, "GEMINI_API_KEY=\nOPENROUTER_API_KEY=\n")
    findings = scope.check_ambient_env(
        {"GEMINI_API_KEY": "a-value", "OPENROUTER_API_KEY": "another"}, root
    )
    assert findings == []


def test_findings_never_contain_the_value(tmp_path: Path) -> None:
    """The whole point of the gate is that it names variables, never their contents."""
    secret = "qwer tyui opas dfgh"
    root = _write_repo(tmp_path, {"VNX_SMTP_PASS": secret}, "GEMINI_API_KEY=\n")

    repo_findings = scope.check_repo_sources(root)
    ambient_findings = scope.check_ambient_env({"VNX_SMTP_PASS": secret}, root)

    assert repo_findings and ambient_findings
    for finding in [*repo_findings, *ambient_findings]:
        assert secret not in finding


def test_declared_variables_reads_env_example(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, {}, "# comment\nGEMINI_API_KEY=\n\nSHUTDOWN_TOKEN=abc\nBARE_LINE\n")
    assert scope.declared_variables(root) == {"GEMINI_API_KEY", "SHUTDOWN_TOKEN"}


def test_repo_env_example_declares_the_credentials_this_project_consumes() -> None:
    """Guards the contract the ambient check depends on: .env.example is the declaration."""
    declared = scope.declared_variables()
    assert "GEMINI_API_KEY" in declared
    assert "OPENROUTER_API_KEY" in declared


def test_this_repo_has_no_credential_literal_in_committed_env_sources() -> None:
    """The gate must pass on the checked-in tree."""
    assert scope.check_repo_sources() == []
