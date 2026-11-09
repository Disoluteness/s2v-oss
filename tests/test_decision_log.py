"""P2: the run decision log records reasoning, not just edits.

``audit_logs`` answers "who changed what" and ``event_logs`` answers "what
happened". Neither answers "why this tool and not the other one", which is the
question an operator actually asks when a run produces something unexpected.
These tests pin the four write points (plan, review verdict, downgrade
acknowledgement, paid-provider consent) and the two read shapes (current view
and full history).

The database is in-memory with a ``StaticPool``: every assertion here is about
the log's own semantics, and a file-backed sqlite would add nothing except a
journal file to clean up.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    ProjectCreate,
    ProviderConsentRequest,
    ReviewRequestStatus,
    RunCreate,
    SubmitReviewDecision,
)
from apps.api.app.routes import confirm_paid_provider as confirm_paid_provider_route
from apps.api.app.routes import get_run_decision_log
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.registry import ToolRegistry

RUN_INPUT = {
    "raw_script": "秋天第一杯奶茶，还没人给你点？",
    "brand_name": "茶小朵",
    "product_name": "冲泡奶茶",
}


def _store(*, quality_mode: str | None = None, isolated_registry: bool = False):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        tool_registry=ToolRegistry(
            env={}, binary_checker=lambda name: f"/fake/bin/{name}"
        )
        if isolated_registry
        else None,
    )
    project = store.create_project(ProjectCreate(title="decision log"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(**RUN_INPUT, quality_mode=quality_mode),
        pipeline=PipelineLoader().get_pipeline(
            "avatar_spokesperson" if isolated_registry else "spokesperson_ad"
        ),
    )
    return store, run


# --------------------------------------------------------------------------- #
# plan -> tool_selection
# --------------------------------------------------------------------------- #


def test_plan_execution_records_one_row_per_capability() -> None:
    store, run = _store()

    artifact = store.plan_execution(run.run_id)
    log = store.list_run_decisions(run.run_id)

    assert artifact is not None
    assert log is not None
    assert log.categories == ["tool_selection"]
    assert log.count == len(log.entries) > 0
    assert log.history_included is False

    # Every decision in the plan is persisted, including the ones that picked
    # nothing: "why did this run have no music" is a question about a skip.
    planned_subjects = {item["subject"] for item in artifact.payload["decision_log"]}
    assert {entry.subject for entry in log.entries} == planned_subjects

    entry = next(item for item in log.entries if item.chosen.get("tool_key"))
    assert entry.category == "tool_selection"
    assert entry.actor_type == "system"
    assert entry.actor_id is None
    assert entry.source_artifact_id == artifact.artifact_id
    assert entry.chosen["capability"]
    assert entry.chosen["tool_key"]
    assert entry.rationale
    # The alternatives travel with the choice — that is the whole point.
    assert entry.candidates
    assert {"tool_key", "score", "selection_role"} <= set(entry.candidates[0])


def test_confidence_is_a_separation_measure_not_a_probability() -> None:
    store, run = _store()
    store.plan_execution(run.run_id)

    log = store.list_run_decisions(run.run_id)
    assert log is not None
    scored = [entry for entry in log.entries if entry.confidence is not None]
    assert scored, "a plan with candidates must report a confidence"
    for entry in scored:
        assert 0.0 <= entry.confidence <= 1.0
        assert entry.confidence_basis in {"score_margin", "uncontested"}

    # A capability with no usable tool has nothing to be confident about, and the
    # column stays NULL rather than claiming certainty.
    blocked = [entry for entry in log.entries if entry.decision in {"blocked", "skip_optional"}]
    assert all(entry.confidence is None for entry in blocked)


def test_current_view_collapses_replans_and_history_keeps_them() -> None:
    store, run = _store()

    store.plan_execution(run.run_id)
    first = store.list_run_decisions(run.run_id)
    store.plan_execution(run.run_id)
    current = store.list_run_decisions(run.run_id)
    history = store.list_run_decisions(run.run_id, include_history=True)

    assert first is not None and current is not None and history is not None
    assert current.count == first.count
    assert history.count == first.count * 2
    assert history.history_included is True
    assert [entry.sequence for entry in history.entries] == sorted(
        entry.sequence for entry in history.entries
    )
    # The current view keeps the *latest* row for each subject.
    assert max(entry.sequence for entry in current.entries) == max(
        entry.sequence for entry in history.entries
    )


# --------------------------------------------------------------------------- #
# review -> review_decision
# --------------------------------------------------------------------------- #


def test_review_verdict_is_recorded_with_its_actor() -> None:
    store, run = _store()
    started = store.start_run(run.run_id)
    assert started is not None

    requests = store.list_review_requests(run.run_id, status=ReviewRequestStatus.OPEN)
    assert requests
    detail = store.submit_review_decision(
        requests[0].review_request_id,
        SubmitReviewDecision(
            decision="approve",
            comment="节奏没问题，可以继续。",
            actor_user_id="user_reviewer",
            actor_display_name="审片人",
        ),
    )
    assert detail is not None

    log = store.list_run_decisions(run.run_id, category="review_decision")
    assert log is not None
    assert log.count == 1
    entry = log.entries[0]
    assert entry.decision == "approve"
    assert entry.subject == requests[0].stage_key
    assert entry.actor_type == "human"
    assert entry.actor_id == "user_reviewer"
    assert entry.actor_display_name == "审片人"
    assert entry.rationale == "节奏没问题，可以继续。"
    assert entry.chosen["review_request_id"] == requests[0].review_request_id
    # A reviewer states a verdict, not a preference ranking.
    assert entry.candidates == []
    assert entry.confidence is None


# --------------------------------------------------------------------------- #
# downgrade + paid consent
# --------------------------------------------------------------------------- #


def test_acknowledged_quality_downgrade_is_a_decision() -> None:
    store, run = _store(quality_mode="provider_avatar", isolated_registry=True)

    store.start_run(
        run.run_id,
        accept_intent_downgrade=True,
        actor_id="user_ops",
        actor_display_name="运营",
    )

    log = store.list_run_decisions(run.run_id, category="quality_downgrade")
    assert log is not None
    assert log.count == 1
    entry = log.entries[0]
    assert entry.decision == "accept_intent_downgrade"
    assert entry.actor_type == "human"
    assert entry.actor_id == "user_ops"
    assert entry.chosen["quality_mode"] == "provider_avatar"
    # What was given up is the candidate list: asked for, not obtained.
    assert entry.candidates
    assert entry.candidates[0]["expected_runtime"] == "api"
    assert entry.candidates[0]["selected_runtime"] == "local"


def test_paid_provider_consent_is_a_decision_with_both_directions() -> None:
    store, run = _store()

    store.confirm_paid_provider(
        run.run_id,
        provider="dashscope",
        note="运营已确认",
        actor_id="user_ops",
        actor_display_name="运营",
    )
    store.confirm_paid_provider(
        run.run_id,
        provider="dashscope",
        accept=False,
        actor_id="user_ops",
        actor_display_name="运营",
    )

    log = store.list_run_decisions(run.run_id, category="paid_provider_consent")
    assert log is not None
    # Same subject, so the current view shows only the retraction ...
    assert log.count == 1
    assert log.entries[0].decision == "retract_paid_provider"
    # ... while the history keeps the confirmation that came before it.
    history = store.list_run_decisions(
        run.run_id, category="paid_provider_consent", include_history=True
    )
    assert history is not None
    assert [entry.decision for entry in history.entries] == [
        "accept_paid_provider",
        "retract_paid_provider",
    ]


# --------------------------------------------------------------------------- #
# read surface
# --------------------------------------------------------------------------- #


def test_unknown_run_has_no_log_rather_than_an_empty_one() -> None:
    store, _run = _store()

    assert store.list_run_decisions("run_does_not_exist") is None


def test_endpoint_returns_the_documented_shape() -> None:
    store, run = _store()
    store.plan_execution(run.run_id)

    log = get_run_decision_log(run.run_id, store)

    assert log.run_id == run.run_id
    assert log.count == len(log.entries)
    assert log.categories == ["tool_selection"]
    payload = log.model_dump()
    assert set(payload) == {
        "run_id",
        "count",
        "total",
        "truncated",
        "categories",
        "history_included",
        "entries",
    }
    assert payload["count"] == payload["total"]
    assert payload["truncated"] is False
    assert set(payload["entries"][0]) >= {
        "decision_id",
        "sequence",
        "category",
        "subject",
        "decision",
        "chosen",
        "candidates",
        "confidence",
        "rationale",
        "actor_type",
        "created_at",
    }


def test_a_truncated_read_says_it_is_truncated() -> None:
    """``limit`` cuts the list; the response has to admit it.

    A truncated audit view that stays silent reads as a complete one, which is
    exactly the failure mode this log exists to prevent.
    """

    store, run = _store()
    store.plan_execution(run.run_id)
    full = store.list_run_decisions(run.run_id)
    assert full is not None and full.total > 1

    cut = store.list_run_decisions(run.run_id, limit=1)
    assert cut is not None
    assert cut.count == 1
    assert cut.total == full.total
    assert cut.truncated is True
    # The truncation keeps the newest rows: the current view is what a page shows.
    assert cut.entries[0].sequence == max(entry.sequence for entry in full.entries)


def test_endpoint_filters_by_category_and_404s_for_an_unknown_run() -> None:
    store, run = _store()
    store.start_run(run.run_id)
    requests = store.list_review_requests(run.run_id, status=ReviewRequestStatus.OPEN)
    assert requests

    empty = get_run_decision_log(run.run_id, store, category="review_decision")
    assert empty.count == 0
    # The category list is computed over the whole run, so the filter chips stay
    # complete even when one of them is selected.
    assert empty.categories == ["tool_selection"]

    with pytest.raises(HTTPException) as exc_info:
        get_run_decision_log("run_does_not_exist", store)
    assert exc_info.value.status_code == 404


def test_paid_consent_route_still_works_through_the_http_signature() -> None:
    """The route gained an actor parameter; the response shape must not move."""

    from apps.api.app.actor import ActorContext

    store, run = _store()
    response = confirm_paid_provider_route(
        run.run_id,
        ProviderConsentRequest(provider="dashscope"),
        store,
        ActorContext(actor_id="user_ops", tenant_id="tenant_default", display_name="运营"),
    )

    assert response["accept"] is True
    assert response["paid_provider_consents"] == {"dashscope": True}
    log = store.list_run_decisions(run.run_id, category="paid_provider_consent")
    assert log is not None
    assert log.entries[0].actor_id == "user_ops"
