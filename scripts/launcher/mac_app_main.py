#!/usr/bin/env python3
"""Entry point of the frozen OnCue.app (py2app).

Mirrors the rc.6 launcher (scripts/launcher/build_app.sh + oncue-launch.sh): a
dialog asks what to do ("Record and transcribe", the notetaker, is the default
button; "Live coach" starts the dashboard). Its texts come from the i18n catalogue.
It never starts the hub directly. The notetaker also starts the recording bar embedded in the
bundle (``Contents/Helpers/OnCue Bar.app``) and stops it when OnCue quits.

``ONCUE_APP_MODE=record|dashboard`` skips the dialog (smoke test, scripted start).
A cancelled dialog exits without starting anything.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _text(key: str, **values: object) -> str:
    """A message of the ``app`` catalogue section, in the resolved language (``LANGUAGE``, English by default)."""
    from sales_copilot.core import i18n
    from sales_copilot.core.paths import resolve_app_resource

    # The bundled catalogue is read directly: the start dialog appears before the app-support tree is seeded.
    # The override is restored afterwards, so the record server or dashboard that run() starts in this
    # process still resolves the app-support catalogue the user may have edited.
    with i18n.catalog_dir(resolve_app_resource("config/i18n")):
        return i18n.t(f"app.{key}", **values)


def labels() -> tuple[str, str, str]:
    """``(record, coach, cancel)`` button labels in the resolved language."""
    return _text("record_label"), _text("coach_label"), _text("cancel_label")


def dialog_text() -> str:
    record, coach, _cancel = labels()
    return _text("dialog_text", record=record, coach=coach)


def applescript_escape(text: str) -> str:
    """Escape ``\\`` and ``"`` so ``text`` sits safely inside an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def dialog_script() -> str:
    """The AppleScript for the start dialog; every interpolated value is escaped."""
    text = applescript_escape(dialog_text()).replace("\n", "\\n")
    record, coach, cancel = (applescript_escape(label) for label in labels())
    return (
        f'button returned of (display dialog "{text}" '
        f'buttons {{"{cancel}", "{coach}", "{record}"}} '
        f'default button "{record}" with title "OnCue")'
    )


def choose_mode(environ: dict[str, str] | None = None) -> str | None:
    """Return ``record``, ``dashboard`` or ``None`` (cancelled). An invalid override is an error."""
    env = os.environ if environ is None else environ
    forced = env.get("ONCUE_APP_MODE", "").strip()
    if forced:
        if forced not in ("record", "dashboard"):
            raise SystemExit(f"ONCUE_APP_MODE must be 'record' or 'dashboard', got {forced!r}")
        return forced
    script = dialog_script()
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # -128 is the user cancelling the dialog; anything else is a real failure.
        if "-128" in proc.stderr:
            return None
        raise SystemExit(f"OnCue could not show its start dialog: {proc.stderr.strip()}")
    record, coach, _cancel = labels()
    return {record: "record", coach: "dashboard"}.get(proc.stdout.strip())


