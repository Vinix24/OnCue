#!/usr/bin/env python3
"""Deterministic architecture-boundary gate for the sales-copilot repo.

This script encodes the hard architectural invariants from CLAUDE.md and
 docs/ARCHITECTURE_BOUNDARIES.md as an enforceable CI check. A PR that breaks
a boundary exits non-zero with a concrete file/line message.

Rules:
1. Provider-agnostic LLM: all provider SDK imports live inside
   `src/sales_copilot/core/llm_client.py`. `import anthropic` is forbidden
   repository-wide.
2. Local-first data plane: network-client imports in `src/` are allow-listed;
   new network-using modules must be reviewed.
3. Dashboard is vanilla HTML/CSS/JS: no framework imports or build tooling.
4. WebSocket hub is localhost-only.
5. App code does not introduce new writes to `.vnx-data/` (legacy modules are
   grandfathered).
6. CI runs this script.

Note on invariant 1 (local-first data plane): the default outbound class is
short, PII-redacted LLM fragments. A deliberate second outbound class — the
deep-insight lane (PR-D0, revised 2026-08-04) — may send the PII-redacted full
session transcript to an operator-configured frontier/enterprise destination,
opt-in per conversation and default off. This check does not redact or measure
payload size; it enforces the *plumbing* invariants (SDK confinement, network
import allow-list) that the deep lane must also satisfy. PII redaction on the
exit is enforced at runtime by `core/outbound_policy.py`, not here. See
`docs/ARCHITECTURE_BOUNDARIES.md` ("Deep-insight lane outbound class").

A THIRD outbound class (added 2026-09-06): customer-configured trigger delivery
(`src/sales_copilot/modules/reports/delivery.py`) hands the finished post-call
report to an operator-named directory or HTTP endpoint, opt-in and default off.
Unlike the deep-insight lane it does NOT go through `core/outbound_policy.py` --
the destination is never an LLM provider, and redaction for this payload is
controlled by the existing `REPORT_REDACT_PII` flag instead. See
`docs/ARCHITECTURE_BOUNDARIES.md` ("Trigger delivery outbound class").

To mark a conscious exception for a single line, add:
    # architecture-boundary-ignore
To add a file-level exception, update the relevant allow-list in this script.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from pathlib import Path

# Directories excluded from every scan.
_IGNORED_DIR_PARTS = frozenset(
    {
        ".git",
        ".vnx-data",
        ".venv",
        "venv",
        "node_modules",
        "vendor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
    }
)

# Provider SDKs that must be imported only through the central LLM client seam.
_PROVIDER_SDKS = [
    "openai",
    "google.genai",
    "google",  # catches `from google import genai`
    "anthropic",
    "groq",
    "vertexai",
    "cohere",
    "mistralai",
    "openrouter",
]

# Network-client libraries tracked in the local-first data-plane rule.
_NETWORK_LIBS = [
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "http.client",
    "urllib.request",
    "websockets",
]

# CSP connect-src directive required by the hub-localhost rule (rule 4).
_EXPECTED_CSP_CONNECT_SRC = (
    "connect-src 'self' http://localhost:8760 http://127.0.0.1:8760 "
    "ws://localhost:8760 ws://127.0.0.1:8760"
)

# Legacy modules that currently hardcode `.vnx-data/` paths. New occurrences or
# new modules are not allowed.
_VNX_DATA_LEGACY_FILES = frozenset(
    {
        "src/sales_copilot/core/session_store.py",
        "src/sales_copilot/core/audit_ledger.py",
        "src/sales_copilot/auth/startup_check.py",
        "src/sales_copilot/auth/email_capture.py",
        "src/sales_copilot/websocket/hub_auth.py",
    }
)

# Functions that legitimately create an app-support-scoped `.vnx-data/`
# subdirectory (via resolve_app_support()), *not* the repo-root governance state.
_VNX_DATA_APP_SUPPORT_CREATORS: dict[str, str] = {
    "src/sales_copilot/core/paths.py": "ensure_app_support_tree",
}

# Files allowed to import network-client libraries. The purpose is not to bless
# every current call site blindly, but to make new network-using modules
# impossible to add without an explicit review.
_NETWORK_ALLOWLIST: dict[str, set[str]] = {
    # httpx
    "src/sales_copilot/core/llm_client.py": {"httpx"},
    "src/sales_copilot/sample_aha/player.py": {"httpx", "websockets"},
    "src/sales_copilot/modules/transcriber/backends/whisper_cpp_backend.py": {"httpx"},
    "src/sales_copilot/modules/transcriber/backends/groq_backend.py": {"httpx"},
    "src/sales_copilot/modules/detector/eval_shared.py": {"httpx"},
    # urllib.request
    "src/sales_copilot/core/measurement_signals.py": {"urllib.request"},
    "src/sales_copilot/core/compliance_audit.py": {"urllib.request"},
    "src/sales_copilot/auth/revocation_cache.py": {"urllib.request"},
    "src/sales_copilot/modules/reports/delivery.py": {"urllib.request"},
    # websockets: local hub only
    "src/sales_copilot/__main__.py": {"websockets"},
    "src/sales_copilot/modules/reports/__main__.py": {"websockets"},
    "src/sales_copilot/modules/reports/session.py": {"websockets"},
    "src/sales_copilot/modules/copilot/injector.py": {"websockets"},
    "src/sales_copilot/modules/talk_time/__main__.py": {"websockets"},
    "src/sales_copilot/modules/talk_time/publisher.py": {"websockets"},
    "src/sales_copilot/modules/detector/__main__.py": {"websockets"},
    "src/sales_copilot/modules/detector/suggestions.py": {"websockets"},
    "src/sales_copilot/modules/detector/summary.py": {"websockets"},
    "src/sales_copilot/modules/insight/engine.py": {"websockets"},
    "src/sales_copilot/modules/detector/objection_detector.py": {"websockets"},
    "src/sales_copilot/modules/transcriber/whisper_direct.py": {"websockets"},
    "src/sales_copilot/modules/detector/phase_detector.py": {"websockets"},
    "src/sales_copilot/modules/transcriber/inference_worker.py": {"websockets"},
    "src/sales_copilot/modules/transcriber/__main__.py": {"websockets"},
    "src/sales_copilot/modules/coaching/script_tracker.py": {"websockets"},
    # MCP bridge (read-only, hub-client over localhost websockets)
    "src/sales_copilot/mcp_bridge/server.py": {"websockets"},
}

# Dashboard framework/build-tool markers.
_DASHBOARD_FORBIDDEN_FILES = [
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "node_modules",
]
_DASHBOARD_BUILD_CONFIG_PATTERNS = [
    re.compile(r"^vite\.config\."),
    re.compile(r"^webpack\.config\."),
    re.compile(r"^rollup\.config\."),
    re.compile(r"^esbuild\.config\."),
    re.compile(r"^babel\.config\."),
    re.compile(r"^tsconfig\.json$"),
    re.compile(r"^postcss\.config\."),
    re.compile(r"^tailwind\.config\."),
]
_DASHBOARD_FRAMEWORK_RE = re.compile(
    r"\b(?:react|vue|angular|svelte|preact|lit|solid-js|next\.js|nuxt|create-react-app|@angular)\b",
    re.IGNORECASE,
)


def _is_ignored_path(path: Path, root: Path) -> bool:
    return any(part in _IGNORED_DIR_PARTS for part in path.relative_to(root).parts)


def _iter_files(root: Path, *subpaths: str, glob: str = "*") -> Iterable[Path]:
    for sub in subpaths:
        base = root / sub
        if not base.exists():
            continue
        if base.is_file():
            yield base
            continue
        for path in base.rglob(glob):
            if path.is_file() and not _is_ignored_path(path, root):
                yield path


def _iter_python_files(root: Path, *subpaths: str) -> Iterable[Path]:
    yield from _iter_files(root, *subpaths, glob="*.py")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _has_ignore_pragma(line: str) -> bool:
    return "# architecture-boundary-ignore" in line


def _make_import_re(names: list[str]) -> re.Pattern[str]:
    # Build a regex that matches `import x`, `import a, b, x` and `from x import ...`.
    escaped = [re.escape(n) for n in names]
    alternatives = "|".join(escaped)
    return re.compile(
        rf"^\s*(?:import\s+(?:[\w.]+\s*,\s*)*(?:{alternatives})\b|from\s+(?:{alternatives})\s+import)",
        re.MULTILINE | re.IGNORECASE,
    )


_PROVIDER_RE = _make_import_re(_PROVIDER_SDKS)
_NETWORK_RE = _make_import_re(_NETWORK_LIBS)


def _matched_lines(text: str, pattern: re.Pattern[str]) -> list[tuple[int, str]]:
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line) and not _has_ignore_pragma(line)
    ]


def check_provider_sdk_confinement(root: Path) -> list[str]:
    """All provider SDK imports must live in core/llm_client.py."""
    allowed = {Path("src/sales_copilot/core/llm_client.py")}
    violations: list[str] = []
    for path in _iter_python_files(root, "src", "scripts"):
        rel = path.relative_to(root)
        if rel in allowed:
            continue
        for lineno, line in _matched_lines(_read_text(path), _PROVIDER_RE):
            violations.append(
                f"[provider-sdk-confinement] {rel}:{lineno}: provider SDK import outside llm_client.py: {line.strip()}"
            )
    return violations


def check_anthropic_forbidden(root: Path) -> list[str]:
    """`import anthropic` is forbidden repository-wide."""
    violations: list[str] = []
    anthropic_re = re.compile(
        r"^\s*(?:import\s+(?:[\w.]+\s*,\s*)*anthropic\b|from\s+anthropic\s+import)",
        re.MULTILINE | re.IGNORECASE,
    )
    for path in _iter_python_files(root, "."):
        rel = path.relative_to(root)
        for lineno, line in _matched_lines(_read_text(path), anthropic_re):
            violations.append(
                f"[anthropic-forbidden] {rel}:{lineno}: Anthropic SDK is not allowed: {line.strip()}"
            )
    return violations


def check_network_allowlist(root: Path) -> list[str]:
    """Network-client imports in src/ must be in the allow-list."""
    violations: list[str] = []
    for path in _iter_python_files(root, "src"):
        rel = str(path.relative_to(root))
        allowed = _NETWORK_ALLOWLIST.get(rel, set())
        for lineno, line in _matched_lines(_read_text(path), _NETWORK_RE):
            lib = _extract_imported_lib(line)
            if lib not in allowed:
                violations.append(
                    f"[network-allowlist] {rel}:{lineno}: unallowed network-client import: {line.strip()}"
                )
    return violations


def _extract_imported_lib(line: str) -> str:
    """Best-effort extraction of the library name from an import line."""
    line = line.strip()
    if line.startswith("from "):
        module = line.split()[1]
        return module
    # import a, b, c
    after = line[len("import") :].strip()
    # Take the first aliased/non-aliased name.
    first = after.split(",")[0].strip()
    if " as " in first:
        first = first.split(" as ")[0].strip()
    return first


def check_dashboard_vanilla(root: Path) -> list[str]:
    """dashboard/ must stay vanilla HTML/CSS/JS."""
    violations: list[str] = []
    dashboard = root / "dashboard"
    if not dashboard.exists():
        return violations
    for path in _iter_files(root, "dashboard"):
        rel = path.relative_to(root)
        name = path.name
        if name in _DASHBOARD_FORBIDDEN_FILES:
            violations.append(f"[dashboard-vanilla] {rel}: forbidden file found")
            continue
        for pattern in _DASHBOARD_BUILD_CONFIG_PATTERNS:
            if pattern.match(name):
                violations.append(
                    f"[dashboard-vanilla] {rel}: build-tool/config file not allowed"
                )
                break
        if path.suffix in {".jsx", ".tsx"}:
            violations.append(f"[dashboard-vanilla] {rel}: JSX/TSX not allowed")
            continue
        if not _is_text_file(path):
            continue
        text = _read_text(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _DASHBOARD_FRAMEWORK_RE.search(line):
                violations.append(
                    f"[dashboard-vanilla] {rel}:{lineno}: framework reference in dashboard: {line.strip()[:80]}"
                )
                break
    return violations


def _is_text_file(path: Path) -> bool:
    return path.suffix in {".html", ".htm", ".js", ".css", ".md", ".json", ".yaml", ".yml"}


def check_hub_localhost_only(root: Path) -> list[str]:
    """The WebSocket hub must bind/CORS to localhost only."""
    violations: list[str] = []
    hub_dir = root / "src" / "sales_copilot" / "websocket"
    if not hub_dir.exists():
        return violations
    for path in hub_dir.glob("hub*.py"):
        text = _read_text(path)
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _has_ignore_pragma(line):
                continue
            if "0.0.0.0" in line or '"::"' in line or "'::'" in line:
                violations.append(
                    f"[hub-localhost] {rel}:{lineno}: non-localhost bind address: {line.strip()}"
                )
            if re.search(r'allow_origins\s*=\s*\[\s*"\*"', line):
                violations.append(
                    f"[hub-localhost] {rel}:{lineno}: wildcard CORS origin not allowed: {line.strip()}"
                )

    hub = hub_dir / "hub.py"
    if hub.exists():
        text = _read_text(hub)
        if 'if host not in {"localhost", "127.0.0.1"}:' not in text:
            violations.append(
                f"[hub-localhost] {hub.relative_to(root)}: run_hub() is missing localhost guard"
            )
        if 'raise ValueError("The local WebSocket hub must bind to localhost")' not in text:
            violations.append(
                f"[hub-localhost] {hub.relative_to(root)}: run_hub() guard has wrong error message"
            )
        if _EXPECTED_CSP_CONNECT_SRC not in text:
            violations.append(
                f"[hub-localhost] {hub.relative_to(root)}: CSP connect-src must allow both hostnames for http and ws"
            )

    core = hub_dir / "hub_core.py"
    if core.exists():
        text = _read_text(core)
        if '"http://localhost:8760"' not in text or '"http://127.0.0.1:8760"' not in text:
            violations.append(
                f"[hub-localhost] {core.relative_to(root)}: _ALLOWED_WS_ORIGINS must contain localhost origins"
            )

    return violations


def _function_line_range(text: str, func_name: str) -> tuple[int, int] | None:
    """Return the inclusive line-number span of ``func_name`` in *text*.

    Used to scope legitimate `.vnx-data` references to a specific app-support
    helper; returns ``None`` when the function cannot be found or the text does
    not parse.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            start = node.lineno
            end = max(getattr(n, "lineno", start) for n in ast.walk(node))
            return start, end
    return None


