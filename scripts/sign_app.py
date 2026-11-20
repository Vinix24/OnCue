#!/usr/bin/env python3
"""Developer ID signing, notarization and verification for OnCue (ADR-006 points 4, 5, 9).

Subcommands (every external call goes through run(), the seam the tests replace):
  app  <OnCue.app> --identity NAME --entitlements FILE [--dry-run]
        sign inside-out: every Mach-O file, then the .framework bundles, then nested .app
        bundles (the recording bar, signed as a bundle without entitlements), then the app.
        Hardened runtime and a secure timestamp; never --deep. --dry-run prints the signing
        order and the codesign commands and signs nothing.
  dmg  <file.dmg> --identity NAME --profile NAME
        sign the .dmg, submit it with a notarytool keychain profile, wait, staple.
        Any status other than Accepted fails and prints the notarytool log.
  verify <OnCue.app> <file.dmg>
        codesign --verify --strict on the app, stapler validate and spctl on the .dmg,
        spctl --assess --type execute on the app inside the mounted .dmg.
Exit code 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # 64-bit little endian
    b"\xca\xfe\xba\xbe",  # fat
    b"\xbe\xba\xfe\xca",  # fat, little endian
}
_MH_EXECUTE = 0x2
_MH_THIN_MAGICS = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}


def is_executable(path: Path) -> bool:
    """True when the Mach-O file type is MH_EXECUTE (not MH_DYLIB or MH_BUNDLE).

    A fat file is an executable when its first slice is. Anything unreadable counts as not an executable,
    so it never receives entitlements.
    """
    with path.open("rb") as fh:
        head = fh.read(4)
        if head in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
            order = ">" if head == b"\xca\xfe\xba\xbe" else "<"
            fh.read(4)  # architecture count, then the first fat_arch: cputype, cpusubtype, offset
            entry = fh.read(12)
            if len(entry) < 12:
                return False
            fh.seek(int.from_bytes(entry[8:12], "big" if order == ">" else "little"))
            head = fh.read(4)
        order = _MH_THIN_MAGICS.get(head)
        if order is None:
            return False
        header = fh.read(8)  # cputype, cpusubtype
        filetype = fh.read(4)
    if len(header) < 8 or len(filetype) < 4:
        return False
    return int.from_bytes(filetype, "little" if order == "<" else "big") == _MH_EXECUTE


class SigningError(RuntimeError):
    """A signing, notarization or verification step failed."""


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """The command seam: runs one external program and captures its output."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _must(cmd: list[str], runner=run) -> str:
    proc = runner(cmd)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SigningError(f"{' '.join(cmd[:3])} failed ({proc.returncode}): {detail}")
    return proc.stdout or ""


def _inside_nested_app(app: Path, path: Path) -> bool:
    """True when ``path`` sits inside a nested ``.app`` bundle of ``app`` (not ``app`` itself)."""
    return any(part.endswith(".app") for part in path.relative_to(app).parts[:-1])


def nested_apps(app: Path) -> list[Path]:
    """Nested .app bundles (the embedded recording bar), deepest first, without following symlinks."""
    found = [Path(r) / d for r, dirs, _f in os.walk(app, followlinks=False) for d in dirs
             if d.endswith(".app") and not (Path(r) / d).is_symlink()]
    return sorted(found, key=lambda p: (-len(p.relative_to(app).parts), str(p)))


def macho_files(app: Path) -> list[Path]:
    """Every Mach-O file of the bundle outside nested .app bundles, deepest first (inside-out).

    Symlinks are skipped. A nested .app is signed as a bundle by ``sign_app`` (its own identity,
    no entitlements), so its files are not signed one by one with the outer app's entitlements.
    """
    found = []
    for root, _dirs, files in os.walk(app, followlinks=False):
        for name in files:
            p = Path(root) / name
            if p.is_symlink() or _inside_nested_app(app, p):
                continue
            with p.open("rb") as fh:
                if fh.read(4) in _MACHO_MAGICS:
                    found.append(p)
    return sorted(found, key=lambda p: (-len(p.relative_to(app).parts), str(p)))


def frameworks(app: Path) -> list[Path]:
    """The .framework bundles, deepest first, without following symlinks."""
    found = [Path(r) / d for r, dirs, _f in os.walk(app, followlinks=False) for d in dirs
             if d.endswith(".framework") and not (Path(r) / d).is_symlink()]
    return sorted(found, key=lambda p: (-len(p.relative_to(app).parts), str(p)))


