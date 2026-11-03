import pytest

from packages.orchestration_kit.state_machine import (
    assert_run_action_allowed,
    run_state_transition_table,
)


def test_run_state_transition_table_exposes_core_actions() -> None:
    table = run_state_transition_table()
    actions = {entry["action"] for entry in table}

    assert {
        "pause",
        "resume",
        "cancel",
        "rerun_stage",
        "rerun_artifact",
        "skip_optional_stage",
    }.issubset(actions)
    pause = next(entry for entry in table if entry["action"] == "pause")
    assert "running" in pause["from_statuses"]
    assert pause["to_status"] == "paused"


def test_run_state_transition_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="cannot pause run from status completed"):
        assert_run_action_allowed("pause", "completed")

    with pytest.raises(ValueError, match="unsupported run state action"):
        assert_run_action_allowed("explode", "running")
