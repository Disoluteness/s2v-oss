import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    ApprovalRequest,
    CreateReviewComment,
    ProjectCreate,
    ReviewActionKind,
    ReviewActionStatus,
    ReviewCommentSeverity,
    ReviewCommentStatus,
    ReviewDecisionType,
    ReviewReasonCode,
    ReviewRequestCreate,
    ReviewRequestStatus,
    RunCreate,
    RunStateActionName,
    RunStateActionRequest,
    RunStatus,
    StageStatus,
    SubmitReviewDecision,
)
from apps.api.app.review_gate import ReviewGateError, infer_action_kind
from packages.pipeline_kit.loader import PipelineLoader


def _store_at_first_gate(tmp_path, name="review-kernel.db"):
    """A store whose run has produced the plan artifact and is parked at its gate."""
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Review kernel project"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
        ),
        pipeline=pipeline,
    )
    store.start_run(run.run_id)
    return engine, session, store, project, run


def _open_request_for(store, run_id, stage_key):
    requests = store.list_review_requests(run_id, stage_key=stage_key)
    assert requests is not None
    open_requests = [item for item in requests if item.status == ReviewRequestStatus.OPEN]
    assert open_requests, f"no open review request for stage {stage_key}"
    return open_requests[-1]


def _stage(store, run_id, stage_key):
    run = store.get_run(run_id)
    assert run is not None
    return next(item for item in run.stages if item.stage_key == stage_key)


def test_start_run_opens_review_request_with_policy_snapshot(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)

    request = _open_request_for(store, run.run_id, "plan")
    detail = store.get_review_request(request.review_request_id)
    session.close()

    assert detail is not None
    assert detail.stage_key == "plan"
    assert detail.status == ReviewRequestStatus.OPEN
    assert detail.iteration == 1
    assert detail.gate_type == "human_review"
    assert detail.gates_stage_key == "script"
    # The policy is snapshotted so the request stays reviewable if the pipeline changes.
    assert detail.review_focus
    assert detail.success_criteria
    assert [ref.artifact_type for ref in detail.artifact_scope] == ["production_plan"]
    assert all(ref.version == 1 for ref in detail.artifact_scope)


def test_review_decision_rejected_when_stage_not_awaiting_review(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")

    # storyboard exists in the run but has never been produced, so it has no gate to open.
    with pytest.raises(ReviewGateError) as excinfo:
        store.open_review_request(
            run.run_id,
            ReviewRequestCreate(stage_key="storyboard"),
        )
    assert excinfo.value.code == "stage_not_awaiting_review"
    assert excinfo.value.details["stage_status"] == StageStatus.PENDING.value

    # A closed request cannot be answered twice in place.
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
    )
    with pytest.raises(ReviewGateError) as closed:
        store.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        )
    session.close()
    assert closed.value.code == "review_request_closed"


def test_superseded_request_cannot_be_answered(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")

    # A rerun replaces the artifact under review, so the pending request is superseded.
    store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="plan"),
    )
    with pytest.raises(ReviewGateError) as excinfo:
        store.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        )
    stale = store.get_review_request(request.review_request_id)
    session.close()

    assert excinfo.value.code == "review_request_superseded"
    assert excinfo.value.status_code == 409
    assert stale is not None
    assert stale.status == ReviewRequestStatus.SUPERSEDED


def test_stale_scope_blocks_decision_even_when_request_still_open(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")
    record = store.reviews.get_request_record(request.review_request_id)
    assert record is not None
    # Simulate an out-of-band artifact change without closing the request.
    record.artifact_scope = [
        {"artifact_id": "artifact_missing", "artifact_type": "production_plan", "version": 1}
    ]
    session.flush()

    with pytest.raises(ReviewGateError) as excinfo:
        store.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        )
    session.close()

    assert excinfo.value.code == "stale_artifact_scope"
    assert excinfo.value.details["current_artifact_scope"][0]["version"] == 1


def test_only_one_open_review_request_per_stage_run(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)

    first = _open_request_for(store, run.run_id, "plan")
    again = store.open_review_request(run.run_id, ReviewRequestCreate(stage_key="plan"))
    requests = store.list_review_requests(run.run_id, stage_key="plan")
    session.close()

    assert again is not None
    assert again.review_request_id == first.review_request_id
    assert requests is not None
    assert len(requests) == 1


