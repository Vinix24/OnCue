"""Contract tests for the Claude Code hook wiring in `.claude/settings.json`.

Background (2026-09-05/06): the `UserPromptSubmit` hook branches on `$PWD`
ending in `/T0`, `/T1`, `/T2` or `/T3`. A dispatch worker's cwd is a worktree
root (`.vnx-data/worktrees/dispatch-D-<id>`), which matches none of those, so
every worker prompt fell through to the `else` branch. That branch emitted the
legacy top-level shape::

    {"decision": "allow"}

Claude Code 2.1.x rejects it with "Hook JSON output validation failed —
decision: Invalid input (top-level `decision` is the legacy approve|block
field...)". Per the documented contract the rejection is a NON-blocking error:
the prompt still reaches the model. It is noise, not a delivery failure — but
it is noise on every single worker prompt, and a hook whose output is rejected
can never carry `additionalContext` if one is ever added to that branch.

The documented no-op for `UserPromptSubmit` is exit 0 with no stdout. Plain-text
stdout on that event is injected into the model's context, so a chatty no-op is
not free either.

These tests execute the real hook command strings from the real settings file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SETTINGS = ROOT / ".claude" / "settings.json"

# `.claude/` is in export_public.sh's FORBIDDEN_PATHS, so this whole file is
# meaningless in a public checkout: there is no settings file to have a
# contract with. Skip rather than fail, the same way the other
# private-path-dependent tests in this suite do.
pytestmark = pytest.mark.skipif(
    not SETTINGS.is_file(),
    reason=".claude/settings.json is excluded from the public export",
)

# A worker's cwd during a dispatch: a per-dispatch git worktree, no /T{n} suffix.
WORKTREE_CWD_SUFFIX = ".vnx-data/worktrees/dispatch-D-deadbeef"


def _settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def _hook_commands(event: str) -> list[str]:
    commands: list[str] = []
    for entry in _settings().get("hooks", {}).get(event, []):
        for hook in entry.get("hooks", []):
            command = hook.get("command")
            if command:
                commands.append(command)
    return commands


def test_settings_file_is_valid_json() -> None:
    assert SETTINGS.is_file(), f"missing {SETTINGS}"
    _settings()


def test_no_hook_emits_the_legacy_top_level_decision_shape() -> None:
    """`{"decision": ...}` is the legacy approve|block field, rejected in 2.1.x."""
    offenders = [
        (event, command)
        for event in _settings().get("hooks", {})
        for command in _hook_commands(event)
        if '"decision"' in command or "'decision'" in command
    ]
    assert not offenders, (
        "hook command emits the legacy top-level `decision` field; use "
        "`hookSpecificOutput` for a decision, or exit 0 with no stdout for a "
        f"no-op: {offenders}"
    )


def test_userpromptsubmit_fallback_is_a_silent_noop_from_a_worktree(
    tmp_path: Path,
) -> None:
    """From a dispatch-worktree cwd the branch must exit 0 and print nothing.

    This is the branch every tmux-lane worker actually takes. It runs the real
    command string, so a regression that reintroduces any stdout here fails.
    """
    commands = _hook_commands("UserPromptSubmit")
    assert commands, "UserPromptSubmit hook is not configured"

    cwd = tmp_path / WORKTREE_CWD_SUFFIX
    cwd.mkdir(parents=True)

    branching = [c for c in commands if "*/T0" in c]
    assert branching, "expected a terminal-branching UserPromptSubmit hook command"

    for command in branching:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"fallback branch exited {result.returncode}; a UserPromptSubmit no-op "
            f"must exit 0.\nstderr: {result.stderr}"
        )
        assert result.stdout.strip() == "", (
            "fallback branch wrote to stdout; UserPromptSubmit stdout is either "
            "parsed as a JSON decision (and rejected when malformed) or injected "
            f"verbatim into the model's context. Got: {result.stdout!r}"
        )


def test_userpromptsubmit_terminal_branches_still_target_the_inject_scripts() -> None:
    """The fix must not have collapsed the T0/T1-T3 intelligence-injection paths."""
    branching = [c for c in _hook_commands("UserPromptSubmit") if "*/T0" in c]
    assert branching, "expected a terminal-branching UserPromptSubmit hook command"
    for command in branching:
        assert "userpromptsubmit_intelligence_inject.sh" in command
        assert "userpromptsubmit_worker_intelligence_inject.sh" in command
        for terminal in ("*/T1", "*/T2", "*/T3"):
            assert terminal in command, f"lost the {terminal} branch"