def _codesign(target: Path, identity: str, entitlements: Path | None, runner) -> None:
    cmd = ["codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity]
    if entitlements is not None:
        cmd += ["--entitlements", str(entitlements)]
    _must(cmd + [str(target)], runner)


def sign_app(app: Path, identity: str, entitlements: Path, runner=run) -> list[Path]:
    """Sign inside-out; returns the signing order. Only MH_EXECUTE files get entitlements; nested apps do not."""
    if not identity:
        raise SigningError("no signing identity")
    if not entitlements.is_file():
        raise SigningError(f"entitlements file missing: {entitlements}")
    order: list[Path] = []
    for f in macho_files(app):
        _codesign(f, identity, entitlements if is_executable(f) else None, runner)
        order.append(f)
    for fw in frameworks(app):
        _codesign(fw, identity, None, runner)
        order.append(fw)
    for nested in nested_apps(app):
        _codesign(nested, identity, None, runner)  # the bar reads a localhost port; it needs no entitlements
        order.append(nested)
    _codesign(app, identity, entitlements, runner)
    order.append(app)
    return order


def notarize(dmg: Path, profile: str, runner=run, timeout: str | None = None) -> str:
    """Submit and wait. Returns the submission id; raises unless the status is exactly Accepted."""
    if not profile:
        raise SigningError("SALES_COPILOT_NOTARY_PROFILE is not set (a notarytool keychain profile name)")
    timeout = timeout or os.environ.get("SALES_COPILOT_NOTARY_TIMEOUT", "20m")
    proc = runner(["xcrun", "notarytool", "submit", str(dmg), "--keychain-profile", profile,
                   "--wait", "--timeout", timeout, "--output-format", "json"])
    try:
        info = json.loads(proc.stdout or "")
        sub_id, status = str(info["id"]), str(info["status"])
    except (ValueError, KeyError, TypeError):
        raise SigningError(f"notarytool gave no submission id and status ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout or '').strip()}") from None
    print(f"notarytool submission id: {sub_id}, status: {status}")
    if status == "Accepted" and proc.returncode == 0:
        return sub_id
    if status == "In Progress":
        raise SigningError(f"notarization of {sub_id} is still In Progress; not resubmitting. "
                           f"Check later: xcrun notarytool info {sub_id} --keychain-profile {profile}")
    log = runner(["xcrun", "notarytool", "log", sub_id, "--keychain-profile", profile])
    print((log.stdout or log.stderr or "").strip())
    raise SigningError(f"notarization of {sub_id} ended as {status!r}, not Accepted")


def sign_and_notarize_dmg(dmg: Path, identity: str, profile: str, runner=run) -> str:
    if not profile:  # refuse before anything is signed or uploaded
        raise SigningError("SALES_COPILOT_NOTARY_PROFILE is not set (a notarytool keychain profile name)")
    _codesign(dmg, identity, None, runner)
    sub_id = notarize(dmg, profile, runner)
    _must(["xcrun", "stapler", "staple", str(dmg)], runner)
    return sub_id


def verify(app: Path, dmg: Path, runner=run) -> None:
    """Each check fails the build; the output of each is printed."""
    checks = [
        ["codesign", "--verify", "--strict", "--verbose=2", str(app)],
        ["xcrun", "stapler", "validate", str(dmg)],
        ["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "-v", str(dmg)],
    ]
    for cmd in checks:
        proc = runner(cmd)
        print(f"$ {' '.join(cmd)}\n{(proc.stdout or '') + (proc.stderr or '')}".rstrip())
        if proc.returncode != 0:
            raise SigningError(f"verification failed: {' '.join(cmd)}")
    mount = tempfile.mkdtemp(prefix="oncue-mnt-")
    _must(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, str(dmg)], runner)
    failure: SigningError | None = None
    try:
        inner = Path(mount) / app.name
        cmd = ["spctl", "--assess", "--type", "execute", "-v", str(inner)]
        proc = runner(cmd)
        print(f"$ {' '.join(cmd)}\n{(proc.stdout or '') + (proc.stderr or '')}".rstrip())
        if proc.returncode != 0:
            failure = SigningError(f"verification failed: {' '.join(cmd)}")
    finally:
        detach = runner(["hdiutil", "detach", mount])
        detached = detach.returncode == 0
        if detached:
            try:
                os.rmdir(mount)  # empty mount point only
            except OSError:
                pass
    if failure is not None:
        if not detached:
            print(f"WARNING: hdiutil detach {mount} failed ({detach.returncode}): "
                  f"{(detach.stderr or detach.stdout or '').strip()}", file=sys.stderr)
        raise failure
    if not detached:
        raise SigningError(f"hdiutil detach {mount} failed ({detach.returncode}), the image is still mounted: "
                           f"{(detach.stderr or detach.stdout or '').strip()}")


def _dry_run(cmd: list[str]) -> subprocess.CompletedProcess:
    """A runner that prints the command and succeeds without running it."""
    print(f"would run: {' '.join(cmd)}")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("app")
    p.add_argument("app", type=Path)
    p.add_argument("--identity", required=True)
    p.add_argument("--entitlements", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="print the signing order and commands, sign nothing")
    p = sub.add_parser("dmg")
    p.add_argument("dmg", type=Path)
    p.add_argument("--identity", required=True)
    p.add_argument("--profile", default="")
    p = sub.add_parser("verify")
    p.add_argument("app", type=Path)
    p.add_argument("dmg", type=Path)
    a = ap.parse_args(argv[1:])
    try:
        if a.cmd == "app":
            if a.dry_run:
                order = sign_app(a.app, a.identity, a.entitlements, _dry_run)
                print("dry run, nothing signed. Signing order:")
                for n, item in enumerate(order, 1):
                    print(f"  {n}. {item.relative_to(a.app) if item != a.app else a.app.name}")
                return 0
            order = sign_app(a.app, a.identity, a.entitlements)
            print(f"signed {len(order)} item(s), inside-out, hardened runtime, no --deep")
        elif a.cmd == "dmg":
            sign_and_notarize_dmg(a.dmg, a.identity, a.profile)
        else:
            verify(a.app, a.dmg)
    except SigningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
