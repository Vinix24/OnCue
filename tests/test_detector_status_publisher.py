"""DetectorStatusPublisher — the detector's counters on the wire.

#203 made "running but dropping everything" distinguishable from "never
started" in the log. This publisher carries the same counters to the dashboard
so the seller can see it too. The rules that matter here: the cadence stays
bounded, the three moments that must never be throttled away are forced, and
transcript text can never reach the payload.
"""

from __future__ import annotations

import pytest

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector.status_publisher import (
    DETECTOR_STATUS_CHANNEL,
    DetectorStatusPublisher,
)

_COUNT_KEYS = (
    "received",
    "skipped_not_prospect",
    "buffered_below_min",
    "debounced",
    "classified",
    "dropped_none",
    "dropped_low_confidence",
    "dispatched",
)


class _FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Recorder:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self._fail = fail

    async def __call__(self, channel: str, payload: dict) -> None:
        if self._fail:
            raise RuntimeError("hub unreachable")
        self.sent.append((channel, payload))


def _counts(**overrides: int) -> dict[str, int]:
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    counts.update(overrides)
    return counts


def _publisher(recorder: _Recorder, clock: _FakeClock, **kwargs) -> DetectorStatusPublisher:
    return DetectorStatusPublisher(
        recorder,
        DetectorConfig(),
        monotonic=clock,
        wall_clock_ms=lambda: 1_700_000_000_000,
        **kwargs,
    )


async def test_publishes_to_the_detector_status_channel() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock)

    assert await publisher.publish(_counts()) is True

    channel, payload = recorder.sent[0]
    assert channel == DETECTOR_STATUS_CHANNEL
    assert payload["type"] == "detector_status"


async def test_cadence_is_bounded_between_forced_publishes() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock, interval_seconds=5.0)

    await publisher.publish(_counts())  # first call always goes out
    for _ in range(20):
        clock.advance(0.25)
        await publisher.publish(_counts())

    # 20 loop ticks at 0.25s = 5.0s elapsed, so exactly one more publish.
    assert len(recorder.sent) == 2


async def test_force_bypasses_the_throttle() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock, interval_seconds=5.0)

    await publisher.publish(_counts())
    clock.advance(0.1)
    assert await publisher.publish(_counts(), force=True) is True
    assert len(recorder.sent) == 2


async def test_state_tracks_whether_anything_was_received() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock, interval_seconds=0.0)

    await publisher.publish(_counts())
    await publisher.publish(_counts(received=3))
    await publisher.publish(_counts(received=3), stopped=True)

    assert [payload["state"] for _, payload in recorder.sent] == ["listening", "active", "stopped"]


async def test_payload_carries_every_counter_from_the_consume_loop() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock)

    await publisher.publish(_counts(received=9, skipped_not_prospect=4, dispatched=1))

    counts = recorder.sent[0][1]["counts"]
    assert set(counts) == set(_COUNT_KEYS)
    assert counts["received"] == 9
    assert counts["skipped_not_prospect"] == 4
    assert counts["dispatched"] == 1


async def test_payload_carries_the_thresholds_and_policy_the_dashboard_needs() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    config = DetectorConfig(
        confidence_threshold_low=0.4,
        confidence_threshold_high=0.7,
        min_chunks_to_classify=5,
        only_classify_prospect=False,
    )
    publisher = DetectorStatusPublisher(recorder, config, monotonic=clock)

    await publisher.publish(_counts())

    payload = recorder.sent[0][1]
    assert payload["thresholds"] == {"low": 0.4, "high": 0.7}
    assert payload["policy"]["min_chunks_to_classify"] == 5
    assert payload["policy"]["only_classify_prospect"] is False


async def test_uptime_and_last_chunk_age_are_reported() -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock, interval_seconds=0.0)

    await publisher.publish(_counts())
    assert recorder.sent[0][1]["last_chunk_age_ms"] is None

    clock.advance(2.0)
    publisher.note_chunk()
    clock.advance(3.0)
    await publisher.publish(_counts(received=1))

    payload = recorder.sent[1][1]
    assert payload["uptime_ms"] == 5000
    assert payload["last_chunk_age_ms"] == 3000


