#!/usr/bin/env python3
"""py2app setup for the native macOS OnCue .app bundle.

Usage (from repo root, with a Python environment that has py2app installed):

    python setup_app.py py2app

This produces ``dist/OnCue.app``. The companion script
``scripts/build_dmg.sh`` wraps this step, codesigning, DMG creation and
notarization hooks.

Design choices (see ``claudedocs/plan-native-dmg-packaging.md``):

- py2app is the bundler (mature, macOS-native, Apple-Silicon).
- Whisper/turbo/large-v3 model weights are NOT bundled; the first-run wizard
  downloads the chosen model into ``~/Library/Application Support/SalesCopilot``.
- Default config, sample data, dashboard/presentation assets and the AudioTee
  binary are shipped as read-only bundle resources.
- Writable state (DB, audit ledger, logs, sessions, hub token) resolves at
  runtime to the app-support directory via ``sales_copilot.core.paths``.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

from setuptools import setup

REPO_ROOT = Path(__file__).resolve().parent
SRC_ROOT = REPO_ROOT / "src"
APP_NAME = "OnCue"
APP_SCRIPT = REPO_ROOT / "scripts" / "launcher" / "mac_app_main.py"
ICON = REPO_ROOT / "scripts" / "launcher" / "icon" / "sales-copilot.icns"


def _read_version() -> str:
    """Read the project version from ``pyproject.toml``."""
    import tomllib

    pyproject = REPO_ROOT / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return str(data["project"]["version"])


def _collect_resources() -> list[str | tuple[str, list[str]]]:
    """Return the list of resources to embed in the .app bundle.

    Only small, read-only assets are bundled. Large model weights under
    ``vendor/whisper.cpp/models/`` are deliberately omitted.
    """
    resources: list[str | tuple[str, list[str]]] = []

    def _add_dir(name: str) -> None:
        src = REPO_ROOT / name
        if src.is_dir():
            resources.append(str(src))

    # Configuration shipped as defaults; ``seed_app_support_defaults()`` copies
    # these into the writable app-support tree on first launch.
    _add_dir("config")

    # Tiny bundled sample for the "Toon me wat het doet" first-screen button.
    _add_dir("data/samples")

    # Dashboard and presentation static assets served by the embedded hub.
    _add_dir("dashboard")
    _add_dir("presentation")

    # The notetaker server the app starts by default (see scripts/launcher/mac_app_main.py).
    resources.append(("scripts", [str(REPO_ROOT / "scripts" / "record_server.py")]))

    # Prebuilt whisper.cpp and AudioTee, the programs release-macos-binaries.yml packs
    # into oncue-macos-arm64.tar.gz. The directory comes from ONCUE_BINARIES_DIR
    # (scripts/build_dmg.sh sets it). The target paths are the ones the code resolves
    # through resolve_app_resource(): bin/audiotee and vendor/whisper.cpp/build/bin/.
    binaries = os.environ.get("ONCUE_BINARIES_DIR", "")
    if binaries:
        bin_dir = Path(binaries)
        wanted = {
            "whisper-cli": "vendor/whisper.cpp/build/bin",
            "whisper-server": "vendor/whisper.cpp/build/bin",
            "audiotee": "bin",
        }
        for name, target in wanted.items():
            if not (bin_dir / name).is_file():
                raise RuntimeError(f"ONCUE_BINARIES_DIR={binaries} has no {name}")
            resources.append((target, [str(bin_dir / name)]))

    return resources


def _clean_build_artifacts() -> None:
    """Remove previous py2app build directories to avoid stale bundles."""
    for name in ("build", "dist"):
        path = REPO_ROOT / name
        if path.exists():
            shutil.rmtree(path)


# Packages that carry Mach-O files (dylibs, .so). codesign cannot reach a file inside python312.zip,
# so they ship as real directories under Contents/Resources/lib/python3.12/:
#   PIL (.dylibs), _sounddevice_data (libportaudio), llvmlite (libllvmlite): regular packages, the
#     py2app "packages" option copies them as they are.
#   mlx (libmlx, libjaccl) and google (protobuf's _upb/_message.abi3.so): namespace packages (no
#     __init__.py). py2app's own lookup fails on them ("No module named 'mlx'"), so _Py2appUnzipped
#     resolves them through importlib, and drops their modules and synthetic __init__ from the zip
#     (a regular package in the zip would hide the namespace directory on disk).
UNZIPPED_BINARY_PACKAGES = ["PIL", "mlx", "google", "_sounddevice_data", "llvmlite"]


def _package_dir(name: str) -> str:
    """The one directory of an importable (namespace) package, without importing it."""
    spec = importlib.util.find_spec(name)
    locations = list(spec.submodule_search_locations or ()) if spec is not None else []
    if len(locations) != 1:
        raise RuntimeError(f"cannot unzip {name}: expected one package directory, found {locations}")
    return os.path.realpath(locations[0])


def main() -> None:
    # Fail fast if py2app is not installed in the build environment.
    try:
        import py2app  # noqa: F401
    except ImportError as exc:  # pragma: no cover - build-time guard
        raise RuntimeError("py2app is required to build the .app bundle. Install it with: pip install py2app") from exc

    # When invoked as ``python setup_app.py py2app``, py2app is the only
    # setuptools command we care about.
    if len(sys.argv) < 2 or sys.argv[1] != "py2app":
        sys.argv.insert(1, "py2app")

    # Optional: start from a clean slate so the manifest tests never see stale
    # files. Comment this out if you want incremental builds during development.
    _clean_build_artifacts()

    from py2app.build_app import py2app as py2app_command

    class _Py2appUnzipped(py2app_command):
        """py2app that also unzips namespace packages (see UNZIPPED_* above)."""

        def get_bootstrap(self, bootstrap):
            if isinstance(bootstrap, str) and bootstrap in UNZIPPED_BINARY_PACKAGES:
                return _package_dir(bootstrap)
            return super().get_bootstrap(bootstrap)

        def create_binaries(self, py_files, pkgdirs, extensions, loader_files):
            # py2app writes a namespace package into the zip as a synthetic empty __init__ plus its
            # modules; the pkgdirs filter does not catch these. Keep them out of the zip by name.
            def zipped(mod):
                ident = getattr(mod, "identifier", "")
                return not any(ident == n or ident.startswith(f"{n}.") for n in UNZIPPED_BINARY_PACKAGES)

            return super().create_binaries([m for m in py_files if zipped(m)], pkgdirs, extensions, loader_files)

    # Make sure the package is importable during build analysis.
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))

    # modulegraph walks the AST of the large ML packages recursively and overflows
    # the default limit of 1000.
    sys.setrecursionlimit(20000)

    # py2app refuses ``install_requires``, which setuptools derives from the
    # dependencies in pyproject.toml. setuptools only reads pyproject.toml from the
    # working directory, so run setup() from an empty one and point py2app's output
    # at the repo explicitly. Every path below is absolute.
    sys.argv += ["--dist-dir", str(REPO_ROOT / "dist"), "--bdist-base", str(REPO_ROOT / "build")]
    app_script, resources, version = str(APP_SCRIPT), _collect_resources(), _read_version()
    os.chdir(tempfile.mkdtemp(prefix="oncue-py2app-"))

    setup(
        name=APP_NAME,
        version=version,
        app=[app_script],
        cmdclass={"py2app": _Py2appUnzipped},
        options={
            "py2app": {
                # Packages in "packages" are copied as real directories, not into python312.zip:
                # codesign cannot reach a Mach-O inside a zip and notarization rejects it
                # (scripts/check_zip_macho.py fails the build when one is left in the zip).
                "packages": [
                    "sales_copilot",
                    "starlette",
                    "fastapi",
                    "uvicorn",
                    "anyio",
                    "h11",
                    "websockets",
                    *UNZIPPED_BINARY_PACKAGES,
                ],
                "includes": [
                    "aiohttp",
                    "litellm",
                    "dotenv",
                    "yaml",
                    "pydantic",
                    "rich",
                    "numpy",
                    "sounddevice",
                    "silero_vad",
                    "mlx_whisper",
                    "semantic_router",
                    "sentence_transformers",
                    "instructor",
                    "aiosqlite",
                    "google.genai",
                    "groq",
                    "openai",
                    "ollama",
                ],
                "excludes": [
                    "tests",
                    "test",
                    "pytest",
                    "pip",
                    "setuptools",
                    "wheel",
                    "py2app",
                    "ruff",
                    "mypy",
                    "pip_audit",
                    "IPython",
                    "jupyter",
                    "matplotlib",
                    "tkinter",
                ],
                "resources": resources,
                "plist": {
                    "CFBundleName": APP_NAME,
                    "CFBundleShortVersionString": version,
                    "CFBundleIdentifier": "nl.vincentvandeth.oncue",
                    "LSMinimumSystemVersion": "13.0",
                    "NSMicrophoneUsageDescription": (
                        "OnCue needs microphone access to analyse your side of the sales conversation."
                    ),
                    "NSCameraUsageDescription": (
                        "OnCue does not use the camera; this key is included "
                        "to satisfy App Store review requirements when present."
                    ),
                },
                # The entry point (scripts/launcher/mac_app_main.py) reads no argv.
                "argv_emulation": False,
                "iconfile": str(ICON) if ICON.is_file() else "",
            }
        },
        setup_requires=["py2app"],
    )


if __name__ == "__main__":
    main()
