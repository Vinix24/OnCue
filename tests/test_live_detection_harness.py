"""Pure parts of scripts/measure_live_detection.py: no server, no network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "measure_live_detection", Path(__file__).resolve().parents[1] / "scripts" / "measure_live_detection.py"
)
assert _SPEC and _SPEC.loader
mld = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_live_detection"] = mld
_SPEC.loader.exec_module(mld)


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["--fixture-dir", str(tmp_path), "--lang", "en", "--out-dir", str(tmp_path / "o"), *extra]


def _ev(t: float, ch: str, **payload: object) -> dict:
    return {"t_ms": t, "channel": ch, "event": payload}


def test_parse_defaults(tmp_path: Path) -> None:
    a = mld.parse_args(_argv(tmp_path))
    assert (a.port, a.tier, a.speed) == (8771, "free", 1.0)


@pytest.mark.parametrize("bad", ["8760", "80", "70000"])
def test_parse_refuses_bad_port(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SystemExit):
        mld.parse_args(_argv(tmp_path, "--port", bad))


@pytest.mark.parametrize("extra", [["--tier", "gold"], ["--wait", "0"], ["--speed", "-1"]])
def test_parse_refuses_bad_values(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit):
        mld.parse_args(_argv(tmp_path, *extra))


def test_build_env_is_scratch_and_keyless(tmp_path: Path) -> None:
    a = mld.parse_args(_argv(tmp_path, "--tier", "pro", "--port", "8799"))
    host = {"OPENAI_API_KEY": "k", "LLM_PROVIDER": "openai", "LANGUAGE": "nl", "PATH": "/bin", "WS_HUB_PORT": "8760"}
    env = mld.build_env(a, "tok", host)
    assert env["OPENAI_API_KEY"] == ""
    assert env["LLM_PROVIDER"] == "none"
    assert env["WS_HUB_PORT"] == "8799"
    assert env["SHUTDOWN_TOKEN"] == "tok"
    assert env["SALES_COPILOT_DEV_TIER"] == "pro"
    assert env["LANGUAGE"] == "en" and env["CALL_LANGUAGE"] == "en"


def test_summarize_counts_and_delays() -> None:
    events = [
        _ev(0, "pain-points"),  # sticky empty message: not counted
        _ev(1, "coaching", _subscribed=True),  # marker: not counted
        _ev(10, "_http", start_call=[200, "ok"]),
        _ev(100, "transcript", type="transcript", text="a"),
        _ev(160, "pain-points", type="pain_point"),
        _ev(300, "transcript", type="transcript", text="b"),
        _ev(320, "pain-points", type="pain_point"),
        _ev(340, "pain-points", type="pain_point"),
    ]
    s = mld.summarize(events)
    assert s["transcript"]["count"] == 2
    assert s["transcript"]["first_delay_ms"] is None
    assert s["pain-points"]["count"] == 3
    assert s["pain-points"]["first_delay_ms"] == 60
    assert s["pain-points"]["median_delay_ms"] == 40  # delays 60, 20, 40
    assert "_http" not in s


def test_summarize_event_before_any_transcript_has_no_delay() -> None:
    s = mld.summarize([_ev(5, "objections", type="objection")])
    assert s["objections"]["count"] == 1
    assert s["objections"]["first_delay_ms"] is None


def test_format_table_lists_every_channel_with_zero_rows() -> None:
    table = mld.format_table(mld.summarize([_ev(1, "objections", type="objection")]))
    lines = table.splitlines()
    assert len(lines) == 2 + len(mld.CHANNELS)
    assert any(line.startswith("suggestions | 0 | - | - | -") for line in lines)
    assert any(line.startswith("objections | 1 | objection 1") for line in lines)


def test_main_refuses_missing_fixture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert mld.main(_argv(tmp_path / "nope")) == 2
    assert "fixture missing" in capsys.readouterr().err


def test_validate_port_and_token() -> None:
    with pytest.raises(ValueError):
        mld.validate_port(8760)
    assert mld.make_token() != mld.make_token()


def test_build_env_sets_provider_keys_empty_so_dotenv_cannot_inject(tmp_path: Path) -> None:
    a = mld.parse_args(_argv(tmp_path))
    env = mld.build_env(a, "tok", {"PATH": "/bin", "OPENAI_API_KEY": "k", "GROQ_API_KEY": "g"})
    for key in ("OPENAI_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        assert env[key] == ""
    assert env["LLM_PROVIDER"] == "none"


def test_failed_channels_lists_errors_in_channel_order() -> None:
    events = [_ev(1, "coaching", _subscribe_error="X"), _ev(2, "transcript", _subscribed=True)]
    events.append(_ev(3, "transcript", _subscribe_error="Y"))
    assert mld.failed_channels(events) == ["transcript", "coaching"]
    assert mld.failed_channels([_ev(1, "transcript", _subscribed=True)]) == []


class _FakeProc:
    returncode = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _fixture(tmp_path: Path) -> Path:
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "self.wav").write_bytes(b"")
    (fx / "prospect.wav").write_bytes(b"")
    return fx


def _main_args(tmp_path: Path) -> list[str]:
    return ["--fixture-dir", str(_fixture(tmp_path)), "--lang", "en", "--out-dir", str(tmp_path / "o")]


def _read_result(tmp_path: Path) -> dict:
    import json

    return json.loads((tmp_path / "o" / "result.json").read_text())


def test_main_failed_subscription_is_incomplete_and_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mld.subprocess, "Popen", lambda *a, **k: _FakeProc())

    async def fake_run(args, token, proc, events):
        events.append(_ev(1, "transcript", _subscribed=True))
        events.append(_ev(2, "coaching", _subscribe_error="ConnectionClosed()"))
        events.append(_ev(3, "_http", start_call=[200, "ok"]))
        return "subscription failed for channel(s): coaching"

    monkeypatch.setattr(mld, "_run", fake_run)
    assert mld.main(_main_args(tmp_path)) == 1
    result = _read_result(tmp_path)
    assert "coaching" in result["error"]
    assert result["failed_channels"] == ["coaching"]


def test_run_reports_failed_subscription_after_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def fake_sub(ch, args, auth, events, clock, stop):
        if ch == "coaching":
            events.append(_ev(1, ch, _subscribe_error="boom"))
        else:
            events.append(_ev(1, ch, _subscribed=True))

    async def no_sleep(_s: float) -> None:
        return None

    class _Sock:
        def close(self) -> None:
            return None

    monkeypatch.setattr(mld, "_subscribe", fake_sub)
    monkeypatch.setattr(mld.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(mld.socket, "create_connection", lambda *a, **k: _Sock())
    monkeypatch.setattr(mld, "_post", lambda *a, **k: (200, "ok"))
    args = mld.parse_args(_argv(tmp_path))
    err = asyncio.run(mld._run(args, "tok", _FakeProc(), []))  # type: ignore[arg-type]
    assert err is not None and "coaching" in err


def test_main_exception_in_run_still_writes_result_and_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProc()
    monkeypatch.setattr(mld.subprocess, "Popen", lambda *a, **k: proc)

    async def fake_run(args, token, proc, events):
        events.append(_ev(5, "transcript", type="transcript", text="a"))
        raise TimeoutError("start-call timed out")

    monkeypatch.setattr(mld, "_run", fake_run)
    assert mld.main(_main_args(tmp_path)) == 1
    result = _read_result(tmp_path)
    assert result["failure"] == {"stage": "run", "error_type": "TimeoutError", "message": "start-call timed out"}
    assert result["channels"]["transcript"]["count"] == 1
    assert (tmp_path / "o" / "events.jsonl").read_text().count("\n") == 1
    assert proc.terminated is True


def test_main_popen_failure_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("no interpreter")

    monkeypatch.setattr(mld.subprocess, "Popen", boom)
    assert mld.main(_main_args(tmp_path)) == 1
    result = _read_result(tmp_path)
    assert result["failure"]["stage"] == "start_server"
    assert result["failure"]["error_type"] == "FileNotFoundError"
    assert result["error"]