async def test_payload_is_counters_only_and_never_carries_transcript_text() -> None:
    """The status channel is a privacy boundary: counters and timestamps only.

    Serialise the whole payload and assert nothing string-shaped beyond the
    fixed vocabulary is in it, so a future field carrying an utterance fails
    here rather than shipping speech to a channel that never promised it.
    """
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock)

    await publisher.publish(_counts(received=3))

    payload = recorder.sent[0][1]

    def _strings(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for key, value in node.items() for s in ([key] + _strings(value))]
        if isinstance(node, (list, tuple)):
            return [s for item in node for s in _strings(item)]
        return []

    allowed = {
        "type",
        "detector_status",
        "state",
        "listening",
        "active",
        "stopped",
        "counts",
        "uptime_ms",
        "last_chunk_age_ms",
        "interval_ms",
        "thresholds",
        "low",
        "high",
        "policy",
        "only_classify_prospect",
        "min_chunks_to_classify",
        "classification_debounce_seconds",
        "timestamp_ms",
        *_COUNT_KEYS,
    }
    assert set(_strings(payload)) <= allowed


async def test_a_failing_hub_never_takes_the_detector_down() -> None:
    recorder, clock = _Recorder(fail=True), _FakeClock()
    publisher = _publisher(recorder, clock)

    assert await publisher.publish(_counts(), force=True) is False


@pytest.mark.parametrize("interval", [0.0, 5.0])
async def test_first_publish_always_goes_out(interval: float) -> None:
    recorder, clock = _Recorder(), _FakeClock()
    publisher = _publisher(recorder, clock, interval_seconds=interval)

    assert await publisher.publish(_counts()) is True


# --- _StatusChannel: one reused hub connection ------------------------------
#
# The status heartbeat fires every few seconds for the whole call, so it does
# NOT go through the module's per-message _publish_ws: that would open a
# connection per tick and log a WARNING per tick whenever the hub is away.


class _FakeConnection:
    def __init__(self, *, fail_on_send: bool = False) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._fail_on_send = fail_on_send

    async def send(self, data: str) -> None:
        if self._fail_on_send:
            raise ConnectionResetError("hub went away")
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


def _patch_connect(monkeypatch, connections: list[_FakeConnection]) -> list[str]:
    urls: list[str] = []

    async def _connect(url: str):
        urls.append(url)
        return connections[len(urls) - 1]

    monkeypatch.setattr(detector_main.websockets, "connect", _connect)
    return urls


async def test_status_channel_reuses_one_connection(monkeypatch) -> None:
    connection = _FakeConnection()
    urls = _patch_connect(monkeypatch, [connection])
    channel = detector_main._StatusChannel(WebSocketConfig())

    for _ in range(3):
        await channel.send("detector-status", {"type": "detector_status"})

    assert len(urls) == 1, "the heartbeat must not reconnect per tick"
    assert urls[0].split("?")[0].endswith("/ws/detector-status")
    assert len(connection.sent) == 3


async def test_status_channel_reconnects_after_a_failed_send(monkeypatch) -> None:
    broken, healthy = _FakeConnection(fail_on_send=True), _FakeConnection()
    urls = _patch_connect(monkeypatch, [broken, healthy])
    channel = detector_main._StatusChannel(WebSocketConfig())

    with pytest.raises(ConnectionResetError):
        await channel.send("detector-status", {"type": "detector_status"})

    await channel.send("detector-status", {"type": "detector_status"})

    assert len(urls) == 2
    assert len(healthy.sent) == 1


async def test_status_channel_close_is_idempotent(monkeypatch) -> None:
    connection = _FakeConnection()
    _patch_connect(monkeypatch, [connection])
    channel = detector_main._StatusChannel(WebSocketConfig())

    await channel.send("detector-status", {"type": "detector_status"})
    await channel.close()
    await channel.close()

    assert connection.closed is True
