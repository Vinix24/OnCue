"""First-start model download (resume, rename on completion, failures) and the app's port check."""

from __future__ import annotations

import errno
import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

from sales_copilot.core import model_download as md

_LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "launcher" / "mac_app_main.py"
_spec = importlib.util.spec_from_file_location("mac_app_main_under_test", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, content_range: str | None = None, etag: str | None = None,
                 length: bool = True) -> None:
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body))} if length else {}
        if content_range:
            self.headers["Content-Range"] = content_range
        if etag:
            self.headers["ETag"] = etag

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, handler):
    seen = []

    def fake(request, timeout=None):
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(md.urllib.request, "urlopen", fake)
    return seen


def test_fresh_download_renames_only_when_complete(tmp_path, monkeypatch) -> None:
    target = tmp_path / "models" / "m.bin"
    seen = _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef"))
    calls = []
    md.download(target, "http://x/m.bin", lambda d, t: calls.append((d, t)))
    assert target.read_bytes() == b"abcdef"
    assert not target.with_name("m.bin.part").exists()
    assert "Range" not in seen[0].headers and calls[-1] == (6, 6)


def test_part_file_resumes_with_range_request(tmp_path, monkeypatch) -> None:
    target = tmp_path / "m.bin"
    (tmp_path / "m.bin.part").write_bytes(b"abc")
    (tmp_path / "m.bin.part.validator").write_text('"etag1"')
    seen = _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"def", status=206, content_range="bytes 3-5/6"))
    md.download(target, "http://x/m.bin")
    assert seen[0].get_header("Range") == "bytes=3-"
    assert seen[0].get_header("If-range") == '"etag1"'
    assert target.read_bytes() == b"abcdef"


def test_server_ignoring_range_restarts_instead_of_corrupting(tmp_path, monkeypatch) -> None:
    (tmp_path / "m.bin.part").write_bytes(b"abc")
    (tmp_path / "m.bin.part.validator").write_text('"etag1"')
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef", status=200))
    md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef"


def test_interrupted_download_keeps_part_and_never_renames(tmp_path, monkeypatch) -> None:
    class Short(FakeResponse):
        def __init__(self) -> None:
            super().__init__(b"abc")
            self.headers = {"Content-Length": "10"}

    _patch_urlopen(monkeypatch, lambda r: Short())
    with pytest.raises(OSError, match="incomplete download"):
        md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert not (tmp_path / "m.bin").exists()
    assert (tmp_path / "m.bin.part").read_bytes() == b"abc"


def test_range_not_satisfiable_starts_over(tmp_path, monkeypatch) -> None:
    (tmp_path / "m.bin.part").write_bytes(b"too-long-leftover")
    (tmp_path / "m.bin.part.validator").write_text('"etag1"')

    def handler(request):
        if request.get_header("Range"):
            raise urllib.error.HTTPError("u", 416, "bad range", {}, None)
        return FakeResponse(b"abcdef")

    _patch_urlopen(monkeypatch, handler)
    md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef"


def test_disk_full_and_network_failures_are_reported_with_retry(tmp_path, monkeypatch) -> None:
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(md, "download", full)
    dl = md.ModelDownloader(target=tmp_path / "m.bin", url="http://x")
    dl._run()
    status = dl.status()
    assert status["state"] == "failed" and status["error"] == "model_error_disk_full"
    monkeypatch.setattr(md, "download", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("down")))
    dl._run()
    assert dl.status()["error"] == "model_error_network"


def test_status_ready_once_the_model_exists(tmp_path) -> None:
    (tmp_path / "m.bin").write_bytes(b"x")
    assert md.ModelDownloader(target=tmp_path / "m.bin").status()["state"] == "ready"
    assert md.ModelDownloader(target=tmp_path / "none.bin").status()["state"] == "missing"


def test_model_url_override() -> None:
    assert md.model_url({}) == md.DEFAULT_MODEL_URL
    assert md.model_url({"ONCUE_MODEL_URL": "http://mirror/m.bin"}) == "http://mirror/m.bin"


