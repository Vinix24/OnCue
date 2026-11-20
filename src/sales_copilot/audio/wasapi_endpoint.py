"""Pick the Windows render endpoint where a call plays (ADR-008 points 1, 2, 4).

COM access is plain ``ctypes`` (no comtypes, no pycaw). Every COM failure
degrades to "no information" (``None`` / empty), logged once, and never raises
into the reader thread. The selection itself (:func:`select_endpoint`) is pure
so it is testable without Windows.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sales_copilot.core.paths import resolve_app_resource

logger = logging.getLogger(__name__)

_APPS_RELATIVE = Path("config") / "wasapi_call_apps.yaml"

REASON_COMMUNICATIONS = "communications_default"
REASON_CONSOLE = "console_default"
REASON_TIE = "tie_kept_current"
REASON_TIE_FIRST = "tie_first_sorted"


def load_call_apps(path: Path | None = None) -> frozenset[str]:
    """Return the lower-cased call-app executable names from the YAML file.

    Raises ValueError/OSError on a missing or malformed file: the caller
    decides (the stream treats it as "no app list" and logs once).
    """

    import yaml

    # Bundled read-only resource: the repo root in a checkout, ``_resources/`` in an
    # installed wheel, ``Contents/Resources`` in the frozen app.
    target = path or resolve_app_resource(_APPS_RELATIVE)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    apps = data.get("call_apps") if isinstance(data, dict) else None
    if not isinstance(apps, list) or not all(isinstance(a, str) and a.strip() for a in apps):
        raise ValueError(f"{target}: 'call_apps' must be a list of executable names")
    return frozenset(a.strip().lower() for a in apps)


@dataclass(frozen=True)
class EndpointSnapshot:
    """What COM reported: per endpoint id the executables with an active session."""

    sessions: dict[str, frozenset[str]] = field(default_factory=dict)
    communications_id: str | None = None
    console_id: str | None = None


def select_endpoint(
    sessions: dict[str, frozenset[str]],
    communications_id: str | None,
    console_id: str | None,
    current_id: str | None,
    call_apps: frozenset[str],
) -> tuple[str | None, str]:
    """ADR-008 order: active call-app session, communications default, console default.

    Tie (several endpoints with an active call-app session): the communications
    default if it is one of them, otherwise keep the current endpoint if it is
    one of them. With no usable current endpoint the sorted-first candidate is
    taken so the choice is deterministic (reason ``tie_first_sorted``, logged). Returns ``(endpoint_id, reason)``;
    ``endpoint_id`` is None only when nothing is known at all.
    """

    apps = {a.lower() for a in call_apps}
    active = {
        eid: sorted(e for e in exes if e.lower() in apps)
        for eid, exes in sessions.items()
        if any(e.lower() in apps for e in exes)
    }
    if len(active) == 1:
        eid, exes = next(iter(active.items()))
        return eid, f"active_session:{exes[0]}"
    if len(active) > 1:
        if communications_id in active:
            return communications_id, f"active_session:{active[communications_id][0]}"
        if current_id in active:
            return current_id, REASON_TIE
        logger.info(
            "WASAPI endpoint tie among %d active endpoints without a usable current one; taking the first sorted.",
            len(active),
        )
        return sorted(active)[0], REASON_TIE_FIRST
    if communications_id:
        return communications_id, REASON_COMMUNICATIONS
    if console_id:
        return console_id, REASON_CONSOLE
    return None, "no_endpoint"


# --- COM via ctypes (Windows only) -------------------------------------------

_E_RENDER = 0
_E_CONSOLE = 0
_E_COMMUNICATIONS = 2
_DEVICE_STATE_ACTIVE = 1
_CLSCTX_ALL = 0x17
_SESSION_STATE_ACTIVE = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_CLSID_ENUMERATOR = uuid.UUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_ENUMERATOR = uuid.UUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_SESSION_MANAGER2 = uuid.UUID("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
_IID_SESSION_CONTROL2 = uuid.UUID("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")

_warned: set[str] = set()


def _warn_once(key: str, exc: BaseException | None = None) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning("WASAPI endpoint follow: %s; no endpoint information.", key, exc_info=exc)


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, value: uuid.UUID) -> _GUID:
        g = cls()
        g.d1, g.d2, g.d3 = value.fields[0], value.fields[1], value.fields[2]
        g.d4[:] = value.bytes[8:]
        return g


def _ole32() -> Any | None:
    windll = getattr(ctypes, "windll", None)
    return getattr(windll, "ole32", None) if windll is not None else None


def _call(ptr: Any, index: int, *args: Any, argtypes: tuple[Any, ...] = ()) -> int:
    """Call vtable slot ``index`` of COM pointer ``ptr`` (a c_void_p); return the HRESULT.

    ``argtypes`` lists the ctypes types of ``args`` (``this`` is added here).
    """

    vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    return int(proto(vtable[index])(ptr, *args))


def _check(hr: int) -> None:
    if hr < 0:
        raise OSError(f"COM call failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


def _release(ptr: ctypes.c_void_p | None) -> None:
    if ptr is not None and ptr.value:
        try:
            _call(ptr, 2)
        except Exception:
            logger.debug("COM Release failed", exc_info=True)


def _device_id(device: ctypes.c_void_p) -> str:
    out = ctypes.c_wchar_p()
    _check(_call(device, 5, ctypes.byref(out), argtypes=(ctypes.c_void_p,)))
    value = out.value or ""
    ctypes.windll.ole32.CoTaskMemFree(out)  # type: ignore[attr-defined]
    return value


def _exe_name(pid: int) -> str | None:
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def _active_executables(device: ctypes.c_void_p) -> frozenset[str]:
    manager = ctypes.c_void_p()
    iid = _GUID.of(_IID_SESSION_MANAGER2)
    _check(
        _call(
            device,
            3,
            ctypes.byref(iid),
            _CLSCTX_ALL,
            None,
            ctypes.byref(manager),
            argtypes=(ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p),
        )
    )
    enumerator = ctypes.c_void_p()
    names: set[str] = set()
    try:
        _check(_call(manager, 5, ctypes.byref(enumerator), argtypes=(ctypes.c_void_p,)))
        count = ctypes.c_int()
        _check(_call(enumerator, 3, ctypes.byref(count), argtypes=(ctypes.c_void_p,)))
        for i in range(count.value):
            control, control2 = ctypes.c_void_p(), ctypes.c_void_p()
            try:
                _check(_call(enumerator, 4, i, ctypes.byref(control), argtypes=(ctypes.c_int, ctypes.c_void_p)))
                iid2 = _GUID.of(_IID_SESSION_CONTROL2)
                _check(
                    _call(
                        control,
                        0,
                        ctypes.byref(iid2),
                        ctypes.byref(control2),
                        argtypes=(ctypes.c_void_p, ctypes.c_void_p),
                    )
                )
                state = ctypes.c_int()
                _check(_call(control2, 3, ctypes.byref(state), argtypes=(ctypes.c_void_p,)))
                if state.value != _SESSION_STATE_ACTIVE:
                    continue
                pid = ctypes.c_uint32()
                _check(_call(control2, 14, ctypes.byref(pid), argtypes=(ctypes.c_void_p,)))
                exe = _exe_name(pid.value) if pid.value else None
                if exe:
                    names.add(exe)
            finally:
                _release(control2)
                _release(control)
    finally:
        _release(enumerator)
        _release(manager)
    return frozenset(names)


def read_snapshot() -> EndpointSnapshot | None:
    """Enumerate active render endpoints and the two defaults, or None on any failure.

    Must run on a thread that joined COM (the reader thread does). Returns None
    ("no information") off Windows or when any COM step fails; logs once.
    """

    if sys.platform != "win32":
        return None
    ole32 = _ole32()
    if ole32 is None:
        return None
    enumerator = ctypes.c_void_p()
    try:
        clsid, iid = _GUID.of(_CLSID_ENUMERATOR), _GUID.of(_IID_ENUMERATOR)
        _check(
            int(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid), None, _CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(enumerator)
                )
            )
        )
        ids: dict[str, str | None] = {}
        for key, role in (("comm", _E_COMMUNICATIONS), ("console", _E_CONSOLE)):
            device = ctypes.c_void_p()
            try:
                _check(
                    _call(
                        enumerator,
                        4,
                        _E_RENDER,
                        role,
                        ctypes.byref(device),
                        argtypes=(ctypes.c_int, ctypes.c_int, ctypes.c_void_p),
                    )
                )
                ids[key] = _device_id(device)
            except OSError:
                ids[key] = None  # e.g. no communications device: that default is simply absent
            finally:
                _release(device)
        collection = ctypes.c_void_p()
        sessions: dict[str, frozenset[str]] = {}
        _check(
            _call(
                enumerator,
                3,
                _E_RENDER,
                _DEVICE_STATE_ACTIVE,
                ctypes.byref(collection),
                argtypes=(ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p),
            )
        )
        try:
            count = ctypes.c_uint()
            _check(_call(collection, 3, ctypes.byref(count), argtypes=(ctypes.c_void_p,)))
            for i in range(count.value):
                device = ctypes.c_void_p()
                try:
                    _check(_call(collection, 4, i, ctypes.byref(device), argtypes=(ctypes.c_uint, ctypes.c_void_p)))
                    sessions[_device_id(device)] = _active_executables(device)
                finally:
                    _release(device)
        finally:
            _release(collection)
        return EndpointSnapshot(sessions=sessions, communications_id=ids["comm"], console_id=ids["console"])
    except Exception as exc:
        _warn_once("COM session enumeration failed", exc)
        return None
    finally:
        _release(enumerator)


def pick_endpoint_id(
    current_id: str | None,
    call_apps: frozenset[str],
    snapshot: EndpointSnapshot | None,
) -> tuple[str | None, str]:
    """Select from a snapshot; with no snapshot, return (None, 'no_information').

    The caller then falls back to ``soundcard.default_speaker()`` (the console
    default), which is today's behaviour.
    """

    if snapshot is None:
        return None, "no_information"
    return select_endpoint(snapshot.sessions, snapshot.communications_id, snapshot.console_id, current_id, call_apps)
