"""Safe, minimal upsert writer for the user's ``.env`` file.

The first-run wizard lets a non-technical user paste their LLM provider key and
Pro license key into the browser instead of hand-editing ``.env``. This module
does the actual write: it sets or updates a single ``KEY=value`` line while
preserving every other line, comment, and blank line in the file (upsert
semantics, never a clobber). It also exposes a non-mutating reader so callers
can decide whether the app is already configured.

Path resolution mirrors :func:`sales_copilot.core.config.load_env`: in a frozen
``.app`` the writable ``.env`` lives under the app-support tree and the template
ships as a bundled resource; in a dev checkout both resolve relative to the cwd.

Never logs a secret value — callers pass API keys and license keys through here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

from sales_copilot.core.paths import is_frozen_app, resolve_app_path, resolve_app_resource

# A valid POSIX-ish env identifier. Guards against writing a malformed line.
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Values made only of these characters are safe to write unquoted; dotenv reads
# them back verbatim. Anything else (whitespace, ``#``, quotes, ``$`` …) is
# double-quoted so it round-trips through ``dotenv_values``.
_SIMPLE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:+=@,-]*$")


def _default_env_path() -> Path:
    return resolve_app_path(".env") if is_frozen_app() else Path(".env")


def _default_example_path() -> Path:
    return resolve_app_resource(".env.example") if is_frozen_app() else Path(".env.example")


def _quote(value: str) -> str:
    """Return ``value`` ready to place on the right-hand side of ``KEY=``."""

    if value == "" or _SIMPLE_VALUE_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _assignment_key(line: str) -> str | None:
    """Return the variable name a line assigns, or ``None`` for non-assignments.

    Comments and blank lines return ``None`` so they are never treated as the
    target key (e.g. a commented ``# SALES_COPILOT_LICENSE_CHECK_URL=…`` never
    shadows the real ``SALES_COPILOT_LICENSE=`` line).
    """

    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    lhs = stripped.split("=", 1)[0].strip()
    if lhs.startswith("export "):
        lhs = lhs[len("export "):].strip()
    return lhs or None


def read_env_value(name: str, *, env_path: str | Path | None = None) -> str | None:
    """Read ``name`` from the process env, falling back to the ``.env`` file.

    Mirrors ``load_dotenv(override=False)`` precedence (process env wins) but
    without mutating ``os.environ``. Returns ``None`` when the variable is set
    nowhere. An empty string in the environment is returned as ``""`` (present
    but empty), matching how the wizard treats an unfilled key.
    """

    current = os.environ.get(name)
    if current is not None:
        return current
    path = Path(env_path) if env_path else _default_env_path()
    if not path.exists():
        return None
    return dotenv_values(path).get(name)


def set_env_var(
    key: str,
    value: str,
    *,
    env_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> Path:
    """Set or update a single ``KEY=value`` in the user's ``.env`` (upsert).

    - Creates ``.env`` from ``.env.example`` when it does not exist yet (or an
      empty file when no template is present), and locks it to ``0600``.
    - Updates the first uncommented assignment of ``key`` in place, preserving
      every other line, comment, and blank line.
    - Appends ``key=value`` at the end when the key is not yet present.

    Returns the path written. Never logs ``value``.
    """

    if not _KEY_RE.match(key):
        raise ValueError(f"Invalid env key: {key!r}")

    target = Path(env_path) if env_path else _default_env_path()
    example = Path(example_path) if example_path else _default_example_path()

    created = False
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        seed = example.read_text(encoding="utf-8") if example.exists() else ""
        target.write_text(seed, encoding="utf-8")
        created = True

    lines = target.read_text(encoding="utf-8").splitlines()
    new_line = f"{key}={_quote(value)}"

    replaced = False
    for index, line in enumerate(lines):
        if _assignment_key(line) == key:
            lines[index] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    target.write_text(text, encoding="utf-8")

    if created:
        try:
            target.chmod(0o600)
        except OSError:
            pass

    return target
