"""Platform-routing tests for the Windows WASAPI capture wiring (PR-W2).

Fully mocked: no real Windows host, no real ``soundcard``/``sounddevice``. The
two pure normalization helpers (``normalize_capture_method``,
``resolve_prospect_source``) are exercised directly via their injectable
``platform`` kwarg. ``DualAudioCapture.create()`` reads ``sys.platform``
dynamically at call time, so its win32 routing is exercised by monkeypatching
``sales_copilot.audio.capture.sys.platform`` instead.
"""

from __future__ import annotations

import sales_copilot.audio.capture as cap
from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.calltap import CallTapStream
from sales_copilot.audio.capture import (
    AudioConfig,
    AudioTeeStream,
    DualAudioCapture,
    MicStream,
    normalize_capture_method,
    resolve_prospect_source,
)
from sales_copilot.audio.wasapi import WasapiLoopbackStream

# --- normalize_capture_method ------------------------------------------------


def test_normalize_capture_method_win32_maps_macos_methods_to_wasapi() -> None:
    assert normalize_capture_method("audiotee", platform="win32") == "wasapi"
    assert normalize_capture_method("blackhole", platform="win32") == "wasapi"


def test_normalize_capture_method_win32_unset_defaults_to_wasapi() -> None:
    assert normalize_capture_method(None, platform="win32") == "wasapi"
    assert normalize_capture_method("", platform="win32") == "wasapi"


def test_normalize_capture_method_win32_honors_mic_and_wasapi() -> None:
    assert normalize_capture_method("mic", platform="win32") == "mic"
    assert normalize_capture_method("wasapi", platform="win32") == "wasapi"


def test_normalize_capture_method_non_win32_maps_wasapi_to_audiotee() -> None:
    assert normalize_capture_method("wasapi", platform="darwin") == "audiotee"
    assert normalize_capture_method("wasapi", platform="linux") == "audiotee"


def test_normalize_capture_method_non_win32_unset_defaults_to_audiotee() -> None:
    assert normalize_capture_method(None, platform="darwin") == "audiotee"
    assert normalize_capture_method("", platform="linux") == "audiotee"


def test_normalize_capture_method_non_win32_leaves_others_unchanged() -> None:
    assert normalize_capture_method("audiotee", platform="darwin") == "audiotee"
    assert normalize_capture_method("blackhole", platform="darwin") == "blackhole"
    assert normalize_capture_method("mic", platform="linux") == "mic"


# --- resolve_prospect_source ---------------------------------------------------


def test_resolve_prospect_source_never_returns_audiotee_call_on_win32() -> None:
    assert resolve_prospect_source("audiotee_call", None, platform="win32") == "blackhole"
    assert (
        resolve_prospect_source("blackhole", {"prospect_source": "audiotee_call"}, platform="win32")
        == "blackhole"
    )


def test_resolve_prospect_source_still_honors_audiotee_call_off_win32() -> None:
    assert resolve_prospect_source("audiotee_call", None, platform="darwin") == "audiotee_call"


# --- DualAudioCapture.create() factory wiring ----------------------------------


def test_factory_returns_wasapi_stream_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(cap.sys, "platform", "win32")

    config = AudioConfig(capture_method="audiotee")
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, WasapiLoopbackStream)


def test_factory_normalizes_blackhole_to_wasapi_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(cap.sys, "platform", "win32")

    config = AudioConfig(capture_method="blackhole")
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, WasapiLoopbackStream)


def test_factory_honors_mic_only_on_win32(monkeypatch) -> None:
    monkeypatch.setattr(cap.sys, "platform", "win32")

    config = AudioConfig(capture_method="mic")
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert system is None


def test_factory_entitlement_branch_unreachable_on_win32(monkeypatch) -> None:
    """Even with a Pro policy + prospect_source=audiotee_call, win32 never
    reaches the CallTap entitlement branch -- resolve_prospect_source
    neutralizes it to 'blackhole' before the branch check runs."""

    monkeypatch.setattr(cap.sys, "platform", "win32")

    class _ProPolicy:
        def allows(self, _feature_id: str) -> bool:
            return True

    config = AudioConfig(capture_method="audiotee", prospect_source="audiotee_call")
    mic, system = DualAudioCapture(config, feature_policy=_ProPolicy()).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, WasapiLoopbackStream)
    assert not isinstance(system, CallTapStream)
    assert not isinstance(system, BlackHoleStream)


def test_factory_stays_on_macos_routing_when_platform_unpatched(tmp_path) -> None:
    """Sanity regression check: without monkeypatching sys.platform, the real
    (macOS CI) platform keeps the existing audiotee routing untouched."""

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")

    config = AudioConfig(capture_method="audiotee", audiotee_path=str(audiotee_path))
    mic, system = DualAudioCapture(config).create()

    assert isinstance(mic, MicStream)
    assert isinstance(system, AudioTeeStream)
