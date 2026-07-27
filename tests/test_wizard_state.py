from pathlib import Path

from sales_copilot.wizard.state import WizardState, load_state, save_state


def test_fresh_state_is_empty() -> None:
    state = WizardState()
    assert state.completed == []
    assert state.results == {}
    assert state.env_written is False
    assert state.is_completed("mic") is False


def test_mark_completed_tracks_steps_and_results() -> None:
    state = WizardState()
    state.mark_completed("mic", {"ok": True, "message": "mic ok"})

    assert state.is_completed("mic") is True
    assert state.results["mic"] == {"ok": True, "message": "mic ok", "details": {}}


def test_load_and_save_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "wizard_state.json"
    state = WizardState(completed=["mic", "route"], results={"mic": {"ok": True}}, env_written=True)

    save_state(state, path)
    loaded = load_state(path)

    assert loaded.completed == ["mic", "route"]
    assert loaded.results == {"mic": {"ok": True}}
    assert loaded.env_written is True


def test_load_missing_file_returns_fresh_state(tmp_path: Path) -> None:
    path = tmp_path / "does_not_exist.json"
    loaded = load_state(path)
    assert loaded == WizardState()


def test_load_corrupt_file_returns_fresh_state(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("not json")
    loaded = load_state(path)
    assert loaded == WizardState()


def test_load_non_dict_json_returns_fresh_state(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    loaded = load_state(path)
    assert loaded == WizardState()
