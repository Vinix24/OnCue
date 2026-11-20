"""Which processes use the microphone right now (macOS 14.2+, Core Audio).

Core Audio lists one object per process that uses audio. Each carries its pid,
its bundle id and whether it is capturing input at this moment. This module
reads exactly those three values through ``ctypes`` and never reads audio.

``read_mic_users`` always returns one of three results: ``MicUsageUnavailable``
(not macOS, macOS below 14.2, or no process list), ``MicUsageOk`` (a list that
may be empty) or ``MicUsageReadError`` (a failed read, never an empty list).
OnCue's own processes (this process and all its descendants, which includes the
audio-capture helper started with ``subprocess.Popen``) are left out.

Probe on the command line, from the repo root::

    PYTHONPATH=src .venv/bin/python scripts/probe_mic_users.py
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

_COREAUDIO_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
_COREFOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_MIN_MACOS = (14, 2)

# AudioHardware.h: kAudioObjectSystemObject
_SYSTEM_OBJECT = 1
# AudioHardwareBase.h: kAudioObjectPropertyScopeGlobal ('glob')
_SCOPE_GLOBAL = 0x676C6F62
# AudioHardwareBase.h: kAudioObjectPropertyElementMain
_ELEMENT_MAIN = 0
# AudioHardware.h: kAudioHardwarePropertyProcessObjectList ('prs#')
_PROP_PROCESS_OBJECT_LIST = 0x70727323
# AudioHardware.h: kAudioProcessPropertyPID ('ppid')
_PROP_PROCESS_PID = 0x70706964
# AudioHardware.h: kAudioProcessPropertyBundleID ('pbid')
_PROP_PROCESS_BUNDLE_ID = 0x70626964
# AudioHardware.h: kAudioProcessPropertyIsRunningInput ('piri')
_PROP_PROCESS_IS_RUNNING_INPUT = 0x70697269
# CoreFoundation CFString.h: kCFStringEncodingUTF8 (not a Core Audio constant)
_CF_UTF8 = 0x08000100


@dataclass(frozen=True)
class MicUser:
    pid: int
    bundle_id: str


@dataclass(frozen=True)
class MicUsageUnavailable:
    reason: str


@dataclass(frozen=True)
class MicUsageOk:
    users: tuple[MicUser, ...]


@dataclass(frozen=True)
class MicUsageReadError:
    reason: str


MicUsageResult = MicUsageUnavailable | MicUsageOk | MicUsageReadError


class NativeReadError(Exception):
    """The native reader failed; the message is a one-line reason."""


# A native reader returns (pid, bundle_id, running_input) per audio process.
NativeReader = Callable[[], list[tuple[int, str, bool]]]


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _macos_version() -> tuple[int, ...]:
    parts = platform.mac_ver()[0].split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return ()


def _platform_ok() -> str | None:
    """Return None when this OS can offer the signal, else the reason it cannot."""

    if sys.platform != "darwin":
        return f"not macOS ({sys.platform})"
    version = _macos_version()
    if version[:2] < _MIN_MACOS:
        return f"macOS {platform.mac_ver()[0] or 'unknown'} is below 14.2"
    return None


class _CoreAudioReader:
    def __init__(self) -> None:
        self._ca = ctypes.CDLL(_COREAUDIO_PATH)
        self._cf = ctypes.CDLL(_COREFOUNDATION_PATH)
        u32, vp = ctypes.c_uint32, ctypes.c_void_p
        u32p = ctypes.POINTER(u32)
        addrp = ctypes.POINTER(_AudioObjectPropertyAddress)
        self._ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, addrp]
        self._ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
        self._ca.AudioObjectGetPropertyDataSize.argtypes = [u32, addrp, u32, vp, u32p]
        self._ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self._ca.AudioObjectGetPropertyData.argtypes = [u32, addrp, u32, vp, u32p, vp]
        self._ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self._cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, u32]
        self._cf.CFStringGetCString.restype = ctypes.c_ubyte
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._cf.CFRelease.restype = None

    @staticmethod
    def _addr(selector: int) -> _AudioObjectPropertyAddress:
        return _AudioObjectPropertyAddress(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)

    def has_process_list(self) -> bool:
        addr = self._addr(_PROP_PROCESS_OBJECT_LIST)
        return bool(self._ca.AudioObjectHasProperty(_SYSTEM_OBJECT, ctypes.byref(addr)))

    def _get(
        self, obj: int, selector: int, buf: ctypes.Array | ctypes.c_uint32 | ctypes.c_int32 | ctypes.c_void_p, what: str
    ) -> None:
        addr = self._addr(selector)
        size = ctypes.c_uint32(ctypes.sizeof(buf))
        status = self._ca.AudioObjectGetPropertyData(
            obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(buf)
        )
        if status != 0:
            raise NativeReadError(f"{what}: OSStatus {status}")

    def _object_ids(self) -> list[int]:
        addr = self._addr(_PROP_PROCESS_OBJECT_LIST)
        size = ctypes.c_uint32(0)
        status = self._ca.AudioObjectGetPropertyDataSize(
            _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)
        )
        if status != 0:
            raise NativeReadError(f"process list size: OSStatus {status}")
        count = size.value // ctypes.sizeof(ctypes.c_uint32)
        if count == 0:
            return []
        ids = (ctypes.c_uint32 * count)()
        size = ctypes.c_uint32(ctypes.sizeof(ids))
        status = self._ca.AudioObjectGetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids
        )
        if status != 0:
            raise NativeReadError(f"process list: OSStatus {status}")
        return list(ids[: size.value // ctypes.sizeof(ctypes.c_uint32)])

    def _bundle_id(self, obj: int) -> str:
        ref = ctypes.c_void_p(None)
        self._get(obj, _PROP_PROCESS_BUNDLE_ID, ref, "bundle id")
        if not ref.value:
            return ""
        try:
            buf = ctypes.create_string_buffer(1024)
            if not self._cf.CFStringGetCString(ref, buf, len(buf), _CF_UTF8):
                raise NativeReadError("bundle id: conversion to UTF-8 failed")
            return buf.value.decode("utf-8")
        finally:
            self._cf.CFRelease(ref)

    def __call__(self) -> list[tuple[int, str, bool]]:
        out: list[tuple[int, str, bool]] = []
        for obj in self._object_ids():
            running = ctypes.c_uint32(0)
            self._get(obj, _PROP_PROCESS_IS_RUNNING_INPUT, running, "running input")
            if not running.value:
                continue
            pid = ctypes.c_int32(0)
            self._get(obj, _PROP_PROCESS_PID, pid, "pid")
            out.append((pid.value, self._bundle_id(obj), True))
        return out


def _make_reader() -> _CoreAudioReader | None:
    try:
        reader = _CoreAudioReader()
        return reader if reader.has_process_list() else None
    except OSError:
        return None


def mic_usage_available() -> bool:
    """True when this Mac can list microphone users. Run once at start."""

    return _platform_ok() is None and _make_reader() is not None


def _process_table() -> dict[int, int]:
    """pid -> ppid for every process, from one ``ps`` call."""

    proc = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5, check=False)
    if proc.returncode != 0:
        raise NativeReadError(f"ps exited with status {proc.returncode}")
    table: dict[int, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            table[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    if not table:
        raise NativeReadError("ps returned no processes")
    return table


def _own_pids(table: dict[int, int], root: int) -> set[int]:
    own = {root}
    grew = True
    while grew:
        grew = False
        for pid, ppid in table.items():
            if ppid in own and pid not in own:
                own.add(pid)
                grew = True
    return own


def read_mic_users(
    *,
    reader: NativeReader | None = None,
    process_table: Callable[[], dict[int, int]] = _process_table,
    own_pid: int | None = None,
) -> MicUsageResult:
    """Return the processes capturing input now, excluding OnCue's own."""

    if reader is None:
        reason = _platform_ok()
        if reason is not None:
            return MicUsageUnavailable(reason)
        native = _make_reader()
        if native is None:
            return MicUsageUnavailable("Core Audio has no process object list")
        reader = native
    try:
        raw = reader()
        own = _own_pids(process_table(), os.getpid() if own_pid is None else own_pid)
    except NativeReadError as exc:
        return MicUsageReadError(str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        return MicUsageReadError(f"{type(exc).__name__}: {exc}")
    users = tuple(
        MicUser(pid=pid, bundle_id=bundle_id) for pid, bundle_id, running in raw if running and pid not in own
    )
    return MicUsageOk(users)


def group_by_bundle(users: tuple[MicUser, ...] | list[MicUser]) -> dict[str, tuple[int, ...]]:
    """One entry per app: bundle id -> sorted pids. No bundle id keys on the pid."""

    grouped: dict[str, list[int]] = {}
    for user in users:
        key = user.bundle_id or f"pid:{user.pid}"
        grouped.setdefault(key, []).append(user.pid)
    return {key: tuple(sorted(pids)) for key, pids in grouped.items()}