def test_model_path_frozen_vs_dev(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(md, "is_frozen_app", lambda: True)
    monkeypatch.setattr(md, "resolve_app_support", lambda: tmp_path)
    assert md.model_path() == tmp_path / "models" / md.MODEL_FILE
    monkeypatch.setattr(md, "is_frozen_app", lambda: False)
    monkeypatch.setattr(md, "resolve_app_resource", lambda rel: tmp_path / "repo" / rel)
    assert md.model_path() == tmp_path / "repo" / "vendor/whisper.cpp/models" / md.MODEL_FILE


def test_port_free(monkeypatch) -> None:
    def refuse(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(launcher.socket, "create_connection", refuse)
    assert launcher.probe_port(8780) is None


def _listening(monkeypatch, body: bytes | Exception) -> None:
    monkeypatch.setattr(launcher.socket, "create_connection", lambda *a, **k: io.BytesIO())

    def urlopen(url, timeout=None):
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr(launcher.urllib.request, "urlopen", urlopen)


def test_port_taken_by_oncue_opens_it_and_says_where(monkeypatch) -> None:
    _listening(monkeypatch, b'{"phase": "idle", "pid": 4242}')
    found = launcher.probe_port(8780)
    assert found == {"phase": "idle", "pid": 4242}
    said, opened = [], []
    monkeypatch.setattr(launcher, "notify", said.append)
    monkeypatch.setattr(launcher, "_running_folder", lambda pid: "/Users/x/OnCue")
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.handle_taken_port(8780, found) == 1
    assert "/Users/x/OnCue" in said[0] and opened == ["http://127.0.0.1:8780"]


@pytest.mark.parametrize("body", [b"<html>nope</html>", b'{"hello": 1}', b"[1]", OSError("refused")])
def test_port_taken_by_something_else_starts_nothing(monkeypatch, body) -> None:
    monkeypatch.delenv("LANGUAGE", raising=False)
    _listening(monkeypatch, body)
    found = launcher.probe_port(8780)
    assert found == "other"
    said, opened = [], []
    monkeypatch.setattr(launcher, "notify", said.append)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.handle_taken_port(8780, found) == 1
    assert "something other than OnCue" in said[0] and opened == []


def test_applescript_escape_handles_quote_and_backslash() -> None:
    assert launcher.applescript_escape('a"b\\c') == 'a\\"b\\\\c'


def test_model_texts_exist_in_both_languages() -> None:
    import yaml

    root = Path(__file__).resolve().parents[1]
    keys = {
        "model_retry", "model_progress", "model_progress_unknown", "model_preparing", "model_failed",
        "model_downloading_block", "model_error_disk_full", "model_error_http", "model_error_network",
        "model_error_other", "model_error_checksum",
    }
    for lang in ("en", "nl"):
        recorder = yaml.safe_load((root / "config" / "i18n" / f"{lang}.yaml").read_text(encoding="utf-8"))["recorder"]
        assert keys <= set(recorder), lang
    en = yaml.safe_load((root / "config" / "i18n" / "en.yaml").read_text(encoding="utf-8"))["recorder"]
    nl = yaml.safe_load((root / "config" / "i18n" / "nl.yaml").read_text(encoding="utf-8"))["recorder"]
    assert en["model_retry"] != nl["model_retry"]
    assert "{percent}" in en["model_progress"] and "{percent}" in nl["model_progress"]


def _part(tmp_path, validator: str | None = '"v1"') -> None:
    (tmp_path / "m.bin.part").write_bytes(b"abc")
    if validator is not None:
        (tmp_path / "m.bin.part.validator").write_text(validator)


def test_wrong_content_range_restarts_from_zero(tmp_path, monkeypatch) -> None:
    _part(tmp_path)

    def handler(request):
        if request.get_header("Range"):
            return FakeResponse(b"XYZ", status=206, content_range="bytes 0-2/6")
        return FakeResponse(b"abcdef")

    seen = _patch_urlopen(monkeypatch, handler)
    md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef" and len(seen) == 2


def test_missing_content_range_on_206_restarts(tmp_path, monkeypatch) -> None:
    _part(tmp_path)
    handler = lambda r: FakeResponse(b"def", status=206) if r.get_header("Range") else FakeResponse(b"abcdef")  # noqa: E731
    _patch_urlopen(monkeypatch, handler)
    md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef"


def test_part_without_validator_is_not_resumed(tmp_path, monkeypatch) -> None:
    _part(tmp_path, validator=None)
    seen = _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef"))
    md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert not seen[0].has_header("Range") and (tmp_path / "m.bin").read_bytes() == b"abcdef"


def test_first_response_validator_is_stored_and_used_on_resume(tmp_path, monkeypatch) -> None:
    class Cut(FakeResponse):
        def read(self, n=-1):
            raise ConnectionResetError("cut")

    _patch_urlopen(monkeypatch, lambda r: Cut(b"abc", etag='"v9"'))
    with pytest.raises(ConnectionResetError):
        md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert (tmp_path / "m.bin.part.validator").read_text() == '"v9"'


def test_206_without_content_length_takes_total_from_content_range(tmp_path, monkeypatch) -> None:
    _part(tmp_path)
    calls = []
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"def", status=206, content_range="bytes 3-5/6", length=False))
    md.download(tmp_path / "m.bin", "http://x/m.bin", lambda d, t: calls.append((d, t)))
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef" and calls[-1] == (6, 6)


