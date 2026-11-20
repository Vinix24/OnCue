"""First-start download of the speech model for the frozen OnCue.app (ADR-006 point 6).

The model is not in the .dmg. It is fetched once into Application Support, into a
``.part`` file that a later start resumes with an HTTP range request, and renamed
only when complete. This is the only outbound call the app makes, and the start
page shows it happening.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

from sales_copilot.core.paths import is_frozen_app, resolve_app_resource, resolve_app_support

MODEL_FILE = "ggml-large-v3-turbo.bin"
# Same default and override as scripts/install.sh (prebuilt_fetch_model).
DEFAULT_MODEL_URL = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{MODEL_FILE}"
# SHA-256 of the file scripts/install.sh downloads (1,624,555,275 bytes); equals the x-linked-etag
# Hugging Face reports for the resolve URL above.
DEFAULT_MODEL_SHA256 = "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
_CHUNK = 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
logger = logging.getLogger(__name__)


class ChecksumError(Exception):
    """The downloaded model does not match the expected SHA-256."""


def model_path() -> Path:
    """Where the notetaker finds the model: Application Support in the frozen app, else the vendor dir."""
    if is_frozen_app():
        return resolve_app_support() / "models" / MODEL_FILE
    return resolve_app_resource(f"vendor/whisper.cpp/models/{MODEL_FILE}")


def model_url(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return (env.get("ONCUE_MODEL_URL") or "").strip() or DEFAULT_MODEL_URL


def _fail_reason(exc: BaseException) -> tuple[str, str]:
    """A language-neutral failure code and its detail; the page text comes from the i18n catalog."""
    if isinstance(exc, ChecksumError):
        return "model_error_checksum", ""
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "model_error_disk_full", ""
    if isinstance(exc, urllib.error.HTTPError):
        return "model_error_http", str(exc.code)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "model_error_network", ""
    return "model_error_other", str(exc)


def expected_sha256(url: str, environ: dict[str, str] | None = None) -> str | None:
    """The SHA-256 the download must match, or None when there is nothing to check against.

    The default URL is pinned. A custom ``ONCUE_MODEL_URL`` is checked only against ``ONCUE_MODEL_SHA256``.
    """
    env = os.environ if environ is None else environ
    given = (env.get("ONCUE_MODEL_SHA256") or "").strip().lower()
    if given:
        if not _SHA256_RE.fullmatch(given):
            raise ValueError("ONCUE_MODEL_SHA256 must be 64 hexadecimal characters")
        return given
    if url == DEFAULT_MODEL_URL:
        return DEFAULT_MODEL_SHA256
    return None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_content_range(value: str | None) -> tuple[int, int | None] | None:
    """(first byte, total size or None when the server says ``*``) of ``bytes a-b/total``; None if malformed."""
    match = _CONTENT_RANGE_RE.fullmatch((value or "").strip())
    if not match:
        return None
    total = match.group(3)
    return int(match.group(1)), (None if total == "*" else int(total))


def _discard(part: Path, meta: Path) -> None:
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)


def download(target: Path, url: str, progress=None, timeout: float = 30.0, sha256: str | None = None) -> None:
    """Download ``url`` to ``target`` via ``target.part``, resuming from the bytes already there.

    A resume sends ``If-Range`` with the validator (ETag, else Last-Modified) saved from the first response and
    continues only on a 206 whose ``Content-Range`` starts at the end of the ``.part``; anything else restarts
    from zero. With ``sha256`` the finished file is hashed before the rename; a mismatch deletes the ``.part``.
    Raises on any failure; ``target`` exists only when complete and verified.
    """
    part = target.with_name(target.name + ".part")
    meta = target.with_name(target.name + ".part.validator")
    target.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    validator = meta.read_text(encoding="utf-8").strip() if have and meta.exists() else ""
    if have and not validator:
        # Nothing to prove the remote file is the same one: start over rather than splice two versions.
        _discard(part, meta)
        have = 0
    headers = {"Range": f"bytes={have}-", "If-Range": validator} if have else {}
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 416 or not have:
            raise
        # The .part is at least as long as the file: a stale or oversized leftover. Start over.
        _discard(part, meta)
        return download(target, url, progress, timeout, sha256)
    with response:
        resumed = have > 0 and response.status == 206
        range_total = None
        if resumed:
            parsed = _parse_content_range(response.headers.get("Content-Range"))
            if parsed is None or parsed[0] != have:
                # Wrong or missing range: the bytes do not continue our .part.
                response.close()
                _discard(part, meta)
                return download(target, url, progress, timeout, sha256)
            range_total = parsed[1]
        if resumed:
            length = int(response.headers.get("Content-Length") or 0)
            total = range_total if range_total is not None else (length + have if length else 0)
        else:
            total = int(response.headers.get("Content-Length") or 0)
            new_validator = response.headers.get("ETag") or response.headers.get("Last-Modified") or ""
            if new_validator:
                meta.write_text(new_validator, encoding="utf-8")
            else:
                meta.unlink(missing_ok=True)
        done = have if resumed else 0
        with open(part, "ab" if resumed else "wb") as out:
            while chunk := response.read(_CHUNK):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if total and done != total:
        raise OSError(f"incomplete download: {done} of {total} bytes")
    if sha256 is None:
        logger.info("model download: no SHA-256 (custom ONCUE_MODEL_URL, no ONCUE_MODEL_SHA256), check skipped")
    else:
        actual = _sha256_of(part)
        if actual != sha256.lower():
            _discard(part, meta)
            raise ChecksumError(f"SHA-256 mismatch: expected {sha256}, got {actual}")
    os.replace(part, target)
    meta.unlink(missing_ok=True)


class ModelDownloader:
    """Runs ``download`` in a thread and exposes its state to the start page."""

    def __init__(self, target: Path | None = None, url: str | None = None) -> None:
        self._target = target
        self._url = url
        self._lock = threading.Lock()
        self._state = {"state": "missing", "done": 0, "total": 0, "error": None}

    @property
    def target(self) -> Path:
        return self._target or model_path()

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state)
        if state["state"] != "downloading" and self.target.exists():
            return {"state": "ready", "done": 0, "total": 0, "error": None}
        return state

    def ensure(self) -> None:
        """Start the download unless the model is there or a download already runs."""
        if self.target.exists():
            return
        with self._lock:
            if self._state["state"] == "downloading":
                return
            self._state = {"state": "downloading", "done": 0, "total": 0, "error": None}
        threading.Thread(target=self._run, daemon=True, name="model-download").start()

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._state.update(done=done, total=total)

    def _run(self) -> None:
        try:
            url = self._url or model_url()
            download(self.target, url, self._progress, sha256=expected_sha256(url))
        except Exception as exc:  # reported to the page; the .part stays for the next try
            code, detail = _fail_reason(exc)
            with self._lock:
                self._state = {"state": "failed", "done": 0, "total": 0, "error": code, "detail": detail}
            return
        with self._lock:
            self._state = {"state": "ready", "done": 0, "total": 0, "error": None}
