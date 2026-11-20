"""Benchmark result files are exported publicly: they must not carry absolute local paths."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parent.parent / "docs" / "benchmarks"
_ABSOLUTE = re.compile(r"/Users/|/private/|/home/|/tmp/|[A-Za-z]:\\")
_FILES = sorted(_BENCH_DIR.glob("**/*.json"))


def test_benchmark_json_files_exist() -> None:
    assert _FILES


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_benchmark_json_has_no_absolute_path(path: Path) -> None:
    # JSON escapes a backslash as "\\\\", so check the decoded text too.
    text = path.read_text(encoding="utf-8").replace("\\\\", "\\")
    assert _ABSOLUTE.search(text) is None
