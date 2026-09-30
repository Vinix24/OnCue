#!/usr/bin/env python3
"""CI acceptance check: credential-shaped variables must not reach worker processes.

Background
----------
On 2026-09-05 a dispatch worker running in this repo found ``VNX_SMTP_PASS`` in its
own process environment. No component of this project sends mail, so the worker had
no use for it. Running ``env`` -- an ordinary debugging step -- copied the value into
the worker's tool transcript, and those transcripts are written to disk. The variable
reached the worker by inheritance from a long-lived parent process, not from any file
in this repository.

That incident is one instance of a general class: a credential defined once, in a
place that populates a process environment, silently reaches every descendant process
regardless of whether it has a consumer. This check makes the class visible.

Two independent checks
----------------------
1. **Repo-controlled env sources (fatal).** The ``env`` blocks of
   ``.claude/settings.json`` / ``.claude/settings.local.json`` and the keys of
   ``.env.example`` are committed to this repository and are injected into every
   session started here. A credential-shaped name carrying a real literal value in
   one of those files is a defect this repository can and must fix, so it fails the
   gate. (``.gitleaks.toml`` carries a matching rule so the pre-commit hook and the
   public-export gate reject the same shape at commit time.)

2. **Ambient process environment (advisory, fatal under ``--strict``).**
   ``.env.example`` is this project's declaration of the variables it consumes. Any
   credential-shaped variable present in the environment but absent from that
   declaration reaches our processes without a consumer here. That is usually not
   repo-fixable -- the value comes from a shell profile, a launchd session, or a
   long-lived tmux server -- so by default it is reported rather than enforced.
   ``--strict`` makes it fatal, which is how an operator verifies that a remediation
   actually landed.

This script prints variable NAMES only. It never prints, logs, or otherwise
reproduces a value.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A name is credential-shaped when it ends in one of these segments. Anchoring on the
# suffix keeps the match on the *role* of the variable and avoids sweeping in names
# such as PYTHONPATH or SSH_AUTH_SOCK that merely contain a matching substring.
_CREDENTIAL_SUFFIXES = (
    "PASS",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "KEY",
    "APIKEY",
    "CREDENTIAL",
    "CREDENTIALS",
)

_CREDENTIAL_NAME_RE = re.compile(
    r"^[A-Z][A-Z0-9_]*_(?:" + "|".join(_CREDENTIAL_SUFFIXES) + r")$"
)

# Values that are self-evidently not secrets. Kept in sync with the stopwords in
# .gitleaks.toml so both gates agree on what a placeholder looks like.
_PLACEHOLDER_MARKERS = (
    "placeholder",
    "change-me",
    "changeme",
    "your-",
    "<generate",
    "example",
    "dummy",
    "test",
)

# Variables that are structurally credential-shaped but hold a path or handle rather
# than a secret. Listed explicitly so the reason is on the record.
_NOT_A_SECRET = frozenset({"SSH_AUTH_SOCK"})

_SETTINGS_FILES = (
    Path(".claude/settings.json"),
    Path(".claude/settings.local.json"),
)
_ENV_EXAMPLE = Path(".env.example")


def is_credential_shaped(name: str) -> bool:
    """Return True when *name* names a variable that is expected to hold a secret."""
    if name in _NOT_A_SECRET:
        return False
    return bool(_CREDENTIAL_NAME_RE.match(name))


def is_placeholder(value: str) -> bool:
    """Return True when *value* is empty or an obvious non-secret placeholder."""
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def declared_variables(root: Path = ROOT) -> set[str]:
    """Return the variable names ``.env.example`` declares this project consumes."""
    path = root / _ENV_EXAMPLE
    if not path.is_file():
        return set()
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name:
            names.add(name)
    return names


def settings_env_blocks(root: Path = ROOT) -> list[tuple[Path, dict[str, str]]]:
    """Return the ``env`` block of each Claude settings file that defines one."""
    blocks: list[tuple[Path, dict[str, str]]] = []
    for rel in _SETTINGS_FILES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[FAIL] {rel}: not valid JSON ({exc.msg} at line {exc.lineno})")
            blocks.append((rel, {}))
            continue
        env = data.get("env")
        if isinstance(env, dict):
            blocks.append((rel, {str(k): str(v) for k, v in env.items()}))
    return blocks


def check_repo_sources(root: Path = ROOT) -> list[str]:
    """Return a finding per credential-shaped literal in a committed env source."""
    findings: list[str] = []

    for rel, env in settings_env_blocks(root):
        for name, value in sorted(env.items()):
            if is_credential_shaped(name) and not is_placeholder(value):
                findings.append(
                    f"{rel}: env block sets credential-shaped '{name}' to a literal value"
                )

    example = root / _ENV_EXAMPLE
    if example.is_file():
        for lineno, raw in enumerate(example.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if is_credential_shaped(name) and not is_placeholder(value):
                findings.append(
                    f"{_ENV_EXAMPLE}:{lineno}: credential-shaped '{name}' carries a real value"
                )

    return findings


def check_ambient_env(
    environ: dict[str, str] | None = None, root: Path = ROOT
) -> list[str]:
    """Return a finding per credential-shaped variable this project does not declare."""
    env = os.environ if environ is None else environ
    declared = declared_variables(root)
    findings: list[str] = []
    for name in sorted(env):
        if not is_credential_shaped(name) or name in declared:
            continue
        state = "empty" if not env[name].strip() else "set"
        findings.append(f"{name} ({state}) reaches this process but is not declared in {_ENV_EXAMPLE}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also fail on undeclared credential-shaped variables in the ambient environment",
    )
    args = parser.parse_args(argv)

    repo_findings = check_repo_sources()
    ambient_findings = check_ambient_env()

    if repo_findings:
        print("[FAIL] credential-shaped literal in a committed environment source:")
        for finding in repo_findings:
            print(f"  - {finding}")
        print(
            "\n  A value here is injected into every session started in this repository.\n"
            "  Move it to a secret store the consuming process reads directly."
        )
    else:
        print("[OK] no credential-shaped literal in committed environment sources")

    if ambient_findings:
        label = "FAIL" if args.strict else "WARN"
        print(f"\n[{label}] undeclared credential-shaped variables in this process environment:")
        for finding in ambient_findings:
            print(f"  - {finding}")
        print(
            "\n  These are inherited from a parent process (shell profile, launchd session,\n"
            "  or a long-lived tmux server), not from this repository. A worker that runs\n"
            "  `env` copies them into its transcript. See SECURITY.md\n"
            "  'Credential scope for worker processes' for the operator remediation."
        )
    else:
        print("\n[OK] no undeclared credential-shaped variables in this process environment")

    if repo_findings or (args.strict and ambient_findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
