from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base


def test_metadata_contains_core_tables() -> None:
    tables = set(Base.metadata.tables)

    assert "projects" in tables
    assert "production_runs" in tables
    assert "stage_runs" in tables
    assert "artifacts" in tables
    assert "tool_invocations" in tables
    assert "event_logs" in tables
    assert "background_jobs" in tables


def test_metadata_contains_review_tables() -> None:
    tables = set(Base.metadata.tables)

    assert "review_requests" in tables
    assert "review_decisions" in tables
    assert "review_comments" in tables
    assert "review_action_items" in tables


def test_stage_runs_carries_invalidation_markers() -> None:
    columns = set(Base.metadata.tables["stage_runs"].columns.keys())

    assert "invalidated_at" in columns
    assert "invalidated_by_stage_key" in columns
    assert "invalidation_reason" in columns


def test_review_request_binds_stage_run_and_artifact_scope() -> None:
    columns = set(Base.metadata.tables["review_requests"].columns.keys())

    assert "stage_run_id" in columns
    assert "artifact_scope" in columns
    assert "review_focus" in columns
    assert "success_criteria" in columns
    assert "iteration" in columns
    assert "assignee_user_id" in columns


def test_review_decision_has_no_mutable_current_marker() -> None:
    columns = set(Base.metadata.tables["review_decisions"].columns.keys())

    assert "sequence" in columns
    assert "decision" in columns
    assert "artifact_refs" in columns
    assert "is_current" not in columns
    assert "updated_by" not in columns