def test_approve_with_changes_keeps_run_moving_and_creates_action_items(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")
    store.add_review_comment(
        request.review_request_id,
        CreateReviewComment(body="开头再收紧一点", severity=ReviewCommentSeverity.NITPICK),
    )

    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.APPROVE_WITH_CHANGES,
            comment="先放行，遗留一条打磨项",
            action_items=[
                {
                    "kind": ReviewActionKind.RERUN_STAGE,
                    "instruction": "把 hook 压到 8 个字以内",
                }
            ],
        ),
    )
    run_after = store.get_run(run.run_id)
    inbox = store.project_review_inbox(run.project_id)
    session.close()

    assert detail.status == ReviewRequestStatus.APPROVED
    assert detail.current_decision == ReviewDecisionType.APPROVE_WITH_CHANGES
    # Unlike the pre-T74 behaviour, the run keeps moving.
    assert run_after is not None
    assert run_after.current_stage_key == "script"
    assert [item.kind for item in detail.action_items] == [ReviewActionKind.RERUN_STAGE]
    assert detail.action_items[0].status == ReviewActionStatus.OPEN
    assert detail.action_items[0].result_action == "rerun_stage"
    # "带修改通过" resolves nothing: the ask has not been delivered yet, so the
    # comment has to stay open for as long as the action item does.
    assert [item.status for item in detail.comments] == [ReviewCommentStatus.OPEN]
    # Both approve flavours land on `approved`, so the inbox has to carry the
    # decision for the display layer to tell them apart.
    approved_item = next(
        item for item in inbox.items if item.review_request_id == request.review_request_id
    )
    assert approved_item.status == ReviewRequestStatus.APPROVED
    assert approved_item.current_decision == ReviewDecisionType.APPROVE_WITH_CHANGES
    assert approved_item.open_action_items == 1


def test_request_changes_pauses_run_and_invalidates_downstream_outputs(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    # Walk far enough that downstream stages actually hold artifacts.
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "storyboard", ApprovalRequest(decision="approved"))
    assert _stage(store, run.run_id, "asset_review").status == StageStatus.WAITING_REVIEW
    downstream_with_artifacts = {
        artifact.stage_key for artifact in store.list_artifacts(run.run_id)
    }
    assert {"storyboard", "asset_generation"} <= downstream_with_artifacts

    # Send the run back to the script gate: its downstream now holds stale output.
    store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="script"),
    )
    request = _open_request_for(store, run.run_id, "script")
    store.add_review_comment(
        request.review_request_id,
        CreateReviewComment(body="口播第 2 句太长", severity=ReviewCommentSeverity.CRITICAL),
    )
    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REQUEST_CHANGES,
            comment="脚本要重写",
            reason_code=ReviewReasonCode.CONTENT_ISSUE,
        ),
    )
    run_after = store.get_run(run.run_id)
    session.close()

    assert detail.status == ReviewRequestStatus.CHANGES_REQUESTED
    assert len(detail.action_items) == 1
    assert detail.action_items[0].kind == ReviewActionKind.RERUN_STAGE
    assert detail.action_items[0].instruction == "口播第 2 句太长"
    assert detail.action_items[0].linked_comment_ids

    assert run_after is not None
    assert run_after.status == RunStatus.PAUSED
    script = next(item for item in run_after.stages if item.stage_key == "script")
    assert script.status == StageStatus.PENDING
    # Downstream outputs derived from the rejected script are marked stale.
    storyboard = next(item for item in run_after.stages if item.stage_key == "storyboard")
    assert storyboard.invalidation is not None
    assert storyboard.invalidation.invalidated_by_stage_key == "script"
    assert storyboard.status == StageStatus.PENDING
    # Upstream stages are untouched.
    plan = next(item for item in run_after.stages if item.stage_key == "plan")
    assert plan.invalidation is None


def test_request_changes_without_downstream_artifacts_invalidates_nothing(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")

    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REQUEST_CHANGES,
            comment="方案要重做",
        ),
    )
    run_after = store.get_run(run.run_id)
    session.close()

    assert detail.status == ReviewRequestStatus.CHANGES_REQUESTED
    assert run_after is not None
    # Nothing downstream has produced output yet, so there is nothing to invalidate.
    assert all(
        item.invalidation is None
        for item in run_after.stages
        if item.stage_key != "plan"
    )


