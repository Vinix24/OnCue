"""Verify that ``sales_copilot.__main__`` uses lazy imports for heavy modules.

PR #149 (Windows install-friction, GitHub issue #148): ``detector_main`` and
``reports_main`` were imported unconditionally at module top-level, which meant
a capture-only ``pip install ".[windows]"`` run required all detector deps
(semantic-router, sentence-transformers, instructor) and aiosqlite just to
import the orchestrator. Now those imports are local to ``_run_call()``, so the
orchestrator module is importable without those extras.
"""

from __future__ import annotations

import inspect


def test_orchestrator_no_top_level_detector_import() -> None:
    """Importing __main__ must not pull detector_main at module level."""
    import sales_copilot.__main__ as orchestrator

    assert not hasattr(orchestrator, "detector_main"), (
        "detector_main should be a local import inside _run_call(), not a top-level attribute"
    )


def test_orchestrator_no_top_level_reports_import() -> None:
    """Importing __main__ must not pull reports_main at module level."""
    import sales_copilot.__main__ as orchestrator

    assert not hasattr(orchestrator, "reports_main"), (
        "reports_main should be a local import inside _run_call(), not a top-level attribute"
    )


def test_orchestrator_still_has_talk_time_import() -> None:
    """Importing __main__ MUST still have talk_time_main — it is lightweight."""
    import sales_copilot.__main__ as orchestrator

    assert hasattr(orchestrator, "talk_time_main"), (
        "talk_time_main should remain a top-level import for capture-only runs"
    )


def test_orchestrator_importable_without_detector_deps_structure() -> None:
    """The orchestrator module top-level must not reference detector or reports submodules.

    This is a structural assertion: we grep the source for top-level imports of
    detector.__main__ and reports.__main__ (outside of function bodies). The
    test catches regressions where someone re-adds the imports at module level.
    """
    import sales_copilot.__main__ as orchestrator

    source = inspect.getsource(orchestrator)
    lines_before_functions = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            break
        if stripped.startswith("class "):
            break
        lines_before_functions.append(stripped)

    top_level = "\n".join(lines_before_functions)
    assert "modules.detector" not in top_level, (
        "modules.detector should not appear at top-level — lazy import regression"
    )
    assert "modules.reports" not in top_level, (
        "modules.reports should not appear at top-level — lazy import regression"
    )
    # talk_time is still a top-level import and should appear.
    assert "modules.talk_time" in top_level, (
        "modules.talk_time should still be a top-level import"
    )
