"""The recording bar inside OnCue.app: layout, start with the port, stop, signing order, and the i18n start dialog."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = _load("mac_app_main_bar_under_test", ROOT / "scripts" / "launcher" / "mac_app_main.py")
embed_bar = _load("embed_bar_under_test", ROOT / "scripts" / "embed_bar.py")
sign_app = _load("sign_app_bar_under_test", ROOT / "scripts" / "sign_app.py")

MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 12


def _bar(tmp_path: Path) -> Path:
    bar = tmp_path / "src" / "OnCue Bar.app"
    exe = bar / "Contents" / "MacOS" / "OnCueBar"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(MACHO)
    exe.chmod(0o755)
    (bar / "Contents" / "Info.plist").write_text("<plist/>")
    return bar


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "OnCue.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "OnCue").write_bytes(MACHO)
    (app / "Contents" / "Resources").mkdir()
    return app


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("BAR_HOTKEY", raising=False)


# ---- layout ---------------------------------------------------------------------------------

def test_embed_places_the_bar_in_contents_helpers(tmp_path):
    app, bar = _app(tmp_path), _bar(tmp_path)
    dest = embed_bar.embed(app, bar)
    assert dest == app / "Contents" / "Helpers" / "OnCue Bar.app"
    assert (dest / "Contents" / "MacOS" / "OnCueBar").read_bytes() == MACHO
    assert launcher.bar_executable(app / "Contents" / "Resources") == dest / "Contents" / "MacOS" / "OnCueBar"


def test_embed_replaces_an_older_copy(tmp_path):
    app, bar = _app(tmp_path), _bar(tmp_path)
    old = embed_bar.bar_destination(app)
    old.mkdir(parents=True)
    (old / "stale.txt").write_text("x")
    embed_bar.embed(app, bar)
    assert not (old / "stale.txt").exists()


def test_embed_refuses_a_bar_without_executable_and_a_non_app(tmp_path):
    app = _app(tmp_path)
    empty = tmp_path / "OnCue Bar.app"
    empty.mkdir()
    with pytest.raises(ValueError, match="OnCueBar"):
        embed_bar.embed(app, empty)
    with pytest.raises(ValueError, match="not an app bundle"):
        embed_bar.embed(tmp_path / "nope.app", _bar(tmp_path))
    assert embed_bar.main(["embed_bar", str(app), str(empty)]) == 1
    assert embed_bar.main(["embed_bar"]) == 1
    assert not embed_bar.bar_destination(app).exists()


def test_build_script_embeds_before_the_bundle_gate():
    text = (ROOT / "scripts" / "build_dmg.sh").read_text()
    assert text.index("embed_bar.py") < text.index("check_bundle_gate.py")


# ---- signing order --------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_signing_puts_the_bar_before_the_outer_app_without_entitlements(tmp_path):
    app = _app(tmp_path)
    embed_bar.embed(app, _bar(tmp_path))
    ent = tmp_path / "e.entitlements"
    ent.write_text("<plist/>")
    rec = Recorder()
    order = sign_app.sign_app(app, "ID", ent, rec)
    bar = embed_bar.bar_destination(app)
    assert order[-2:] == [bar, app]
    assert bar / "Contents" / "MacOS" / "OnCueBar" not in order  # signed as part of the bundle
    bar_cmd = next(c for c in rec.cmds if c[-1] == str(bar))
    assert "--entitlements" not in bar_cmd and "runtime" in bar_cmd and "--deep" not in bar_cmd
    assert "--entitlements" in next(c for c in rec.cmds if c[-1] == str(app))


def test_dry_run_lists_the_bar_and_signs_nothing(tmp_path, capsys, monkeypatch):
    app = _app(tmp_path)
    embed_bar.embed(app, _bar(tmp_path))
    ent = tmp_path / "e.entitlements"
    ent.write_text("<plist/>")
    monkeypatch.setattr(sign_app, "run", lambda cmd: pytest.fail(f"ran {cmd}"))
    rc = sign_app.main(["sign_app", "app", str(app), "--identity", "ID", "--entitlements", str(ent), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("Contents/Helpers/OnCue Bar.app\n") < out.index("OnCue.app\n", out.index("Signing order"))
    assert "nothing signed" in out


# ---- start and stop -------------------------------------------------------------------------

class FakeProc:
    def __init__(self, running=True):
        self.running, self.terminated, self.killed = running, False, False
        self.waits: list = []

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def wait(self, timeout=None):
        self.waits.append(timeout)

    def kill(self):
        self.killed = True


@pytest.fixture()
def resources(tmp_path, monkeypatch):
    app = _app(tmp_path)
    embed_bar.embed(app, _bar(tmp_path))
    monkeypatch.setattr("sales_copilot.core.paths.resolve_app_path", lambda rel: tmp_path / "support" / rel)
    return app / "Contents" / "Resources"


def test_start_bar_passes_the_port(resources):
    calls = []
    proc = launcher.start_bar(resources, 8781, {}, popen=lambda cmd, **kw: calls.append(cmd) or FakeProc())
    assert isinstance(proc, FakeProc)
    assert calls[0][1:] == ["--port", "8781"] and calls[0][0].endswith("OnCue Bar.app/Contents/MacOS/OnCueBar")


def test_start_bar_passes_a_configured_hotkey(resources):
    calls = []
    launcher.start_bar(resources, 8780, {"BAR_HOTKEY": " ctrl+option+x "}, popen=lambda cmd, **kw: calls.append(cmd))
    assert calls[0][-2:] == ["--hotkey", "ctrl+option+x"]


def test_missing_bar_is_logged_and_not_fatal(tmp_path, capsys):
    resources = _app(tmp_path) / "Contents" / "Resources"
    assert launcher.start_bar(resources, 8780, {}, popen=lambda *a, **k: pytest.fail("started")) is None
    assert "missing or not executable" in capsys.readouterr().err


def test_launch_error_is_logged_and_not_fatal(resources, capsys):
    def boom(cmd, **kw):
        raise OSError("exec format error")

    assert launcher.start_bar(resources, 8780, {}, popen=boom) is None
    assert "exec format error" in capsys.readouterr().err


def test_stop_bar_terminates_and_never_raises(capsys):
    proc = FakeProc()
    launcher.stop_bar(proc)
    assert proc.terminated and not proc.killed
    launcher.stop_bar(None)
    launcher.stop_bar(FakeProc(running=False))

    stuck = FakeProc()
    stuck.wait = lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("bar", 5))
    launcher.stop_bar(stuck)
    assert stuck.killed

    broken = FakeProc()
    broken.terminate = lambda: (_ for _ in ()).throw(OSError("gone"))
    launcher.stop_bar(broken)
    assert "gone" in capsys.readouterr().err


# ---- i18n start dialog ----------------------------------------------------------------------

def test_dialog_is_english_by_default_and_dutch_with_language_nl(monkeypatch):
    assert launcher.labels() == ("Record and transcribe", "Live coach", "Cancel")
    assert "What do you want to do?" in launcher.dialog_script()
    monkeypatch.setenv("LANGUAGE", "nl")
    assert launcher.labels() == ("Opnemen en uitschrijven", "Live coach", "Annuleer")
    script = launcher.dialog_script()
    assert "Wat wil je doen?" in script and 'default button "Opnemen en uitschrijven"' in script


def test_dialog_escapes_every_interpolated_value(monkeypatch):
    values = {"record_label": 'Re"cord\\', "coach_label": 'Co"ach', "cancel_label": "Can\\cel",
              "dialog_text": 'Say "hi"\nback\\slash'}
    monkeypatch.setattr(launcher, "_text", lambda key, **kw: values[key])
    script = launcher.dialog_script()
    assert 'display dialog "Say \\"hi\\"\\nback\\\\slash"' in script
    assert 'buttons {"Can\\\\cel", "Co\\"ach", "Re\\"cord\\\\"}' in script
    assert 'default button "Re\\"cord\\\\"' in script


@pytest.mark.parametrize("lang,expected", [("en", "record"), ("nl", "record")])
def test_chosen_button_maps_to_the_mode_in_both_languages(monkeypatch, lang, expected):
    monkeypatch.setenv("LANGUAGE", lang)
    record, coach, _ = launcher.labels()
    for button, mode in ((record, expected), (coach, "dashboard")):
        monkeypatch.setattr(launcher.subprocess, "run",
                            lambda *a, _b=button, **k: subprocess.CompletedProcess(a, 0, _b + "\n", ""))
        assert launcher.choose_mode({}) == mode


@pytest.mark.parametrize("lang,fragment", [("en", "is in use by something other than OnCue"),
                                           ("nl", "is in gebruik door iets anders dan OnCue")])
def test_taken_port_messages_follow_the_language(monkeypatch, lang, fragment):
    monkeypatch.setenv("LANGUAGE", lang)
    said = []
    monkeypatch.setattr(launcher, "notify", said.append)
    assert launcher.handle_taken_port(8780, "other") == 1
    assert fragment in said[0] and "8780" in said[0]


@pytest.mark.parametrize("lang,fragment", [("en", "already running from the folder /x/OnCue"),
                                           ("nl", "draait al vanuit de map /x/OnCue")])
def test_running_oncue_message_names_the_folder(monkeypatch, lang, fragment):
    monkeypatch.setenv("LANGUAGE", lang)
    said, opened = [], []
    monkeypatch.setattr(launcher, "notify", said.append)
    monkeypatch.setattr(launcher, "_running_folder", lambda pid: "/x/OnCue")
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.handle_taken_port(8780, {"phase": "idle", "pid": 1}) == 1
    assert fragment in said[0] and opened == ["http://127.0.0.1:8780"]


def test_notify_escapes_the_message_and_reports_success(monkeypatch):
    seen = []
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b""))
    assert launcher.notify('say "x"\\') is True
    assert 'display dialog "say \\"x\\"\\\\"' in seen[0][2]


def test_notify_launch_error_returns_false_and_handle_taken_port_still_opens(monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(launcher.subprocess, "run", boom)
    assert launcher.notify("hello") is False
    assert "could not show a dialog" in capsys.readouterr().err
    opened = []
    monkeypatch.setattr(launcher, "_running_folder", lambda pid: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.handle_taken_port(8780, {"phase": "idle", "pid": 1}) == 1
    assert opened == ["http://127.0.0.1:8780"]


def test_notify_nonzero_exit_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"no window server"))
    assert launcher.notify("hello") is False
    assert "no window server" in capsys.readouterr().err


def test_running_folder_survives_a_missing_lsof(monkeypatch):
    def boom(*a, **k):
        raise OSError("no lsof")

    monkeypatch.setattr(launcher.subprocess, "run", boom)
    assert launcher._running_folder(1) is None


def test_app_catalog_keys_match_in_both_languages():
    cats = {lang: yaml.safe_load((ROOT / "config" / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))["app"]
            for lang in ("en", "nl")}
    assert set(cats["en"]) == set(cats["nl"])
    assert "{record}" in cats["en"]["dialog_text"] and "{record}" in cats["nl"]["dialog_text"]


def test_sigterm_handler_stops_the_bar_then_exits(monkeypatch):
    import signal

    handlers = {}
    monkeypatch.setattr(launcher.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    proc = FakeProc()
    launcher.stop_bar_on_sigterm(proc)
    with pytest.raises(SystemExit) as exit_info:
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert proc.terminated and exit_info.value.code == 128 + signal.SIGTERM


def test_dialog_lookups_restore_the_catalogue_override(tmp_path, monkeypatch):
    from sales_copilot.core import i18n

    support = tmp_path / "support"
    support.mkdir()
    (support / "en.yaml").write_text("probe:\n  edited: from app support\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_CATALOG_DIR_OVERRIDE", None)
    before = i18n._catalog_dir()
    launcher.labels()
    launcher.dialog_text()
    assert i18n._CATALOG_DIR_OVERRIDE is None
    assert i18n._catalog_dir() == before

    i18n.set_catalog_dir(support)
    launcher.labels()
    assert i18n._catalog_dir() == support
    assert i18n.t("probe.edited") == "from app support"
    i18n.set_catalog_dir(None)


def test_catalog_dir_context_restores_after_an_error(tmp_path):
    from sales_copilot.core import i18n

    with pytest.raises(RuntimeError):
        with i18n.catalog_dir(tmp_path):
            assert i18n._catalog_dir() == tmp_path
            raise RuntimeError("boom")
    assert i18n._CATALOG_DIR_OVERRIDE is None