def test_206_short_body_without_content_length_is_incomplete(tmp_path, monkeypatch) -> None:
    _part(tmp_path)
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"de", status=206, content_range="bytes 3-5/6", length=False))
    with pytest.raises(OSError, match="incomplete download"):
        md.download(tmp_path / "m.bin", "http://x/m.bin")
    assert not (tmp_path / "m.bin").exists()


def test_matching_sha256_renames(tmp_path, monkeypatch) -> None:
    import hashlib

    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef"))
    md.download(tmp_path / "m.bin", "http://x/m.bin", sha256=hashlib.sha256(b"abcdef").hexdigest())
    assert (tmp_path / "m.bin").read_bytes() == b"abcdef"
    assert not (tmp_path / "m.bin.part").exists() and not (tmp_path / "m.bin.part.validator").exists()


def test_hash_mismatch_fails_and_cleans_up(tmp_path, monkeypatch) -> None:
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef", etag='"v1"'))
    with pytest.raises(md.ChecksumError):
        md.download(tmp_path / "m.bin", "http://x/m.bin", sha256="0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_hash_mismatch_is_reported_to_the_page_and_retry_starts_fresh(tmp_path, monkeypatch) -> None:
    served = iter([b"badbad", b"abcdef"])
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(next(served)))
    import hashlib

    monkeypatch.setenv("ONCUE_MODEL_URL", "http://x/m.bin")
    monkeypatch.setenv("ONCUE_MODEL_SHA256", hashlib.sha256(b"abcdef").hexdigest())
    dl = md.ModelDownloader(target=tmp_path / "m.bin")
    dl._run()
    assert dl.status()["state"] == "failed" and dl.status()["error"] == "model_error_checksum"
    dl._run()
    assert dl.status()["state"] == "ready" and (tmp_path / "m.bin").read_bytes() == b"abcdef"


def test_override_without_hash_skips_with_log_line(tmp_path, monkeypatch, caplog) -> None:
    assert md.expected_sha256("http://mirror/m.bin", {}) is None
    _patch_urlopen(monkeypatch, lambda r: FakeResponse(b"abcdef"))
    with caplog.at_level("INFO", logger=md.logger.name):
        md.download(tmp_path / "m.bin", "http://mirror/m.bin", sha256=None)
    assert "skipped" in caplog.text and (tmp_path / "m.bin").exists()


def test_expected_sha256_default_pin_and_override() -> None:
    assert md.expected_sha256(md.DEFAULT_MODEL_URL, {}) == md.DEFAULT_MODEL_SHA256
    assert md.expected_sha256("http://m/x", {"ONCUE_MODEL_SHA256": " " + "AB" * 32 + " "}) == "ab" * 32
    with pytest.raises(ValueError):
        md.expected_sha256("http://m/x", {"ONCUE_MODEL_SHA256": "nothex"})