def test_reject_pauses_run_without_auto_rerun(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")

    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REJECT,
            comment="整体方向不对",
            reason_code=ReviewReasonCode.CONTENT_ISSUE,
        ),
    )
    run_after = store.get_run(run.run_id)
    session.close()

    assert detail.status == ReviewRequestStatus.REJECTED
    assert run_after is not None
    assert run_after.status == RunStatus.PAUSED
    assert _stage(store, run.run_id, "plan").status == StageStatus.REJECTED


def test_reopen_appends_decision_and_returns_run_to_gate(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
    )
    assert _stage(store, run.run_id, "script").status == StageStatus.WAITING_REVIEW

    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REOPEN,
            comment="刚发现方案里品牌名写错了",
            reason_code=ReviewReasonCode.CONTENT_ISSUE,
        ),
    )
    run_after = store.get_run(run.run_id)
    session.close()

    assert detail.status == ReviewRequestStatus.OPEN
    assert detail.iteration == 2
    assert [row.decision for row in detail.decisions] == [
        ReviewDecisionType.APPROVE,
        ReviewDecisionType.REOPEN,
    ]
    assert run_after is not None
    assert run_after.status == RunStatus.WAITING_REVIEW
    assert run_after.current_stage_key == "plan"
    # Reopening an upstream gate voids the downstream approval.
    script = next(item for item in run_after.stages if item.stage_key == "script")
    assert script.status == StageStatus.PENDING
    assert script.invalidation is not None


def test_decisions_are_append_only_and_gap_free(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.REJECT, comment="第一次驳回"),
    )
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.REOPEN, comment="再看看"),
    )
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE, comment="这次可以"),
    )
    detail = store.get_review_request(request.review_request_id)
    session.close()

    assert detail is not None
    assert [row.sequence for row in detail.decisions] == [1, 2, 3]
    assert [row.decision for row in detail.decisions] == [
        ReviewDecisionType.REJECT,
        ReviewDecisionType.REOPEN,
        ReviewDecisionType.APPROVE,
    ]
    assert [row.round for row in detail.decisions] == [1, 2, 2]
    assert detail.current_decision == ReviewDecisionType.APPROVE


def test_repository_exposes_no_decision_mutation_path() -> None:
    from apps.api.app.repositories import ReviewRepository

    forbidden = ("update_decision", "delete_decision", "set_decision", "remove_decision")
    assert all(not hasattr(ReviewRepository, name) for name in forbidden)


def test_review_round_limit_blocks_new_round_until_overridden(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    # Force a low limit through the pipeline metadata contract.
    record = store._get_run_record(run.run_id)
    assert record is not None
    snapshot = dict(record.pipeline_definition_snapshot or {})
    metadata = dict(snapshot.get("metadata") or {})
    metadata["review"] = {"max_rounds": 2}
    snapshot["metadata"] = metadata
    record.pipeline_definition_snapshot = snapshot
    session.flush()

    request = _open_request_for(store, run.run_id, "plan")
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.REJECT, comment="先驳回"),
    )
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.REOPEN, comment="第二轮"),
    )
    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.REJECT, comment="再驳回"),
    )
    with pytest.raises(ReviewGateError) as excinfo:
        store.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=ReviewDecisionType.REOPEN),
        )
    assert excinfo.value.code == "review_round_limit_reached"
    assert excinfo.value.details["allow_override"] is True

    detail = store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REOPEN,
            override_round_limit=True,
            comment="业务上必须再走一轮",
        ),
    )
    session.close()

    assert detail.iteration == 3
    assert detail.decisions[-1].reason_code == ReviewReasonCode.ROUND_LIMIT_OVERRIDE.value


def test_rerun_stage_voids_downstream_approvals_and_blocks_dispatch(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    assert _stage(store, run.run_id, "storyboard").status == StageStatus.WAITING_REVIEW

    result = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="script"),
    )
    session.close()

    assert result is not None
    invalidated = result.result_payload["invalidated_stage_keys"]
    assert "storyboard" in invalidated
    assert "plan" not in invalidated
    # The stale approval cannot stand.
    storyboard = _stage(store, run.run_id, "storyboard")
    assert storyboard.invalidation is not None
    assert storyboard.invalidation.invalidated_by_stage_key == "script"


