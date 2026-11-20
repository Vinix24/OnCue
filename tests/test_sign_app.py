"""sign_app: signing order, notarization status handling and the profile refusal, with the command seam mocked."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("sign_app", ROOT / "scripts" / "sign_app.py")
sa = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sa)


def macho(filetype: int) -> bytes:
    """A 64-bit little-endian Mach-O header with the given file type (2 = MH_EXECUTE, 6 = MH_DYLIB, 8 = MH_BUNDLE)."""
    return b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + (0).to_bytes(4, "little") \
        + filetype.to_bytes(4, "little") + b"\0" * 12


MH_EXECUTE, MH_DYLIB, MH_BUNDLE = 2, 6, 8
MACHO = macho(MH_EXECUTE)


class Recorder:
    def __init__(self, replies=None):
        self.cmds: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, cmd):
        self.cmds.append(cmd)
        for key, (rc, out) in self.replies.items():
            if key in " ".join(cmd):
                return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture()
def app(tmp_path):
    a = tmp_path / "OnCue.app"
    for rel in ["Contents/MacOS/OnCue", "Contents/Resources/bin/audiotee",
                "Contents/Resources/lib/python3.12/lib-dynload/x.so",
                "Contents/Frameworks/Python.framework/Versions/3.12/Python",
                "Contents/Frameworks/libz.dylib"]:
        f = a / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        kind = MH_DYLIB if rel.endswith((".dylib", "Python")) else MH_BUNDLE if rel.endswith(".so") else MH_EXECUTE
        f.write_bytes(macho(kind))
    (a / "Contents/Resources/readme.txt").write_text("text")
    os.symlink("OnCue", a / "Contents/MacOS/link")
    return a


@pytest.fixture()
def ent(tmp_path):
    p = tmp_path / "e.entitlements"
    p.write_text("<plist/>")
    return p


def _signed(rec):
    return [c[-1] for c in rec.cmds if c[0] == "codesign"]


def test_signing_is_inside_out_and_app_last(app, ent):
    rec = Recorder()
    sa.sign_app(app, "ID", ent, rec)
    signed = [Path(p) for p in _signed(rec)]
    rel = [p.relative_to(app).as_posix() if p != app else "." for p in signed]
    assert rel[-1] == "."
    assert rel[-2] == "Contents/Frameworks/Python.framework"
    files = rel[:-2]
    assert sorted(files) == sorted(["Contents/MacOS/OnCue", "Contents/Resources/bin/audiotee",
        "Contents/Resources/lib/python3.12/lib-dynload/x.so",
        "Contents/Frameworks/Python.framework/Versions/3.12/Python", "Contents/Frameworks/libz.dylib"])
    depths = [len(Path(f).parts) for f in files]
    assert depths == sorted(depths, reverse=True)
    assert "Contents/MacOS/link" not in rel and "Contents/Resources/readme.txt" not in rel


def test_hardened_runtime_timestamp_and_never_deep(app, ent):
    rec = Recorder()
    sa.sign_app(app, "ID", ent, rec)
    for cmd in rec.cmds:
        assert "--deep" not in cmd
        assert cmd[:7] == ["codesign", "--force", "--options", "runtime", "--timestamp", "--sign", "ID"]
    with_ent = {Path(c[-1]).name for c in rec.cmds if "--entitlements" in c}
    assert {"OnCue", "audiotee", "OnCue.app"} <= with_ent
    assert not ({"x.so", "libz.dylib", "Python.framework"} & with_ent)


def test_codesign_failure_stops_signing(app, ent):
    rec = Recorder({"codesign": (1, "")})
    with pytest.raises(sa.SigningError):
        sa.sign_app(app, "ID", ent, rec)
    assert len(rec.cmds) == 1


def test_missing_entitlements_file_signs_nothing(app, tmp_path):
    rec = Recorder()
    with pytest.raises(sa.SigningError):
        sa.sign_app(app, "ID", tmp_path / "nope", rec)
    assert rec.cmds == []


def test_dmg_without_profile_is_refused_before_signing(tmp_path):
    rec = Recorder()
    with pytest.raises(sa.SigningError, match="NOTARY_PROFILE"):
        sa.sign_and_notarize_dmg(tmp_path / "a.dmg", "ID", "", rec)
    assert rec.cmds == []


def test_build_script_refuses_cert_without_profile(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SALES_COPILOT_")}
    env["SALES_COPILOT_CERT_ID"] = "Developer ID Application: X (TEAM)"
    r = subprocess.run(["bash", str(ROOT / "scripts" / "build_dmg.sh"), "--out", str(tmp_path)],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "SALES_COPILOT_NOTARY_PROFILE" in r.stderr


def test_no_password_route_left_in_build_files():
    for name in ("build_dmg.sh", "sign_app.py"):
        assert "NOTARY_KEY" not in (ROOT / "scripts" / name).read_text()


def _submit(status, rc=0):
    return {"submit": (rc, json.dumps({"id": "abc-1", "status": status}))}


def test_accepted_returns_id():
    assert sa.notarize(Path("a.dmg"), "prof", Recorder(_submit("Accepted")), "5m") == "abc-1"


@pytest.mark.parametrize("status", ["Invalid", "Rejected", "", "accepted", "Weird"])
def test_other_status_fails_and_prints_log(status, capsys):
    rec = Recorder({**_submit(status), "notarytool log": (0, "the log")})
    with pytest.raises(sa.SigningError, match="not Accepted"):
        sa.notarize(Path("a.dmg"), "prof", rec, "5m")
    assert any("log" in c and "abc-1" in c for c in rec.cmds)
    assert "the log" in capsys.readouterr().out


def test_accepted_with_nonzero_exit_fails():
    with pytest.raises(sa.SigningError):
        sa.notarize(Path("a.dmg"), "prof", Recorder(_submit("Accepted", rc=1)), "5m")


def test_in_progress_reports_id_and_does_not_resubmit():
    rec = Recorder(_submit("In Progress", rc=1))
    with pytest.raises(sa.SigningError, match="abc-1.*In Progress"):
        sa.notarize(Path("a.dmg"), "prof", rec, "5m")
    assert len(rec.cmds) == 1


@pytest.mark.parametrize("out", ["", "not json", "{}", '{"id": "x"}', "null"])
def test_unparseable_submit_output_fails(out):
    with pytest.raises(sa.SigningError):
        sa.notarize(Path("a.dmg"), "prof", Recorder({"submit": (0, out)}), "5m")


def test_staple_runs_only_after_accepted(tmp_path):
    rec = Recorder(_submit("Invalid"))
    with pytest.raises(sa.SigningError):
        sa.sign_and_notarize_dmg(tmp_path / "a.dmg", "ID", "prof", rec)
    assert not any("staple" in c for c in rec.cmds)
    rec = Recorder(_submit("Accepted"))
    sa.sign_and_notarize_dmg(tmp_path / "a.dmg", "ID", "prof", rec)
    assert rec.cmds[-1][:3] == ["xcrun", "stapler", "staple"]


def test_verify_runs_every_check_and_detaches(tmp_path):
    rec = Recorder()
    sa.verify(tmp_path / "OnCue.app", tmp_path / "a.dmg", rec)
    joined = [" ".join(c) for c in rec.cmds]
    assert "codesign --verify --strict" in joined[0]
    assert "stapler validate" in joined[1]
    assert "--type open --context context:primary-signature -v" in joined[2]
    assert "hdiutil attach" in joined[3]
    assert "--type execute -v" in joined[4] and "OnCue.app" in joined[4]
    assert joined[5].startswith("hdiutil detach")


@pytest.mark.parametrize("failing", ["codesign --verify", "stapler validate", "--type open", "--type execute"])
def test_each_verification_failure_fails(tmp_path, failing):
    rec = Recorder({failing: (3, "bad")})
    with pytest.raises(sa.SigningError):
        sa.verify(tmp_path / "OnCue.app", tmp_path / "a.dmg", rec)
    if failing == "--type execute":
        assert rec.cmds[-1][:2] == ["hdiutil", "detach"]


def test_entitlements_follow_the_macho_file_type_not_the_name(tmp_path):
    fat = tmp_path / "fat"
    fat.write_bytes(b"\xca\xfe\xba\xbe" + (1).to_bytes(4, "big") + b"\0" * 8 + (28).to_bytes(4, "big")
                    + b"\0" * 8 + macho(MH_EXECUTE))
    assert sa.is_executable(fat)
    for name, kind, want in [("exe", MH_EXECUTE, True), ("lib", MH_DYLIB, False), ("bun", MH_BUNDLE, False)]:
        f = tmp_path / name
        f.write_bytes(macho(kind))
        assert sa.is_executable(f) is want
    short = tmp_path / "short"
    short.write_bytes(b"\xcf\xfa\xed\xfe")
    assert sa.is_executable(short) is False  # truncated header: never entitled


def test_python_framework_binary_gets_no_entitlements(app, ent):
    rec = Recorder()
    sa.sign_app(app, "ID", ent, rec)
    entitled = {c[-1] for c in rec.cmds if "--entitlements" in c}
    assert not any(p.endswith("Versions/3.12/Python") for p in entitled)


def test_failed_detach_fails_the_verification(tmp_path):
    rec = Recorder({"hdiutil detach": (16, "resource busy")})
    with pytest.raises(sa.SigningError, match="still mounted"):
        sa.verify(tmp_path / "OnCue.app", tmp_path / "a.dmg", rec)


def test_failed_detach_does_not_hide_the_verification_error(tmp_path, capsys):
    rec = Recorder({"--type execute": (3, "rejected"), "hdiutil detach": (16, "resource busy")})
    with pytest.raises(sa.SigningError, match="verification failed"):
        sa.verify(tmp_path / "OnCue.app", tmp_path / "a.dmg", rec)
    err = capsys.readouterr().err
    assert "detach" in err and "resource busy" in err
