"""Tests for scripts/measure_live_vs_postcall.py on synthetic data only.

Fictional companies, fake live runner, a tiny normalization config: no customer names,
audio or conversation content, and no server is started.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from scripts import measure_live_vs_postcall as lv
from tests.test_measure_entities_per_model import (
    LISTENING,
    NORMALIZATION_YAML,
    TUNING,
)


def _write_wav(path: Path, seconds: float = 1.0, rate: int = 16000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x01\x02" * int(seconds * rate) * channels)
    return path


def _ident(text: str):
    return text, []


def _entry(text: str, start_ms: int, speaker: str = "prospect") -> dict:
    return {"speaker": speaker, "text": text, "start_ms": start_ms, "end_ms": start_ms + 3000}


class TestReplayDir:
    def test_prospect_is_the_fragment_and_self_is_silent_with_same_length(self, tmp_path):
        fragment = _write_wav(tmp_path / "frag.wav", seconds=2.0)
        out = lv.build_replay_dir(fragment, tmp_path / "replay" / "x")
        with wave.open(str(out / "prospect.wav"), "rb") as prospect, wave.open(str(out / "self.wav"), "rb") as own:
            assert prospect.getnframes() == own.getnframes() == 32000
            assert prospect.getframerate() == own.getframerate() == 16000
            assert prospect.readframes(10) == b"\x01\x02" * 10
            assert set(own.readframes(own.getnframes())) == {0}

    def test_rejects_wrong_sample_rate(self, tmp_path):
        fragment = _write_wav(tmp_path / "frag.wav", rate=8000)
        with pytest.raises(ValueError, match="16 kHz mono"):
            lv.build_replay_dir(fragment, tmp_path / "replay")

    def test_rejects_stereo(self, tmp_path):
        fragment = _write_wav(tmp_path / "frag.wav", channels=2)
        with pytest.raises(ValueError, match="16 kHz mono"):
            lv.build_replay_dir(fragment, tmp_path / "replay")

    def test_duration(self, tmp_path):
        assert lv.wav_seconds(_write_wav(tmp_path / "a.wav", seconds=1.5)) == pytest.approx(1.5)


class TestLiveEnv:
    def _env(self, tmp_path, base_env=None):
        return lv.build_live_env(
            base_env or {},
            replay_dir=tmp_path,
            port=9123,
            model_path="/m/ggml.bin",
            whisper_binary="/b/whisper-cli",
            whisper_server_binary=None,
        )

    def test_operator_settings_and_replay_mode(self, tmp_path):
        env = self._env(tmp_path)
        assert env["WHISPER_MAX_BUFFER_SECONDS"] == "12.0"
        assert env["WHISPER_MAX_BUFFER_HARD_SECONDS"] == "20.0"
        assert env["WHISPER_SILENCE_GAP_SECONDS"] == "2.0"
        assert env["WHISPER_MIN_SEGMENT_SECONDS"] == "3.0"
        assert env["WHISPER_LONG_SILENCE_ESCAPE_SECONDS"] == "4.0"
        assert env["WHISPER_CPP_THREADS"] == "8"
        assert env["WHISPER_VOCABULARY_ENABLED"] == "true"
        assert env["WHISPER_CPP_MODEL_PATH"] == "/m/ggml.bin"
        assert env["AUDIO_CAPTURE_METHOD"] == "replay"
        assert env["REPLAY_SPEED"] == "1.0"
        assert env["WS_HUB_PORT"] == "9123"
        assert env["WS_HUB_HOST"] == "127.0.0.1"

    def test_every_llm_task_is_none(self, tmp_path):
        env = self._env(tmp_path)
        for task in lv.LLM_TASKS:
            assert env[f"{task}_LLM_PROVIDER"] == "none"

    def test_keys_and_foreign_whisper_settings_do_not_leak_in(self, tmp_path):
        env = self._env(
            tmp_path,
            {
                "OPENAI_API_KEY": "sk-x",
                "GROQ_API_KEY": "gsk-x",
                "WHISPER_MODEL": "large-v3",
                "WHISPER_PORT": "1",
                "PATH": "/usr/bin",
            },
        )
        assert "OPENAI_API_KEY" not in env and "GROQ_API_KEY" not in env
        assert env["WHISPER_MODEL"] == "large-v3-turbo"
        assert "WHISPER_PORT" not in env
        assert env["PATH"] == "/usr/bin"

    def test_server_binary_only_when_given(self, tmp_path):
        assert "WHISPER_CPP_SERVER_BINARY" not in self._env(tmp_path)
        env = lv.build_live_env(
            {}, replay_dir=tmp_path, port=1, model_path="m", whisper_binary="b", whisper_server_binary="/s"
        )
        assert env["WHISPER_CPP_SERVER_BINARY"] == "/s"


class TestConversion:
    def test_prospect_entries_become_timestamped_lines_in_start_order(self):
        entries = [_entry("tweede", 65000), _entry("eerste", 1500), _entry("stilte", 2000, speaker="self")]
        assert lv.entries_to_timestamped(entries) == "[00:01] eerste\n[01:05] tweede"

    def test_empty_and_missing_text_is_skipped(self):
        assert lv.entries_to_timestamped([_entry("  ", 0), {"speaker": "prospect", "start_ms": 5}]) == ""

    def test_roundtrips_through_the_scorer_parser(self):
        text = lv.entries_to_timestamped([_entry("a b", 0), _entry("c", 31000)])
        assert lv.strip_timestamps(text) == "a b c"

    def test_report_entries_read(self, tmp_path):
        path = tmp_path / "r_report.json"
        path.write_text(json.dumps({"full_transcript": [_entry("x", 0)]}), encoding="utf-8")
        assert lv.read_report_entries(path)[0]["text"] == "x"

    def test_report_without_transcript_is_an_error(self, tmp_path):
        path = tmp_path / "r_report.json"
        path.write_text(json.dumps({"other": 1}), encoding="utf-8")
        with pytest.raises(ValueError, match="full_transcript"):
            lv.read_report_entries(path)


class TestNewestReport:
    def test_ignores_reports_older_than_the_run(self, tmp_path):
        old = tmp_path / "old_report.json"
        old.write_text("{}", encoding="utf-8")
        assert lv.newest_report(tmp_path, old.stat().st_mtime + 10) is None
        assert lv.newest_report(tmp_path, 0) == old


class TestDifference:
    def test_identical_text_has_zero_difference(self):
        assert lv.path_difference("[00:00] een twee drie", "[00:00] een twee drie", _ident) == 0.0

    def test_one_substitution_in_four_words(self):
        diff = lv.path_difference("[00:00] een twee x vier", "[00:00] een twee drie vier", _ident)
        assert diff == pytest.approx(0.25)

    def test_empty_postcall_is_none(self):
        assert lv.path_difference("[00:00] iets", "", _ident) is None

    def test_layer_is_applied_to_the_postcall_side(self, tmp_path):
        path = tmp_path / "norm.yaml"
        path.write_text(NORMALIZATION_YAML, encoding="utf-8")
        lists = lv.load_normalization_lists(path)

        def fn(text):
            new_text, reps = lv.normalize(text, lists)
            return new_text, [(a, b) for a, b, _ in reps]

        # Post-call raw has Akme, the layer turns it into Acme; live already says Acme.
        assert lv.path_difference("[00:00] met Acme werken", "[00:00] met Akme werken", fn) == 0.0

    def test_word_edit_rate_matches_known_value(self):
        assert lv.word_edit_rate("a b c d", "a c d e") == pytest.approx(0.5)
        assert lv.word_edit_rate("a b", "a b") == 0.0


class TestDirections:
    def test_counts_each_direction(self, tmp_path):
        tuning = tmp_path / "t.md"
        tuning.write_text(TUNING, encoding="utf-8")
        listening = tmp_path / "l.md"
        listening.write_text(LISTENING, encoding="utf-8")
        cases = lv.base.parse_tuning_set(tuning)
        fragments = {lv.m._key(f.name): f for f in lv.m.parse_listening_list(listening)}
        variant = lv.m.AsrVariant("x", "x", True)
        # acmecorp window centers: Akme 30s, Bolt 120s, Zork 210s (relative to fragment start).
        live = {"acmecorp": "[00:20] samen met Acme\n[02:00] door Bolt\n[03:30] afdeling Zork"}
        post = {"acmecorp": "[00:20] samen met Akme\n[02:00] door Bolt\n[03:30] afdeling Zorg"}
        a = lv.m.score_variant(variant, cases, fragments, live, _ident)
        b = lv.m.score_variant(variant, cases, fragments, post, _ident)
        counts = lv.compare_outcomes(a, b)
        assert counts.live_only_good == 1  # Acme right live, Akme post-call
        assert counts.postcall_only_good == 1  # Zorg right post-call, Zork live
        assert counts.both_good == 1  # Bolt
        assert counts.both_wrong == 0  # the unscored case is not counted


class TestRunFragments:
    def test_writes_each_fragment_at_once_and_skips_cached(self, tmp_path):
        calls: list[str] = []

        def live(wav: Path, key: str) -> lv.LiveResult:
            calls.append(key)
            return lv.LiveResult(entries=[_entry(f"tekst {key}", 0)], seconds=12.0, outbound=[])

        audio = {"a": tmp_path / "a.wav", "b": tmp_path / "b.wav"}
        seconds = lv.run_fragments(["a", "b"], audio, tmp_path, live)
        assert seconds == {"a": 12.0, "b": 12.0}
        assert (tmp_path / "live" / "a.txt").read_text(encoding="utf-8") == "[00:00] tekst a"
        assert json.loads((tmp_path / "live" / "b.meta.json").read_text())["segments"] == 1
        lv.run_fragments(["a", "b"], audio, tmp_path, live)
        assert calls == ["a", "b"]

    def test_failure_on_a_later_fragment_keeps_earlier_results(self, tmp_path):
        def live(wav: Path, key: str) -> lv.LiveResult:
            if key == "b":
                raise RuntimeError("server died")
            return lv.LiveResult(entries=[_entry("x", 0)], seconds=1.0)

        audio = {"a": tmp_path / "a.wav", "b": tmp_path / "b.wav"}
        with pytest.raises(RuntimeError, match="server died"):
            lv.run_fragments(["a", "b"], audio, tmp_path, live)
        assert (tmp_path / "live" / "a.txt").exists()
        assert not (tmp_path / "live" / "b.txt").exists()


class TestMain:
    def test_end_to_end_with_fake_live_runner(self, tmp_path, capsys):
        tuning = tmp_path / "t.md"
        tuning.write_text(TUNING, encoding="utf-8")
        listening = tmp_path / "l.md"
        listening.write_text(LISTENING, encoding="utf-8")
        norm = tmp_path / "norm.yaml"
        norm.write_text(NORMALIZATION_YAML, encoding="utf-8")
        frags = tmp_path / "frags"
        frags.mkdir()
        _write_wav(frags / "acmecorp.wav")  # nolisten has no audio
        post = tmp_path / "post"
        post.mkdir()
        (post / "acmecorp.txt").write_text(
            "[00:00] samen met Akme\n[02:00] door Bolt\n[03:30] afdeling Zork", encoding="utf-8"
        )

        def live(wav: Path, key: str) -> lv.LiveResult:
            return lv.LiveResult(
                entries=[
                    _entry("samen met Acme", 20000),
                    _entry("door Boot", 120000),  # what the live layer emits for Bolt
                    _entry("afdeling Zork", 210000),
                ],
                seconds=240.0,
            )

        out = tmp_path / "out"
        code = lv.main(
            [
                "--tuning-set", str(tuning),
                "--listening-list", str(listening),
                "--fragments-dir", str(frags),
                "--postcall-dir", str(post),
                "--out-dir", str(out),
                "--model-path", "/m",
                "--normalization-config", str(norm),
            ],
            live_fn=live,
        )
        assert code == 0
        printed = capsys.readouterr().out
        assert "verschil tussen de paden" in printed.lower()
        assert "zonder audio: 1" in printed
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert summary["fragments_live"] == ["acmecorp"]
        assert summary["fragments_missing_audio"] == ["nolisten"]
        # Acme: live emits it, post-call raw Akme is repaired by the layer: good in both.
        # Bolt: the layer breaks it in both paths. Zork stays wrong in both.
        assert summary["directions"] == {
            "live_only_good": 0, "postcall_only_good": 0, "both_good": 1, "both_wrong": 2,
        }
        assert (out / "live-outcomes.json").exists()

    def test_score_only_does_not_call_the_live_runner(self, tmp_path):
        tuning = tmp_path / "t.md"
        tuning.write_text(TUNING, encoding="utf-8")
        listening = tmp_path / "l.md"
        listening.write_text(LISTENING, encoding="utf-8")
        norm = tmp_path / "norm.yaml"
        norm.write_text(NORMALIZATION_YAML, encoding="utf-8")
        frags = tmp_path / "frags"
        frags.mkdir()
        _write_wav(frags / "acmecorp.wav")
        post = tmp_path / "post"
        post.mkdir()

        def live(wav: Path, key: str) -> lv.LiveResult:
            raise AssertionError("live runner must not be called")

        code = lv.main(
            [
                "--tuning-set", str(tuning),
                "--listening-list", str(listening),
                "--fragments-dir", str(frags),
                "--postcall-dir", str(post),
                "--out-dir", str(tmp_path / "out"),
                "--model-path", "/m",
                "--normalization-config", str(norm),
                "--score-only",
            ],
            live_fn=live,
        )
        assert code == 0
