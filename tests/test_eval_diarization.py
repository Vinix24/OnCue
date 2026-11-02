"""Tests for scripts/eval_diarization.py: the pure parts, with synthetic signals and fake backends.

No model downloads, no real audio, no network.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts import eval_diarization as ed

SR = ed.SAMPLE_RATE


def _noise(seconds: float, seed: int, level: float = 0.2) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * level).astype(np.float32)


class FakeBackend:
    """Deterministic backend: returns the segments it was built with, records how it was prepared."""

    def __init__(self, name: str, segs: list[ed.Seg], oracle: bool = False, fail: bool = False) -> None:
        self.name, self.device, self.oracle = name, "cpu", oracle
        self._segs, self._fail = segs, fail
        self.prepared_with: list[int | None] = []
        self.closed = False

    def prepare(self, num_speakers: int | None) -> None:
        if self._fail:
            raise RuntimeError("model unavailable")
        self.prepared_with.append(num_speakers)

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[ed.Seg]:
        return list(self._segs)

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------ mixing


def test_mix_tracks_sums_without_clipping_when_below_ceiling() -> None:
    a = np.full(100, 0.25, dtype=np.float32)
    b = np.full(100, 0.5, dtype=np.float32)
    mix = ed.mix_tracks(a, b)
    assert np.allclose(mix, 0.75)


def test_mix_tracks_normalises_a_sum_that_would_clip() -> None:
    a = np.full(100, 0.9, dtype=np.float32)
    b = np.full(100, 0.8, dtype=np.float32)
    mix = ed.mix_tracks(a, b)
    assert float(np.max(np.abs(mix))) == pytest.approx(0.99, abs=1e-6)
    assert np.allclose(mix, mix[0])  # uniform scaling, shape preserved


def test_mix_tracks_pads_the_shorter_track() -> None:
    mix = ed.mix_tracks(np.full(10, 0.1, dtype=np.float32), np.full(4, 0.2, dtype=np.float32))
    assert len(mix) == 10
    assert np.allclose(mix[:4], 0.3) and np.allclose(mix[4:], 0.1)


# ------------------------------------------------------------------ reference


def test_build_reference_labels_tracks_and_keeps_overlap() -> None:
    ref = ed.build_reference(mic_regions=[(1.0, 4.0), (10.0, 12.0)], system_regions=[(3.0, 6.0)])
    assert ref == [
        ed.Seg(1.0, 4.0, "self"),
        ed.Seg(3.0, 6.0, "prospect"),
        ed.Seg(10.0, 12.0, "self"),
    ]
    # 3.0-4.0 is overlapped speech: both speakers are present in the reference
    active = {s.speaker for s in ref if s.start <= 3.5 < s.end}
    assert active == {"self", "prospect"}


def test_build_reference_drops_empty_regions() -> None:
    assert ed.build_reference([(2.0, 2.0)], []) == []


def test_rttm_round_trip_and_fragment_clipping(tmp_path: Path) -> None:
    segs = [ed.Seg(1.0, 5.0, "a"), ed.Seg(8.0, 9.5, "b")]
    path = tmp_path / "x.rttm"
    ed.write_rttm(path, "uri", segs)
    assert ed.read_rttm(path) == segs
    # fragment 4..9: first segment clipped to 4..5 -> 0..1, second to 8..9 -> 4..5
    assert ed.read_rttm(path, 4.0, 9.0) == [ed.Seg(0.0, 1.0, "a"), ed.Seg(4.0, 5.0, "b")]


# ------------------------------------------------------------------ loudspeaker bleed


def test_bleed_check_skips_a_mic_that_is_a_delayed_attenuated_copy_of_the_system_track() -> None:
    system = _noise(40, seed=1)
    delay = int(0.12 * SR)
    mic = np.concatenate([np.zeros(delay, dtype=np.float32), system[:-delay] * 0.3])
    regions = [(0.0, 40.0)]
    verdict = ed.detect_loudspeaker_bleed(mic, system, regions, regions)
    assert verdict.bleed
    assert verdict.peak_ncc > 0.9


def test_bleed_check_keeps_independent_tracks() -> None:
    system = _noise(40, seed=1)
    mic = _noise(40, seed=2)
    verdict = ed.detect_loudspeaker_bleed(mic, system, [(2.0, 8.0), (30.0, 35.0)], [(12.0, 25.0)])
    assert not verdict.bleed
    assert verdict.peak_ncc < ed.BLEED_NCC_THRESHOLD
    assert verdict.mic_inside_system_share == 0.0


def test_bleed_check_skips_when_mic_speech_coincides_with_system_speech() -> None:
    system = _noise(40, seed=1)
    mic = _noise(40, seed=2)
    verdict = ed.detect_loudspeaker_bleed(mic, system, [(5.0, 15.0)], [(4.0, 16.0)])
    assert verdict.bleed
    assert verdict.mic_inside_system_share == pytest.approx(1.0)


def test_bleed_thresholds_are_injectable() -> None:
    system = _noise(40, seed=1)
    mic = _noise(40, seed=2)
    regions_mic, regions_sys = [(5.0, 15.0)], [(10.0, 20.0)]  # 50% inside
    assert not ed.detect_loudspeaker_bleed(mic, system, regions_mic, regions_sys).bleed
    assert ed.detect_loudspeaker_bleed(mic, system, regions_mic, regions_sys, overlap_threshold=0.4).bleed


def test_bleed_check_handles_tracks_shorter_than_one_window() -> None:
    short = _noise(2, seed=3)
    verdict = ed.detect_loudspeaker_bleed(short, short, [], [])
    assert verdict.peak_ncc == 0.0 and not verdict.bleed


# ------------------------------------------------------------------ DER and speaker count


def test_score_der_on_hand_written_annotations() -> None:
    pytest.importorskip("pyannote.metrics", reason="DER scoring needs the diar-eval extra")
    reference = [ed.Seg(0, 10, "a"), ed.Seg(10, 20, "b")]
    perfect = [ed.Seg(0, 10, "x"), ed.Seg(10, 20, "y")]  # labels differ, structure identical
    assert ed.score_der(reference, perfect, 20.0).der == pytest.approx(0.0)

    # everything after 15 s attributed to the wrong speaker: 5 s confusion of 20 s, collar excluded
    wrong = [ed.Seg(0, 10, "x"), ed.Seg(10, 15, "y"), ed.Seg(15, 20, "x")]
    score = ed.score_der(reference, wrong, 20.0, collar=0.0)
    assert score.confusion == pytest.approx(0.25)
    assert score.missed == pytest.approx(0.0) and score.false_alarm == pytest.approx(0.0)
    assert score.der == pytest.approx(0.25)


def test_score_der_reports_missed_and_false_alarm() -> None:
    pytest.importorskip("pyannote.metrics", reason="DER scoring needs the diar-eval extra")
    reference = [ed.Seg(0, 10, "a")]
    hypothesis = [ed.Seg(0, 5, "x"), ed.Seg(12, 14, "x")]
    score = ed.score_der(reference, hypothesis, 20.0, collar=0.0)
    assert score.missed == pytest.approx(0.5)
    assert score.false_alarm == pytest.approx(0.2)
    assert score.der == pytest.approx(0.7)


def test_count_speakers() -> None:
    assert ed.count_speakers([ed.Seg(0, 1, "a"), ed.Seg(1, 2, "b"), ed.Seg(2, 3, "a")]) == 2
    assert ed.count_speakers([]) == 0


def test_run_backend_reports_speakers_found_vs_reference_and_writes_rttm(tmp_path: Path) -> None:
    reference = [ed.Seg(0, 10, "self"), ed.Seg(10, 20, "prospect")]
    fake = FakeBackend("fake", [ed.Seg(0, 10, "A"), ed.Seg(10, 15, "B"), ed.Seg(15, 20, "C")])
    audio = np.zeros(20 * SR, dtype=np.float32)
    row, hyp = ed.run_backend(fake, audio, "s1", reference, None, tmp_path)
    assert (row.speakers_found, row.speakers_expected) == (3, 2)
    assert row.rtf is not None and row.seconds is not None
    assert (tmp_path / "s1" / "fake-cpu.rttm").exists()
    assert len(hyp) == 3


def test_run_backend_reports_der_when_the_scorer_is_available(tmp_path: Path) -> None:
    pytest.importorskip("pyannote.metrics", reason="DER scoring needs the diar-eval extra")
    reference = [ed.Seg(0, 10, "self"), ed.Seg(10, 20, "prospect")]
    fake = FakeBackend("fake", [ed.Seg(0, 10, "A"), ed.Seg(10, 15, "B"), ed.Seg(15, 20, "C")])
    row, _ = ed.run_backend(fake, np.zeros(20 * SR, dtype=np.float32), "s1", reference, None, tmp_path)
    assert row.der is not None and row.der > 0


def _scorer_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> None:
        raise ed.ScorerUnavailableError(f"install with {ed.SCORER_INSTALL_HINT}")

    monkeypatch.setattr(ed, "require_scorer", missing)


def test_run_backend_without_the_scorer_leaves_der_unmeasured_and_computes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scorer_missing(monkeypatch)
    reference = [ed.Seg(0, 10, "self"), ed.Seg(10, 20, "prospect")]
    fake = FakeBackend("fake", [ed.Seg(0, 10, "A"), ed.Seg(10, 15, "B"), ed.Seg(15, 20, "C")])
    row, hyp = ed.run_backend(fake, np.zeros(20 * SR, dtype=np.float32), "s1", reference, None, tmp_path)
    assert row.der is None and row.missed is None and "DER not measured" in row.note
    assert (row.speakers_found, row.speakers_expected) == (3, 2)
    assert (tmp_path / "s1" / "fake-cpu.rttm").exists() and len(hyp) == 3


def test_score_der_without_the_extra_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyannote.metrics.diarization", None)
    with pytest.raises(ed.ScorerUnavailableError, match="uv sync --extra diar-eval --extra diarization"):
        ed.score_der([ed.Seg(0, 1, "a")], [ed.Seg(0, 1, "x")], 1.0)


def test_main_with_a_reference_stops_with_one_clear_error_when_the_scorer_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _scorer_missing(monkeypatch)
    rttm = tmp_path / "ref.rttm"
    ed.write_rttm(rttm, "f", [ed.Seg(0, 1, "a")])
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"")
    monkeypatch.setattr(ed, "load_audio", lambda *a, **k: np.zeros(SR, dtype=np.float32))
    monkeypatch.setattr(ed, "build_backends", lambda *a, **k: [FakeBackend("fake", [])])
    out = tmp_path / "out"
    assert ed.main(["--audio", str(audio), "--reference", str(rttm), "--out", str(out)]) == 2
    assert "uv sync --extra diar-eval --extra diarization" in capsys.readouterr().err
    assert not out.exists()


def test_run_backend_oracle_gets_expected_speakers_and_plain_backend_does_not(tmp_path: Path) -> None:
    audio = np.zeros(2 * SR, dtype=np.float32)
    plain, oracle = FakeBackend("p", []), FakeBackend("o", [], oracle=True)
    ed.run_backend(plain, audio, "s1", None, 3, tmp_path)
    ed.run_backend(oracle, audio, "s1", None, 3, tmp_path)
    assert plain.prepared_with == [None]
    assert oracle.prepared_with == [3]


def test_run_backend_skips_oracle_without_a_speaker_count(tmp_path: Path) -> None:
    oracle = FakeBackend("o", [], oracle=True)
    row, hyp = ed.run_backend(oracle, np.zeros(SR, dtype=np.float32), "f", None, None, tmp_path)
    assert row.der is None and "oracle" in row.note and hyp == []
    assert oracle.prepared_with == []


def test_run_backend_survives_a_backend_that_cannot_load(tmp_path: Path) -> None:
    broken = FakeBackend("b", [], fail=True)
    row, hyp = ed.run_backend(broken, np.zeros(SR, dtype=np.float32), "f", None, 2, tmp_path)
    assert row.note == "failed: RuntimeError" and hyp == [] and row.seconds is None
    assert broken.closed


def test_aggregate_mean_median_and_total_rtf() -> None:
    rows = [
        ed.Row("s1", "b", "cpu", der=0.1, seconds=10.0, audio_s=100.0),
        ed.Row("s2", "b", "cpu", der=0.3, seconds=30.0, audio_s=100.0),
        ed.Row("s3", "b", "cpu", der=0.5, seconds=10.0, audio_s=300.0),
        ed.Row("s1", "broken", "cpu", note="failed: RuntimeError", audio_s=100.0),
    ]
    (agg,) = ed.aggregate(rows)
    assert agg["inputs"] == 3
    assert agg["der_mean"] == pytest.approx(0.3)
    assert agg["der_median"] == pytest.approx(0.3)
    assert agg["rtf_total"] == pytest.approx(50.0 / 500.0)


def test_write_table_writes_csv_and_markdown(tmp_path: Path) -> None:
    rows = [ed.Row("s1", "b", "cpu", der=0.1234, speakers_found=2, seconds=1.0, audio_s=10.0)]
    ed.write_table(rows, ed.COLUMNS, tmp_path / "r.csv", tmp_path / "r.md")
    assert "0.123" in (tmp_path / "r.md").read_text()
    assert (tmp_path / "r.csv").read_text().splitlines()[0].startswith("input,backend,device,der")


# ------------------------------------------------------------------ blinded comparison


def test_assign_speaker_picks_largest_overlap() -> None:
    diar = [ed.Seg(0, 3, "a"), ed.Seg(3, 10, "b")]
    assert ed.assign_speaker(ed.TextSeg(1, 6, "x"), diar) == "b"  # 2 s a, 3 s b
    assert ed.assign_speaker(ed.TextSeg(1, 4, "x"), diar) == "a"  # 2 s a, 1 s b
    assert ed.assign_speaker(ed.TextSeg(20, 22, "x"), diar) is None


def test_write_blind_shuffles_letters_and_key_maps_back(tmp_path: Path) -> None:
    transcript = [ed.TextSeg(0, 4, "eerste regel"), ed.TextSeg(65, 70, "tweede regel")]
    outputs = {
        "one": [ed.Seg(0, 4, "S0"), ed.Seg(65, 70, "S1")],
        "two": [ed.Seg(0, 70, "Z")],
        "three": [ed.Seg(0, 4, "Q"), ed.Seg(65, 70, "Q")],
    }
    key = ed.write_blind(tmp_path, transcript, outputs, random.Random(7))
    assert sorted(key) == ["A", "B", "C"] and sorted(key.values()) == ["one", "three", "two"]
    assert json.loads((tmp_path / "key.json").read_text()) == key
    assert sorted(p.name for p in tmp_path.glob("version-*.txt")) == ["version-A.txt", "version-B.txt", "version-C.txt"]

    by_backend = {name: (tmp_path / f"version-{letter}.txt").read_text() for letter, name in key.items()}
    assert by_backend["one"].splitlines() == ["[00:00] SPEAKER_1: eerste regel", "[01:05] SPEAKER_2: tweede regel"]
    assert by_backend["two"].splitlines() == ["[00:00] SPEAKER_1: eerste regel", "[01:05] SPEAKER_1: tweede regel"]
    # the file text never names the backend
    assert not any(name in text for name, text in by_backend.items())


def test_write_blind_order_depends_on_the_rng(tmp_path: Path) -> None:
    outputs = {f"b{i}": [ed.Seg(0, 1, "x")] for i in range(6)}
    orders = set()
    for seed in range(5):
        key = ed.write_blind(tmp_path / str(seed), [ed.TextSeg(0, 1, "t")], outputs, random.Random(seed))
        orders.add(tuple(key.values()))
    assert len(orders) > 1


def test_load_transcript_accepts_whisper_json_and_plain_list(tmp_path: Path) -> None:
    whisper = tmp_path / "w.json"
    segments = [{"start": 1, "end": 2, "text": " hoi "}, {"start": 3, "end": 4, "text": " "}]
    whisper.write_text(json.dumps({"text": "x", "segments": segments}))
    plain = tmp_path / "p.json"
    plain.write_text(json.dumps([{"start": 1.5, "end": 2.5, "text": "dag"}]))
    assert ed.load_transcript(whisper) == [ed.TextSeg(1.0, 2.0, "hoi")]
    assert ed.load_transcript(plain) == [ed.TextSeg(1.5, 2.5, "dag")]


# ------------------------------------------------------------------ privacy guard


def test_out_guard_refuses_repo_output_for_outside_input(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "results").mkdir(parents=True)
    outside = tmp_path / "customer" / "call.wav"
    with pytest.raises(ValueError, match="inside the repository"):
        ed.check_output_dir(repo / "results", [outside], repo_root=repo)


def test_out_guard_allows_outside_output_and_inside_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "customer" / "call.wav"
    ed.check_output_dir(tmp_path / "scratch", [outside], repo_root=repo)
    ed.check_output_dir(repo / "results", [repo / "data" / "x.wav"], repo_root=repo)


def test_out_guard_resolves_dot_dot_escapes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError):
        ed.check_output_dir(repo / "a" / ".." / "b", [tmp_path / "elsewhere.wav"], repo_root=repo)


def test_main_refuses_out_inside_repo_for_outside_audio(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audio = tmp_path / "call.wav"
    audio.write_bytes(b"")
    code = ed.main(["--audio", str(audio), "--out", str(ed.REPO_ROOT / "claudedocs" / "diar-out")])
    assert code == 2
    assert "inside the repository" in capsys.readouterr().err


def test_build_backends_parses_modes_and_rejects_unknown() -> None:
    backends = ed.build_backends("sherpa:eres2net,sherpa:wespeaker@oracle,pyannote-3.1", ["cpu"], Path("/x"), 0.5, 1)
    assert [(b.name, b.oracle) for b in backends] == [
        ("sherpa:eres2net", False),
        ("sherpa:wespeaker@oracle", True),
        ("pyannote-3.1", False),
    ]
    with pytest.raises(ValueError):
        ed.build_backends("nope", ["cpu"], Path("/x"), 0.5, 1)
    with pytest.raises(ValueError):
        ed.build_backends("sherpa:eres2net@weird", ["cpu"], Path("/x"), 0.5, 1)
    assert ed.build_backends("sherpa:eres2net", ["mps"], Path("/x"), 0.5, 1) == []  # no MPS provider: skipped
