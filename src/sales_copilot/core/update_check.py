"""On-demand "Check for updates" for OnCue (ADR-006 point 7).

OnCue never checks by itself. Nothing in this module runs at import time, on a
timer or when a page loads: the single outbound request happens when a person
clicks the button in the library page, and it goes to GitHub only -- no cookies,
no machine or user data, no query string.

The request is sent with the standard library's ``urllib.request``. It carries
exactly two headers: ``Accept: application/vnd.github+json`` and
``User-Agent: OnCue/<installed version>``. A five-second timeout bounds the
call. Tests inject ``fetch`` so they never touch the network.

Failures (no network, a timeout, an HTTP error such as 403/429 rate limiting or
404 for a repository without a release, malformed JSON) return a translated
message in the result instead of raising into the server.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import total_ordering

from sales_copilot import __version__
from sales_copilot.core import i18n
from sales_copilot.core.paths import is_frozen_app, resolve_app_support

#: The only endpoint OnCue contacts, and only when the user asks it to.
RELEASES_LATEST_URL = "https://api.github.com/repos/Vinix24/OnCue/releases/latest"
#: No query string, no cookies: this is the whole request surface.
REQUEST_TIMEOUT_SECONDS = 5.0

#: The exact one-line install from README.md, run again to update that route.
ONE_LINE_INSTALL_COMMAND = (
    '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)"'
)
#: The steps under INSTALL.md "## Update" for a git checkout (Path 2).
GIT_CHECKOUT_UPDATE_COMMANDS = ("git pull", "bash scripts/install.sh")

ROUTE_APP = "app"
ROUTE_ONE_LINE_INSTALL = "one_line_install"
ROUTE_GIT_CHECKOUT = "git_checkout"

#: ``fetch(url, headers, timeout)`` -> response body. Injectable for tests.
Fetch = Callable[[str, dict[str, str], float], bytes]

_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


@total_ordering
@dataclass(frozen=True)
class Version:
    """An ``X.Y.Z`` version with an optional pre-release (``1.0.0-rc.6``).

    A pre-release sorts before the release with the same ``X.Y.Z``. Pre-release
    identifiers follow SemVer precedence: numeric identifiers compare as
    numbers, alphanumeric ones lexically, numeric before alphanumeric, and a
    shorter identifier list before a longer one.
    """

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    def _sort_key(self) -> tuple[object, ...]:
        identifiers = tuple((0, int(part)) if part.isdigit() else (1, part) for part in self.prerelease)
        # The zero marks a pre-release; a final release (empty tuple, marker 1) sorts after it.
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, identifiers)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base


def parse_version(text: str) -> Version:
    """Parse ``v1.0.0`` / ``1.0.1`` / ``1.0.0-rc.6``; raise ``ValueError`` otherwise."""
    match = _VERSION_RE.match((text or "").strip())
    if match is None:
        raise ValueError(f"not an X.Y.Z version: {text!r}")
    major, minor, patch, prerelease = match.groups()
    return Version(
        major=int(major),
        minor=int(minor),
        patch=int(patch),
        prerelease=tuple(prerelease.split(".")) if prerelease else (),
    )


def request_headers(version: str) -> dict[str, str]:
    """The two headers the request carries, and nothing else."""
    return {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"OnCue/{version}",
    }


def _http_get(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """Default fetch: a plain GET with the given headers and timeout."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        if status != 200:
            # Fail closed: anything but a 200 is an error even if urlopen returned it.
            raise urllib.error.HTTPError(url, status or 0, "unexpected HTTP status", None, None)
        return response.read()


def install_route() -> str:
    """Which install this process runs from, for the update instructions.

    - frozen py2app ``.app``: download the new ``.dmg`` from the release page.
    - git checkout: ``git pull`` plus the INSTALL.md "## Update" steps.
    - anything else (the one-line install's release tree in ``~/OnCue``): run
      the README install line again. A plain pipx install also lands here; its
      own update command is out of scope for this route.
    """
    if is_frozen_app():
        return ROUTE_APP
    checkout = resolve_app_support()
    if (checkout / "pyproject.toml").is_file() and (checkout / ".git").exists():
        return ROUTE_GIT_CHECKOUT
    return ROUTE_ONE_LINE_INSTALL


def _action(route: str, latest: str, release_url: str) -> dict[str, object]:
    if route == ROUTE_APP:
        return {
            "type": "release_page",
            "url": release_url,
            "download": f"OnCue-{latest}-arm64.dmg",
        }
    if route == ROUTE_GIT_CHECKOUT:
        return {"type": "steps", "commands": list(GIT_CHECKOUT_UPDATE_COMMANDS)}
    return {"type": "command", "commands": [ONE_LINE_INSTALL_COMMAND]}


def _error_result(route: str, code: str, lang: str | None) -> dict[str, object]:
    return {
        "current_version": __version__,
        "latest_version": None,
        # Unknown is not "up to date": the page shows the error, never a success line.
        "update_available": None,
        "release_url": None,
        "install_route": route,
        "action": None,
        "error": {"code": code, "message": i18n.t(f"update.{code}", lang=lang)},
    }


def check_for_updates(*, fetch: Fetch | None = None, lang: str | None = None) -> dict[str, object]:
    """Ask GitHub for the latest release once and describe what to do.

    Returns a JSON-ready mapping. It never raises: every network, HTTP and
    payload failure becomes ``error`` with a translated ``message``.
    """
    route = install_route()

    try:
        current = parse_version(__version__)
    except ValueError:
        return _error_result(route, "error_invalid_version", lang)

    fetch_fn = fetch or _http_get
    try:
        body = fetch_fn(
            RELEASES_LATEST_URL,
            request_headers(__version__),
            REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _error_result(route, "error_no_release", lang)
        if exc.code in (403, 429):
            return _error_result(route, "error_rate_limited", lang)
        return _error_result(route, "error_http", lang)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return _error_result(route, "error_network", lang)

    try:
        release = json.loads(body)
        if not isinstance(release, dict):
            raise ValueError("release payload is not an object")
        tag = release["tag_name"]
        release_url = release["html_url"]
        if not isinstance(tag, str) or not isinstance(release_url, str):
            raise ValueError("release tag or URL is not a string")
        if not tag.strip() or not release_url.strip():
            raise ValueError("release tag or URL is empty")
        latest = parse_version(tag)
    except (ValueError, TypeError, KeyError):
        return _error_result(route, "error_malformed", lang)

    latest_version = str(latest)
    return {
        "current_version": __version__,
        "latest_version": latest_version,
        "update_available": latest > current,
        "release_url": release_url,
        "install_route": route,
        "action": _action(route, latest_version, release_url),
        "error": None,
    }
