#!/usr/bin/env python3
"""Audit installed dependency licenses for AGPL-3.0 distribution compatibility.

Live Sales Copilot is distributed under the GNU Affero General Public License,
version 3 (see ``LICENSE`` and ``docs/LICENSING.md``). When you distribute a
combined work under the AGPL, every bundled dependency must carry a license that
is compatible with AGPL-3.0. This script inspects the dependencies installed in
the current Python environment (via ``importlib.metadata``) and classifies each
one as:

- COMPATIBLE   permissive (MIT, BSD, Apache-2.0, ISC, ...) or an AGPL-compatible
               copyleft license (LGPL, GPL-3.0, AGPL-3.0, MPL-2.0, ...).
- INCOMPATIBLE proprietary / commercial / non-commercial / source-available
               (SSPL, BUSL, ...) or a copyleft license that conflicts with
               distributing under AGPL-3.0 (GPL-2.0-only, EPL, CDDL, MPL-1.1).
- UNKNOWN      no recognizable license metadata; needs a manual look.

Run it with the project virtualenv so it sees the real dependency set::

    .venv/bin/python scripts/check_dependency_licenses.py

Exit status is 0 (PASS) when no INCOMPATIBLE dependency is found, and 1 (FAIL)
when at least one is. Pass ``--fail-on-unknown`` to also fail on UNKNOWN.

This is an engineering aid, not legal advice. License metadata is often sparse or
wrong; treat every INCOMPATIBLE or UNKNOWN result as a prompt to verify the real
upstream license before shipping.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from importlib import metadata

COMPATIBLE = "COMPATIBLE"
INCOMPATIBLE = "INCOMPATIBLE"
UNKNOWN = "UNKNOWN"

# Short signals only (SPDX expression, classifier trailing segment, or a short
# free-text License field) are scanned for these markers. Long license bodies are
# never scanned for incompatibility, because permissive license texts routinely
# contain words like "commercial" and would trigger false positives.
INCOMPATIBLE_MARKERS = (
    "proprietary",
    "all rights reserved",
    "non-commercial",
    "noncommercial",
    "sspl",
    "server side public license",
    "business source",
    "busl",
    "elastic license",
    "commons clause",
    "prosperity public license",
    # Copyleft licenses that are genuinely GPL/AGPL-incompatible.
    "cddl",
    "common development and distribution",
    "eclipse public license",
    "epl-1",
    "epl-2",
    "mpl-1.1",
    "mozilla public license 1",
    "eupl",  # one-way compatible; flag for manual review under AGPL distribution
)

# Markers that mean "AGPL-3.0 can distribute alongside this".
COMPATIBLE_MARKERS = (
    "mit",
    "bsd",  # 2-/3-clause; 4-clause is handled as an incompatible special case
    "apache",
    "isc",
    "python software foundation",
    "psf",
    "python-2.0",
    "zlib",
    "libpng",
    "mpl-2",
    "mozilla public license 2",
    "lgpl",
    "lesser general public",
    "gpl-3",
    "gplv3",
    "gnu general public license v3",
    "agpl",
    "affero",
    "unlicense",
    "public domain",
    "cc0",
    "0bsd",
    "boost software",
    "postgresql",
    "historical permission",
    "hpnd",
    "wtfpl",
    "universal permissive",
    "upl-1",
)


@dataclass
class Report:
    name: str
    version: str
    license_label: str
    verdict: str
    reason: str


def _is_gpl2_only(text: str) -> bool:
    """True when a short signal names GPL v2 without a v3 or 'or later' escape.

    GPL-2.0-only is incompatible with (A)GPL-3.0. GPL-2.0-or-later is fine because
    the recipient may upgrade to v3. We only conclude 'only' when the text clearly
    pins version 2 and shows no later-version escape hatch and no v3 mention.
    """
    t = text.lower()
    if "gpl" not in t:
        return False
    mentions_v2 = bool(re.search(r"gpl[\s._-]*(v|version)?[\s._-]*2\b", t)) or "gplv2" in t
    if not mentions_v2:
        return False
    mentions_v3 = bool(re.search(r"gpl[\s._-]*(v|version)?[\s._-]*3\b", t)) or "gplv3" in t
    has_or_later = "or later" in t or "or-later" in t or "+" in t or "any later" in t
    return mentions_v2 and not mentions_v3 and not has_or_later


def _verdict_for_signal(text: str, *, scan_incompatible: bool) -> tuple[str, str]:
    """Classify a single normalized license signal. Returns (verdict, reason)."""
    t = text.lower().strip()
    if not t:
        return UNKNOWN, "empty"

    if scan_incompatible:
        # 4-clause BSD has the advertising clause and is GPL-incompatible.
        if "bsd-4" in t or "4-clause bsd" in t or "original bsd" in t:
            return INCOMPATIBLE, "BSD-4-Clause (advertising clause) is GPL-incompatible"
        for marker in INCOMPATIBLE_MARKERS:
            if marker in t:
                return INCOMPATIBLE, f"matched incompatible marker '{marker}'"
        if _is_gpl2_only(t):
            return INCOMPATIBLE, "GPL-2.0-only conflicts with AGPL-3.0"

    for marker in COMPATIBLE_MARKERS:
        if marker in t:
            return COMPATIBLE, f"matched compatible marker '{marker}'"

    return UNKNOWN, "no recognized license marker"


def _classify_expression(expr: str) -> tuple[str, str]:
    """Classify a PEP 639 SPDX license expression (authoritative when present).

    For an ``OR`` expression, any compatible operand makes the whole compatible
    (the distributor may choose that operand). Otherwise incompatibility wins.
    """
    operands = re.split(r"\s+(?:or|and)\s+|\(|\)", expr, flags=re.IGNORECASE)
    operands = [o.strip() for o in operands if o.strip()]
    verdicts = [_verdict_for_signal(o, scan_incompatible=True) for o in operands]
    has_or = re.search(r"\bor\b", expr, flags=re.IGNORECASE) is not None

    if has_or and any(v == COMPATIBLE for v, _ in verdicts):
        return COMPATIBLE, f"SPDX OR-expression with a compatible operand: {expr}"
    if any(v == INCOMPATIBLE for v, _ in verdicts):
        bad = next(r for v, r in verdicts if v == INCOMPATIBLE)
        return INCOMPATIBLE, f"SPDX expression '{expr}': {bad}"
    if any(v == COMPATIBLE for v, _ in verdicts):
        return COMPATIBLE, f"SPDX expression '{expr}' resolves compatible"
    return UNKNOWN, f"SPDX expression '{expr}' not recognized"


def _collect_signals(meta: metadata.PackageMetadata) -> tuple[list[str], list[str], str | None]:
    """Return (short_signals, long_signals, spdx_expression) from package metadata."""
    classifiers = meta.get_all("Classifier") or []
    license_classifiers = [
        c.split("::")[-1].strip() for c in classifiers if c.startswith("License ::")
    ]
    spdx_expr = meta.get("License-Expression")  # PEP 639
    raw_license = meta.get("License")

    short: list[str] = list(license_classifiers)
    long: list[str] = []
    if raw_license and raw_license.strip() and raw_license.strip().upper() != "UNKNOWN":
        if len(raw_license) <= 60 and "\n" not in raw_license.strip():
            short.append(raw_license.strip())
        else:
            long.append(raw_license)
    return short, long, (spdx_expr.strip() if spdx_expr else None)


def _display_label(short: list[str], spdx: str | None, has_long: bool) -> str:
    if spdx:
        return spdx
    if short:
        # De-duplicate while preserving order.
        seen: list[str] = []
        for s in short:
            if s not in seen:
                seen.append(s)
        return ", ".join(seen)
    if has_long:
        return "(license text in metadata)"
    return "(none)"


def classify_distribution(dist: metadata.Distribution) -> Report:
    meta = dist.metadata
    name = meta.get("Name") or "(unknown)"
    version = meta.get("Version") or "?"
    short, long, spdx = _collect_signals(meta)
    label = _display_label(short, spdx, bool(long))

    # 1. SPDX License-Expression is authoritative when present (PEP 639).
    if spdx:
        verdict, reason = _classify_expression(spdx)
        if verdict != UNKNOWN:
            return Report(name, version, label, verdict, reason)

    # 2. Classifiers and short License fields. Incompatible wins over compatible
    #    across these agreeing-by-nature signals; conservative for an audit.
    incompatible_hit: tuple[str, str] | None = None
    compatible_hit: tuple[str, str] | None = None
    for sig in short:
        verdict, reason = _verdict_for_signal(sig, scan_incompatible=True)
        if verdict == INCOMPATIBLE and incompatible_hit is None:
            incompatible_hit = (verdict, reason)
        elif verdict == COMPATIBLE and compatible_hit is None:
            compatible_hit = (verdict, reason)
    if incompatible_hit is not None:
        return Report(name, version, label, *incompatible_hit)
    if compatible_hit is not None:
        return Report(name, version, label, *compatible_hit)

    # 3. Long license body: only mine it for a compatible fingerprint.
    for body in long:
        verdict, reason = _verdict_for_signal(body[:4000], scan_incompatible=False)
        if verdict == COMPATIBLE:
            return Report(name, version, label, COMPATIBLE, "license text matched: " + reason)

    return Report(name, version, label, UNKNOWN, "no recognized license marker")


def gather_reports() -> list[Report]:
    seen: dict[str, Report] = {}
    for dist in metadata.distributions():
        try:
            report = classify_distribution(dist)
        except Exception as exc:  # noqa: BLE001 - never let one bad dist kill the audit
            name = getattr(dist, "name", None) or "(unreadable)"
            report = Report(str(name), "?", "(metadata error)", UNKNOWN, f"metadata error: {exc}")
        # A dist can appear once; keep the first, dedupe by lowercased name.
        key = report.name.lower()
        seen.setdefault(key, report)
    return sorted(seen.values(), key=lambda r: (r.verdict != INCOMPATIBLE, r.verdict != UNKNOWN, r.name.lower()))


def _print_table(reports: list[Report]) -> None:
    name_w = max((len(r.name) for r in reports), default=4)
    name_w = min(max(name_w, 4), 40)
    ver_w = min(max((len(r.version) for r in reports), default=7), 14)
    lic_w = 34

    def row(name: str, version: str, lic: str, verdict: str) -> str:
        lic_disp = lic if len(lic) <= lic_w else lic[: lic_w - 1] + "…"
        return f"{name[:name_w]:<{name_w}}  {version[:ver_w]:<{ver_w}}  {lic_disp:<{lic_w}}  {verdict}"

    print(row("Package", "Version", "License", "Verdict"))
    print("-" * (name_w + ver_w + lic_w + 24))
    for r in reports:
        print(row(r.name, r.version, r.license_label, r.verdict))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_dependency_licenses.py",
        description=(
            "Audit installed dependency licenses for AGPL-3.0 distribution "
            "compatibility. Run with the project virtualenv so it sees the real "
            "dependency set (e.g. .venv/bin/python scripts/check_dependency_licenses.py). "
            "Exit 0 = PASS (no incompatible dependency), exit 1 = FAIL."
        ),
        epilog="Engineering aid, not legal advice. Verify every flagged result upstream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fail-on-unknown",
        action="store_true",
        help="Also exit non-zero when any dependency license could not be recognized.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as JSON instead of a table (machine-readable).",
    )
    parser.add_argument(
        "--only",
        choices=["all", "incompatible", "unknown", "flagged"],
        default="all",
        help="Limit the printed rows: 'flagged' = incompatible + unknown (default: all).",
    )
    args = parser.parse_args(argv)

    reports = gather_reports()
    incompatible = [r for r in reports if r.verdict == INCOMPATIBLE]
    unknown = [r for r in reports if r.verdict == UNKNOWN]
    compatible = [r for r in reports if r.verdict == COMPATIBLE]

    if args.only == "incompatible":
        shown = incompatible
    elif args.only == "unknown":
        shown = unknown
    elif args.only == "flagged":
        shown = incompatible + unknown
    else:
        shown = reports

    failed = bool(incompatible) or (args.fail_on_unknown and bool(unknown))

    if args.json:
        payload = {
            "summary": {
                "total": len(reports),
                "compatible": len(compatible),
                "incompatible": len(incompatible),
                "unknown": len(unknown),
                "result": "FAIL" if failed else "PASS",
            },
            "dependencies": [vars(r) for r in shown],
        }
        print(json.dumps(payload, indent=2))
        return 1 if failed else 0

    print(f"AGPL-3.0 dependency license audit  ({len(reports)} distributions inspected)")
    print()
    if shown:
        _print_table(shown)
    else:
        print("(no rows to show for the selected filter)")
    print()
    print(
        f"Summary: {len(compatible)} compatible, "
        f"{len(incompatible)} incompatible, {len(unknown)} unknown."
    )

    if incompatible:
        print()
        print("AGPL-incompatible dependencies (must resolve before distributing):")
        for r in incompatible:
            print(f"  - {r.name} {r.version}: {r.license_label}  [{r.reason}]")

    if unknown:
        print()
        print(f"{len(unknown)} dependencies have no recognizable license metadata; verify manually.")

    print()
    if failed:
        print("RESULT: FAIL")
    else:
        print("RESULT: PASS")
        if unknown:
            print("  (PASS, but unknown-license dependencies still need a manual check.)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