def check_vnx_data_isolation(root: Path) -> list[str]:
    """App code must not introduce new `.vnx-data/` writes.

    App-support-scoped creation via ``resolve_app_support()`` (e.g. inside a
    frozen ``.app`` under ``~/Library/Application Support``) is exempt, because
    it does not touch the repo-root governance runtime state.
    """
    violations: list[str] = []
    for path in _iter_python_files(root, "src"):
        rel = str(path.relative_to(root))
        text = _read_text(path)
        exempt_range: tuple[int, int] | None = None
        if rel in _VNX_DATA_APP_SUPPORT_CREATORS:
            exempt_range = _function_line_range(text, _VNX_DATA_APP_SUPPORT_CREATORS[rel])
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _has_ignore_pragma(line):
                continue
            if ".vnx-data" in line:
                if rel in _VNX_DATA_LEGACY_FILES:
                    break
                if exempt_range and exempt_range[0] <= lineno <= exempt_range[1]:
                    break
                violations.append(
                    f"[vnx-data-isolation] {rel}:{lineno}: app code references .vnx-data: {line.strip()[:80]}"
                )
                break  # one violation per file is enough
    return violations


def check_ci_step(root: Path) -> list[str]:
    """The architecture check must be invoked in CI."""
    ci = root / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        return ["[ci-step] .github/workflows/ci.yml is missing"]
    text = _read_text(ci)
    if "check_architecture_boundaries.py" not in text:
        return [
            "[ci-step] .github/workflows/ci.yml does not invoke scripts/check_architecture_boundaries.py"
        ]
    return []


ALL_CHECKS = [
    ("Provider SDK confinement", check_provider_sdk_confinement),
    ("Anthropic SDK forbidden", check_anthropic_forbidden),
    ("Network-client allow-list", check_network_allowlist),
    ("Dashboard vanilla HTML/CSS/JS", check_dashboard_vanilla),
    ("WebSocket hub localhost-only", check_hub_localhost_only),
    (".vnx-data isolation", check_vnx_data_isolation),
    ("CI step present", check_ci_step),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enforce architecture boundaries")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan (default: current working directory)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    all_violations: list[str] = []
    for name, check in ALL_CHECKS:
        violations = check(root)
        if violations:
            all_violations.extend(violations)

    if all_violations:
        print("Architecture boundary violations found:", file=sys.stderr)
        for v in all_violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("All architecture boundary checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
