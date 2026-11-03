"""API-contract tests for the T74 review surface.

The other suites exercise the review *kernel*. This one exercises the HTTP
layer: that the eight new endpoints are registered with the right methods and
response models, that a ``ReviewGateError`` is translated into a machine-readable
``{code, message, ...}`` detail instead of a bare 409, and that the pre-T74
one-click review endpoint keeps its external shape while now writing a decision.

``httpx`` is not a dependency of this project, so ``fastapi.testclient`` is not
available (see ``tests/test_web_workspace.py`` for the same constraint). The
route callables are therefore invoked directly with a real store, which still
covers the dependency-injection-independent part of the contract: the request
model in and the exception mapping out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models, routes  # noqa: F401  (db_models registers the tables)
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.models import (
    ApprovalRequest,
    AssignReviewRequest,
    CreateReviewComment,
    ProjectCreate,
    ReviewActionItemInput,
    ReviewActionKind,
    ReviewCommentSeverity,
    ReviewCommentStatus,
    ReviewDecisionType,
    ReviewRequestCreate,
    ReviewRequestStatus,
    RunCreate,
    RunStateActionName,
    RunStateActionRequest,
    SubmitReviewDecision,
    UpdateReviewComment,
)
from apps.api.app.review_gate import ReviewGateError
from packages.pipeline_kit.loader import PipelineLoader

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _store_at_first_gate(tmp_path, name="review-api.db"):
    """A store whose run produced the plan artifact and parked at its gate."""
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Review API project"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
        ),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )
    store.start_run(run.run_id)
    return session, store, project, run


def _open_request(store, run_id, stage_key="plan"):
    requests = store.list_review_requests(run_id, stage_key=stage_key)
    assert requests
    return next(item for item in requests if item.status == ReviewRequestStatus.OPEN)


def _first_artifact(detail):
    assert detail.artifact_scope
    return detail.artifact_scope[0]


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

NEW_REVIEW_ENDPOINTS = {
    "/api/runs/{run_id}/review-requests": {"get", "post"},
    "/api/review-requests/{review_request_id}": {"get"},
    "/api/review-requests/{review_request_id}/decisions": {"post"},
    "/api/review-requests/{review_request_id}/comments": {"post"},
    "/api/review-comments/{comment_id}": {"patch"},
    "/api/review-requests/{review_request_id}/assign": {"post"},
    "/api/projects/{project_id}/review-inbox": {"get"},
}


def test_review_endpoints_are_registered() -> None:
    paths = app.openapi()["paths"]

    for path, methods in NEW_REVIEW_ENDPOINTS.items():
        assert path in paths, f"missing review endpoint: {path}"
        for method in methods:
            assert method in paths[path], f"{path} is missing {method.upper()}"

    # The pre-T74 endpoint stays registered so existing clients keep working.
    legacy = "/api/runs/{run_id}/stages/{stage_key}/review"
    assert legacy in paths
    assert "post" in paths[legacy]


def test_review_request_list_query_params_are_typed() -> None:
    """Filters are enums, so an unknown status is rejected by validation, not ignored."""
    schema = app.openapi()
    params = schema["paths"]["/api/runs/{run_id}/review-requests"]["get"]["parameters"]
    by_name = {item["name"]: item for item in params}

    assert set(by_name) >= {"run_id", "status", "stage_key"}
    assert by_name["run_id"]["in"] == "path"
    assert by_name["status"]["in"] == "query"
    # Optional params are published as ``anyOf: [<enum>, null]``.
    variants = by_name["status"]["schema"]["anyOf"]
    ref = next(item["$ref"] for item in variants if "$ref" in item)
    assert any(item.get("type") == "null" for item in variants)

    component = ref.rsplit("/", 1)[-1]
    assert component == "ReviewRequestStatus"
    assert set(schema["components"]["schemas"][component]["enum"]) == {
        "open",
        "approved",
        "changes_requested",
        "rejected",
        "cancelled",
        "superseded",
    }


def test_review_detail_response_models_are_published() -> None:
    components = app.openapi()["components"]["schemas"]

    for name in (
        "ReviewRequest",
        "ReviewRequestDetail",
        "ReviewDecision",
        "ReviewComment",
        "ReviewActionItem",
        "ReviewInbox",
        "ReviewInboxItem",
    ):
        assert name in components, f"{name} is not part of the public contract"

    # The decision contract must expose the append-only ordering fields.
    decision_fields = components["ReviewDecision"]["properties"]
    assert {"sequence", "round", "decision", "actor_user_id"} <= set(decision_fields)

    # ...and the detail contract must expose the collaboration collections.
    detail_fields = components["ReviewRequestDetail"]["properties"]
    assert {"decisions", "comments", "action_items", "workspace_url"} <= set(detail_fields)

    # Both approve flavours end on the status `approved`, so every queue-shaped
    # contract has to carry the decision as well.
    for name in ("ReviewRequest", "ReviewInboxItem", "DashboardReviewItem"):
        assert "current_decision" in components[name]["properties"], (
            f"{name} cannot distinguish approve from approve_with_changes"
        )


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def test_gate_error_maps_to_code_bearing_http_detail(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)

    # storyboard exists in the pipeline but has never been produced.
    with pytest.raises(HTTPException) as excinfo:
        routes.open_review_request(
            run.run_id,
            ReviewRequestCreate(stage_key="storyboard"),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "stage_not_awaiting_review"
    assert excinfo.value.detail["stage_key"] == "storyboard"
    assert excinfo.value.detail["stage_status"] == "pending"
    assert excinfo.value.detail["message"]


def test_unknown_review_request_is_a_plain_404(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.get_review_request("rq_missing", store)
    session.close()

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "review request not found"


def test_unknown_run_on_review_list_is_a_404(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.list_review_requests("run_missing", store)
    session.close()

    assert excinfo.value.status_code == 404


def test_deciding_a_closed_request_is_a_code_bearing_409(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        store,
    )
    with pytest.raises(HTTPException) as excinfo:
        routes.submit_review_decision(
            request.review_request_id,
            SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "review_request_closed"
    assert excinfo.value.detail["review_status"] == "approved"


def test_gate_error_payload_is_json_safe(tmp_path) -> None:
    """The detail dict is serialized straight into the response body."""
    session, store, _project, run = _store_at_first_gate(tmp_path)
    with pytest.raises(HTTPException) as excinfo:
        routes.open_review_request(
            run.run_id,
            ReviewRequestCreate(stage_key="storyboard"),
            store,
        )
    session.close()

    assert json.loads(json.dumps(excinfo.value.detail)) == excinfo.value.detail


def test_review_gate_error_defaults() -> None:
    """Unknown codes still carry a message, so the UI always has something to show."""
    error = ReviewGateError("some_new_code", "explain me")

    assert error.status_code == 409
    assert error.to_payload() == {"code": "some_new_code", "message": "explain me"}


# ---------------------------------------------------------------------------
# happy path: open -> read -> decide
# ---------------------------------------------------------------------------


def test_open_review_request_route_returns_reviewable_detail(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    existing = _open_request(store, run.run_id)

    detail = routes.get_review_request(existing.review_request_id, store)
    session.close()

    assert detail.stage_key == "plan"
    assert detail.status == ReviewRequestStatus.OPEN
    assert detail.iteration == 1
    assert detail.gate_type == "human_review"
    # The policy is snapshotted on the request so it stays reviewable if the
    # pipeline definition changes underneath it.
    assert detail.review_focus
    assert detail.success_criteria
    # ...and the response carries deep links the Web layer can use directly.
    assert detail.workspace_url == f"/workspace?review_request_id={detail.review_request_id}"
    assert detail.run_board_url == f"/run-board?review_request_id={detail.review_request_id}"


def test_open_review_request_route_is_idempotent(tmp_path) -> None:
    """A double-click must not burn a review round or fork the decision history."""
    session, store, _project, run = _store_at_first_gate(tmp_path)
    existing = _open_request(store, run.run_id)

    again = routes.open_review_request(
        run.run_id,
        ReviewRequestCreate(
            stage_key="plan",
            assignee_user_id="user_reviewer",
            due_at=datetime.now(UTC) + timedelta(days=2),
            note="please check the hook",
        ),
        store,
    )
    all_requests = routes.list_review_requests(run.run_id, store)
    session.close()

    assert again.review_request_id == existing.review_request_id
    assert again.iteration == existing.iteration == 1
    assert len(all_requests) == 1


def test_list_review_requests_route_filters_by_status_and_stage(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    open_only = routes.list_review_requests(
        run.run_id, store, status=ReviewRequestStatus.OPEN
    )
    by_stage = routes.list_review_requests(run.run_id, store, stage_key="plan")
    other_stage = routes.list_review_requests(run.run_id, store, stage_key="storyboard")
    approved_only = routes.list_review_requests(
        run.run_id, store, status=ReviewRequestStatus.APPROVED
    )

    routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        store,
    )
    after_approval = routes.list_review_requests(
        run.run_id, store, status=ReviewRequestStatus.APPROVED
    )
    session.close()

    assert [item.review_request_id for item in open_only] == [request.review_request_id]
    assert [item.review_request_id for item in by_stage] == [request.review_request_id]
    assert other_stage == []
    assert approved_only == []
    assert [item.review_request_id for item in after_approval] == [request.review_request_id]


def test_submit_decision_route_returns_append_only_log(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    first = routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REQUEST_CHANGES,
            comment="第一句钩子不够强",
            actor_display_name="李质控",
        ),
        store,
    )
    assert first.status == ReviewRequestStatus.CHANGES_REQUESTED
    assert [item.decision for item in first.decisions] == [ReviewDecisionType.REQUEST_CHANGES]
    assert [item.sequence for item in first.decisions] == [1]
    assert first.decisions[0].actor_display_name == "李质控"

    # Reopening appends rather than mutating, so history is never rewritten.
    reopened = routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.REOPEN,
            comment="上一轮的判断有误，重新看",
            actor_display_name="李质控",
        ),
        store,
    )
    session.close()

    assert reopened.status == ReviewRequestStatus.OPEN
    assert [item.decision for item in reopened.decisions] == [
        ReviewDecisionType.REQUEST_CHANGES,
        ReviewDecisionType.REOPEN,
    ]
    assert [item.sequence for item in reopened.decisions] == [1, 2]


def test_decision_with_action_items_route_creates_trackable_work(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    detail = routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.APPROVE_WITH_CHANGES,
            comment="整体可用，但需要收紧结尾",
            action_items=[
                ReviewActionItemInput(
                    kind=ReviewActionKind.RERUN_STAGE,
                    stage_key="plan",
                    instruction="结尾 CTA 缩短到 5 秒内",
                )
            ],
        ),
        store,
    )
    session.close()

    assert detail.status == ReviewRequestStatus.APPROVED
    assert detail.current_decision == ReviewDecisionType.APPROVE_WITH_CHANGES
    assert len(detail.action_items) == 1
    item = detail.action_items[0]
    assert item.instruction == "结尾 CTA 缩短到 5 秒内"
    # The action item reuses the existing run-state action contract.
    assert item.result_action == "rerun_stage"


# ---------------------------------------------------------------------------
# comments
# ---------------------------------------------------------------------------


def test_comment_route_creates_and_resolves(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)
    artifact = _first_artifact(store.get_review_request(request.review_request_id))

    comment = routes.add_review_comment(
        request.review_request_id,
        CreateReviewComment(
            body="品牌名和口播稿里的写法不一致",
            severity=ReviewCommentSeverity.CRITICAL,
            artifact_id=artifact.artifact_id,
            artifact_version=artifact.version,
            author_display_name="王制作",
        ),
        store,
    )
    assert comment.stage_key == "plan"
    assert comment.status == ReviewCommentStatus.OPEN
    assert comment.artifact_version == artifact.version

    resolved = routes.update_review_comment(
        comment.comment_id,
        UpdateReviewComment(status=ReviewCommentStatus.RESOLVED),
        store,
    )
    detail = routes.get_review_request(request.review_request_id, store)
    session.close()

    assert resolved.status == ReviewCommentStatus.RESOLVED
    assert [item.comment_id for item in detail.comments] == [comment.comment_id]


def test_comment_on_foreign_artifact_is_rejected(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    with pytest.raises(HTTPException) as excinfo:
        routes.add_review_comment(
            request.review_request_id,
            CreateReviewComment(
                body="指向一个不属于本次审核版本的产物",
                artifact_id="art_not_under_review",
                artifact_version=1,
            ),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "invalid_artifact_scope"


def test_comment_route_404s_on_unknown_request(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.add_review_comment(
            "rq_missing",
            CreateReviewComment(body="无处安放的评论"),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------


def test_assign_route_sets_owner_and_flags_overdue(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)

    assigned = routes.assign_review_request(
        request.review_request_id,
        AssignReviewRequest(
            assignee_user_id="user_alice",
            due_at=datetime.now(UTC) - timedelta(hours=3),
        ),
        store,
    )
    session.close()

    assert assigned.assignee_user_id == "user_alice"
    assert assigned.overdue is True


def test_assign_route_404s_on_unknown_request(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.assign_review_request(
            "rq_missing",
            AssignReviewRequest(assignee_user_id="user_alice"),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


def test_review_inbox_route_orders_overdue_first_and_counts(tmp_path) -> None:
    session, store, project, run = _store_at_first_gate(tmp_path)
    request = _open_request(store, run.run_id)
    artifact = _first_artifact(store.get_review_request(request.review_request_id))

    routes.assign_review_request(
        request.review_request_id,
        AssignReviewRequest(
            assignee_user_id="user_alice",
            due_at=datetime.now(UTC) - timedelta(hours=1),
        ),
        store,
    )
    routes.add_review_comment(
        request.review_request_id,
        CreateReviewComment(
            body="必须有字幕",
            severity=ReviewCommentSeverity.CRITICAL,
            artifact_id=artifact.artifact_id,
            artifact_version=artifact.version,
        ),
        store,
    )

    inbox = routes.project_review_inbox(project.project_id, store)
    mine = routes.project_review_inbox(project.project_id, store, assignee_user_id="user_alice")
    others = routes.project_review_inbox(project.project_id, store, assignee_user_id="user_bob")
    by_stage = routes.project_review_inbox(project.project_id, store, stage_key="plan")
    session.close()

    assert inbox.total == 1
    assert inbox.overdue_total == 1
    assert inbox.status_counts == {"open": 1}
    item = inbox.items[0]
    assert item.overdue is True
    assert item.open_critical_comments == 1
    assert item.workspace_url == f"/workspace?review_request_id={item.review_request_id}"

    assert [entry.review_request_id for entry in mine.items] == [request.review_request_id]
    assert others.total == 0
    assert by_stage.total == 1


def test_review_inbox_hides_superseded_requests(tmp_path) -> None:
    session, store, project, run = _store_at_first_gate(tmp_path)

    # Approving the plan gate releases the run to the script gate, which opens
    # its own request. Rerunning plan then supersedes that pending request.
    request = _open_request(store, run.run_id)
    routes.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(decision=ReviewDecisionType.APPROVE),
        store,
    )
    assert _open_request(store, run.run_id, "script")
    store.run_state_action(
        run.run_id,
        RunStateActionRequest(action=RunStateActionName.RERUN_STAGE, stage_key="plan"),
    )

    superseded = routes.list_review_requests(
        run.run_id, store, status=ReviewRequestStatus.SUPERSEDED
    )
    inbox = routes.project_review_inbox(project.project_id, store)
    session.close()

    assert superseded, "expected the pending script request to be superseded"
    hidden = {item.review_request_id for item in superseded}
    assert not hidden & {item.review_request_id for item in inbox.items}
    assert all(item.status != ReviewRequestStatus.SUPERSEDED for item in inbox.items)


def test_review_inbox_route_404s_on_unknown_project(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.project_review_inbox("proj_missing", store)
    session.close()

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_review_route_still_returns_the_run(tmp_path) -> None:
    session, store, _project, run = _store_at_first_gate(tmp_path)

    payload = routes.review_stage(
        run.run_id,
        "plan",
        ApprovalRequest(decision="approved", comment="可以，开拍"),
        store,
    )
    record = routes.get_review_request(_open_request_id(store, run.run_id, "plan"), store)
    session.close()

    assert payload["run_id"] == run.run_id
    assert payload["current_stage_key"] == "script"
    # The legacy endpoint now writes through the kernel instead of only
    # flipping stage state, so the decision is auditable.
    assert record.decisions[-1].decision == ReviewDecisionType.APPROVE
    assert record.decisions[-1].comment == "可以，开拍"


def test_legacy_review_route_404s_on_unknown_run(tmp_path) -> None:
    session, store, _project, _run = _store_at_first_gate(tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        routes.review_stage(
            "run_missing",
            "plan",
            ApprovalRequest(decision="approved"),
            store,
        )
    session.close()

    assert excinfo.value.status_code == 404


def _open_request_id(store, run_id, stage_key):
    requests = store.list_review_requests(run_id, stage_key=stage_key)
    assert requests
    return requests[-1].review_request_id