def probe_port(port: int, timeout: float = 1.0) -> dict | str | None:
    """None when 127.0.0.1:port is free, the ``/status`` JSON when an OnCue answers, else ``"other"``."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
    except OSError:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return "other"
    # The notetaker's /status carries these keys; a foreign service on the port does not.
    if isinstance(body, dict) and "phase" in body and "pid" in body:
        return body
    return "other"


def notify(message: str) -> bool:
    """Show a macOS dialog; never raises. The message is also printed.

    Returns True when the dialog was shown. A launch error or a non-zero exit of ``osascript``
    is printed to stderr and returns False, so the caller carries on either way.
    """
    print(message)
    text = applescript_escape(message)
    ok = applescript_escape(_text("ok_label"))
    script = f'display dialog "{text}" buttons {{"{ok}"}} default button "{ok}" with title "OnCue"'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, check=False)
    except OSError as exc:
        print(f"OnCue could not show a dialog: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"OnCue could not show a dialog (osascript exit {proc.returncode}): {detail}", file=sys.stderr)
        return False
    return True


def _running_folder(pid: int) -> str | None:
    """Working directory of the running OnCue (its install folder), via lsof; None when unknown."""
    cmd = ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return None  # the folder is only extra information for the message
    for line in proc.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def handle_taken_port(port: int, found: dict | str) -> int:
    """Port taken: open the running OnCue and say where it runs, or say it is not OnCue. Starts nothing."""
    if isinstance(found, dict):
        folder = _running_folder(int(found["pid"])) if isinstance(found.get("pid"), int) else None
        notify(_text("already_running_from", folder=folder) if folder else _text("already_running"))
        webbrowser.open(f"http://127.0.0.1:{port}")
    else:
        notify(_text("port_in_use", port=port))
    return 1


def _open_when_ready(url: str, probe: str) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=1):
                break
        except OSError:
            time.sleep(0.5)
    else:
        return
    webbrowser.open(url)


BAR_EXECUTABLE = ("Helpers", "OnCue Bar.app", "Contents", "MacOS", "OnCueBar")


def bar_executable(resources: Path) -> Path:
    """Where the embedded recording bar lives: ``Contents/Helpers`` next to ``Contents/Resources``."""
    return resources.parent.joinpath(*BAR_EXECUTABLE)


def start_bar(resources: Path, port: int, environ: dict[str, str] | None = None, popen=subprocess.Popen):
    """Start the embedded recording bar with the notetaker's port; ``None`` when it cannot start.

    The bar is optional: a missing executable or a launch error is logged and the notetaker
    carries on. The hotkey is passed only when ``BAR_HOTKEY`` is set.
    """
    from sales_copilot.core.paths import resolve_app_path

    env = os.environ if environ is None else environ
    exe = bar_executable(resources)
    if not exe.is_file() or not os.access(exe, os.X_OK):
        print(f"OnCue bar not started: {exe} is missing or not executable", file=sys.stderr)
        return None
    cmd = [str(exe), "--port", str(port)]
    hotkey = env.get("BAR_HOTKEY", "").strip()
    if hotkey:
        cmd += ["--hotkey", hotkey]
    try:
        log_dir = resolve_app_path("data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "launcher-bar.log").open("ab") as log:
            return popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"OnCue bar not started: {exc}", file=sys.stderr)
        return None


def stop_bar(proc) -> None:
    """Stop the bar started by ``start_bar``; never raises. Kills it when it ignores SIGTERM for 5 s."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    except OSError as exc:
        print(f"OnCue bar could not be stopped: {exc}", file=sys.stderr)


def stop_bar_on_sigterm(proc) -> None:
    """Stop the bar when the app gets SIGTERM.

    uvicorn restores the previous handler after its graceful shutdown and re-raises the signal,
    which would end the process before ``run`` can clean up; this handler is the previous one.
    """

    def handler(signum, _frame):
        stop_bar(proc)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def _load_record_server(resources: Path):
    """Import scripts/record_server.py from the bundle and point it at writable/bundled paths."""
    from sales_copilot.core.paths import resolve_app_path, resolve_app_resource

    spec = importlib.util.spec_from_file_location("record_server", resources / "scripts" / "record_server.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"record_server.py not found in the bundle at {resources}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_server"] = module
    spec.loader.exec_module(module)
    module.OUT_DIR = str(resolve_app_path("data/sessions"))
    module.AUDIOTEE_PATH = str(resolve_app_resource("bin/audiotee"))
    return module


def run(mode: str) -> int:
    from sales_copilot.core.paths import ensure_app_support_tree, resolve_app_resource, seed_app_support_defaults

    ensure_app_support_tree()
    seed_app_support_defaults()
    if mode == "dashboard":
        from sales_copilot.__main__ import main

        main()
        return 0
    module = _load_record_server(resolve_app_resource("."))
    port = module._default_port()
    found = probe_port(port)
    if found is not None:
        return handle_taken_port(port, found)
    from sales_copilot.core.model_download import ModelDownloader

    module.MODEL_DOWNLOADER = ModelDownloader()
    module.MODEL_DOWNLOADER.ensure()
    if not os.environ.get("SALES_COPILOT_NO_BROWSER"):
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=_open_when_ready, args=(url, f"{url}/status"), daemon=True).start()
    sys.argv = ["record_server", "--port", str(port)]
    bar = start_bar(resolve_app_resource("."), port)
    stop_bar_on_sigterm(bar)
    try:
        return int(module.main())
    finally:
        stop_bar(bar)


def main() -> int:
    mode = choose_mode()
    if mode is None:
        return 0
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