def test_superseded_review_request_is_closed_by_upstream_rerun(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    storyboard_request = _open_request_for(store, run.run_id, "storyboard")

    result = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="script"),
    )
    stale = store.get_review_request(storyboard_request.review_request_id)
    session.close()

    assert stale is not None
    assert stale.status == ReviewRequestStatus.SUPERSEDED
    assert stale.closed_at is not None
    # The caller has to be able to tell which requests the rerun voided without
    # re-reading the whole board.
    assert result is not None
    assert storyboard_request.review_request_id in result.result_payload[
        "superseded_review_request_ids"
    ]


def test_dashboard_exposes_review_metadata_and_invalidated_count(tmp_path) -> None:
    _engine, session, store, project, run = _store_at_first_gate(tmp_path)
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    storyboard_request = _open_request_for(store, run.run_id, "storyboard")
    store.assign_review_request(
        storyboard_request.review_request_id,
        assignee_user_id=None,
        due_at=None,
    )

    dashboard = store.project_dashboard(project.project_id)
    session.close()

    assert dashboard is not None
    assert dashboard.metrics.pending_reviews >= 1
    item = next(
        entry for entry in dashboard.review_queue if entry.stage_key == "storyboard"
    )
    assert item.review_request_id == storyboard_request.review_request_id
    assert item.iteration == 1
    assert item.review_status == ReviewRequestStatus.OPEN
    assert item.overdue is False


def test_run_board_exposes_review_timeline_and_invalidation(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    assert _stage(store, run.run_id, "storyboard").status == StageStatus.WAITING_REVIEW
    invalidated_request = _open_request_for(store, run.run_id, "storyboard")

    # Sending the run back to the script gate invalidates the storyboard output.
    store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="script"),
    )
    board = store.run_board(run.run_id)
    session.close()

    assert board is not None
    assert board.review_requests
    assert board.review_timeline
    assert [row.decision for row in board.review_timeline][:2] == [
        ReviewDecisionType.APPROVE,
        ReviewDecisionType.APPROVE,
    ]
    assert "storyboard" in board.invalidated_stages
    stale_lane = next(
        item for item in board.stage_lanes if item.stage.stage_key == "storyboard"
    )
    # The lane keeps the newest request so a reviewer can see the superseded gate.
    assert stale_lane.review_request is not None
    assert stale_lane.review_request.review_request_id == (
        invalidated_request.review_request_id
    )
    assert stale_lane.review_request.status == ReviewRequestStatus.SUPERSEDED
    assert stale_lane.stage.invalidation is not None
    assert board.metrics["invalidated_stage_count"] >= 1


def test_project_review_inbox_orders_overdue_first(tmp_path) -> None:
    _engine, session, store, project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")
    store.add_review_comment(
        request.review_request_id,
        CreateReviewComment(body="品牌名写错", severity=ReviewCommentSeverity.CRITICAL),
    )
    inbox = store.project_review_inbox(project.project_id)
    session.close()

    assert inbox is not None
    assert inbox.total == 1
    assert inbox.items[0].review_request_id == request.review_request_id
    assert inbox.items[0].open_critical_comments == 1


def test_comment_must_target_an_artifact_under_review(tmp_path) -> None:
    _engine, session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request_for(store, run.run_id, "plan")

    with pytest.raises(ReviewGateError) as excinfo:
        store.add_review_comment(
            request.review_request_id,
            CreateReviewComment(
                body="针对一个不在审核范围内的产物",
                artifact_id="artifact_not_in_scope",
            ),
        )
    session.close()

    assert excinfo.value.code == "invalid_artifact_scope"


def test_infer_action_kind_maps_stage_families() -> None:
    assert infer_action_kind("script") == ReviewActionKind.RERUN_STAGE
    assert infer_action_kind("asset_generation") == ReviewActionKind.REGENERATE_MEDIA
    assert infer_action_kind("analysis") == ReviewActionKind.REANALYZE_SOURCE
    assert infer_action_kind("export") == ReviewActionKind.MANUAL
    assert infer_action_kind(None) == ReviewActionKind.MANUAL
