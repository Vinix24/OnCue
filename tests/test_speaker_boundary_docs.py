"""The speaker boundary (no voice profiles stored) is documented and checked in code."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sales_copilot" / "modules"
DIARIZATION_FILES = sorted((SRC / "diarizer").glob("*.py")) + sorted((SRC / "reports").glob("diarize_*.py"))

# Method/function names that put bytes or objects on disk.
WRITE_NAMES = {
    "save",
    "savez",
    "savez_compressed",
    "savetxt",
    "tofile",
    "dump",
    "dumps",
    "write_bytes",
    "write_text",
    "connect",
    "mkstemp",
    "NamedTemporaryFile",
    "to_pickle",
    "to_csv",
    "to_json",
}
# The only functions allowed to write: they download model weights (no call audio, no embeddings).
ALLOWED = {("diarize_sherpa.py", "_download"), ("diarize_sherpa.py", "ensure_sherpa_models")}


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    return f.id if isinstance(f, ast.Name) else ""


def _is_write_open(node: ast.Call) -> bool:
    if _call_name(node) not in {"open", "fdopen"}:
        return False
    modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
    modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
    return any(isinstance(m, str) and any(c in m for c in "wax+") for m in modes)


def _write_sites(path: Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text())
    sites: list[tuple[str, int, str]] = []

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call) and (_call_name(child) in WRITE_NAMES or _is_write_open(child)):
                sites.append((func, child.lineno, _call_name(child)))
            visit(child, name)

    visit(tree, "<module>")
    return sites


def test_diarization_modules_are_found() -> None:
    names = {p.name for p in DIARIZATION_FILES}
    assert {"pyannote_v3.py", "diarize_merge.py", "diarize_sherpa.py"} <= names


def test_diarization_modules_write_nothing_but_model_downloads() -> None:
    offenders = [
        f"{p.name}:{line} {func}() calls {call}"
        for p in DIARIZATION_FILES
        for func, line, call in _write_sites(p)
        if (p.name, func) not in ALLOWED
    ]
    message = "diarization code writes to disk, embeddings must stay in memory: " + "; ".join(offenders)
    assert offenders == [], message


def test_scanner_flags_an_embedding_write(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import numpy as np\ndef run(emb):\n    np.save('x.npy', emb)\n    open('y', 'wb').write(b'')\n")
    assert [(f, c) for f, _, c in _write_sites(bad)] == [("run", "save"), ("run", "open")]


def test_boundary_is_documented() -> None:
    boundaries = (ROOT / "docs" / "ARCHITECTURE_BOUNDARIES.md").read_text()
    privacy = (ROOT / "docs" / "PRIVACY.md").read_text()
    faq = (ROOT / "docs" / "FAQ.md").read_text()
    needles = (
        "Diarization",
        "Speaker assignment per channel",
        "Speaker identification",
        "article 9",
        "participant list",
    )
    for needle in needles:
        assert needle in boundaries
    assert "speaker-boundary-no-voice-profiles-are-stored" in privacy and "article 9" in privacy
    assert "speaker-boundary-no-voice-profiles-are-stored" in faq
