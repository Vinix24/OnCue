"""CHANGELOG fragments: repo state and scripts/changelog_assemble.py behaviour."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("changelog_assemble", ROOT / "scripts" / "changelog_assemble.py")
ca = importlib.util.module_from_spec(_spec)
sys.modules["changelog_assemble"] = ca
_spec.loader.exec_module(ca)

BASE = "# Changelog\n\n## [Unreleased]\n\nPointer line.\n\n## [0.9.0] — 2026-01-01\n\n### Added\n\n- old\n"


def _make(tmp_path: Path, changelog: str, frags: dict[str, str]) -> Path:
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    for name, body in frags.items():
        (tmp_path / "changelog.d" / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_repo_fragments_pass_check():
    assert ca.main(["--check", "--root", str(ROOT)]) == 0


def test_unreleased_section_has_no_list_item():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[|\Z)", text, re.S | re.M)
    assert m, "CHANGELOG.md has no ## [Unreleased] section"
    items = [ln for ln in m.group(1).splitlines() if re.match(r"^\s*[-*+] ", ln)]
    assert items == [], "add a fragment in changelog.d/ instead of editing CHANGELOG.md"


def test_new_version_section_created_below_unreleased(tmp_path):
    root = _make(tmp_path, BASE, {"a.fixed.md": "- fix a\n", "b.added.md": "- add b\n"})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## [Unreleased]") < text.index("## [1.0.0] — 2026-02-02") < text.index("## [0.9.0]")
    new = text.split("## [1.0.0] — 2026-02-02")[1].split("## [0.9.0]")[0]
    assert new.index("### Added") < new.index("### Fixed")
    assert "- add b" in new and "- fix a" in new
    assert "Pointer line." in text.split("## [1.0.0]")[0]
    assert not list((root / "changelog.d").glob("*.md"))


def test_merge_into_existing_version_and_canonical_heading_order(tmp_path):
    cl = (
        "# Changelog\n\n## [Unreleased]\n\nPointer.\n\n## [1.0.0] — 2026-11-18\n\n"
        "### Highlights\n\n- h\n\n### Added\n\n- a1\n\n### Security\n\n- s1\n\n## [0.9.0]\n\n- x\n"
    )
    root = _make(tmp_path, cl, {"z.fixed.md": "- fixed z\n", "y.added.md": "- added y\n"})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-11-19"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    sec = text.split("## [1.0.0]")[1].split("## [0.9.0]")[0]
    assert sec.startswith(" — 2026-11-19")
    heads = re.findall(r"^### (.+)$", sec, re.M)
    assert heads == ["Highlights", "Added", "Fixed", "Security"]
    assert "- a1\n- added y\n" in sec
    assert "### Fixed\n\n- fixed z\n\n### Security" in sec
    assert "## [0.9.0]" in text and text.count("## [1.0.0]") == 1


def test_items_ordered_by_filename_and_continuation_kept(tmp_path):
    root = _make(
        tmp_path,
        BASE,
        {"b-two.added.md": "- two\n  more\n", "a-one.added.md": "- one\n", "c.added.md": "- three\n"},
    )
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 0
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### Added\n\n- one\n- two\n  more\n- three\n" in text


def test_dry_run_leaves_files_untouched(tmp_path, capsys):
    root = _make(tmp_path, BASE, {"a.fixed.md": "- fix a\n"})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02", "--dry-run"]) == 0
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == BASE
    assert (root / "changelog.d" / "a.fixed.md").exists()
    out = capsys.readouterr().out
    assert "+## [1.0.0] — 2026-02-02" in out and "+- fix a" in out


@pytest.mark.parametrize(
    "name,body",
    [
        ("Bad_Slug.fixed.md", "- x\n"),
        ("a.unknown.md", "- x\n"),
        ("a.fixed.txt", "- x\n"),
        ("a.fixed.md", ""),
        ("a.fixed.md", "\n\n"),
        ("a.fixed.md", "# Heading\n- x\n"),
        ("a.fixed.md", "plain text\n"),
        ("a.fixed.md", "  indented first\n"),
        ("a.fixed.md", "- \n"),
    ],
)
def test_invalid_fragment_is_refused(tmp_path, capsys, name, body):
    root = _make(tmp_path, BASE, {name: body})
    assert ca.main(["--check", "--root", str(root)]) == 1
    assert name in capsys.readouterr().err
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 1
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == BASE
    assert (root / "changelog.d" / name).exists()


def test_one_bad_fragment_blocks_good_ones(tmp_path):
    root = _make(tmp_path, BASE, {"good.fixed.md": "- ok\n", "bad.fixed.md": "oops\n"})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 1
    assert (root / "changelog.d" / "good.fixed.md").exists()


def test_readme_is_never_assembled(tmp_path):
    root = _make(tmp_path, BASE, {"README.md": "# explains\n", "a.fixed.md": "- fix\n"})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 0
    assert (root / "changelog.d" / "README.md").exists()
    assert "explains" not in (root / "CHANGELOG.md").read_text(encoding="utf-8")


def test_no_fragments_and_bad_date_fail(tmp_path):
    root = _make(tmp_path, BASE, {})
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "2026-02-02"]) == 1
    (root / "changelog.d" / "a.fixed.md").write_text("- x\n", encoding="utf-8")
    assert ca.main(["--root", str(root), "--version", "1.0.0", "--date", "02-02-2026"]) == 1
    shutil.copy(root / "CHANGELOG.md", root / "copy.md")
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == BASE
