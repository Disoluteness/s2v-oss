"""A review due date has to keep its instant across a MySQL DATETIME round trip.

Neither MySQL ``DATETIME`` nor SQLite stores an offset, and both drivers write
the wall-clock fields verbatim, so an inbound ``+08:00`` timestamp would come
back looking like a UTC one — eight hours off, and wrong about whether the
review is overdue.
"""

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import ProjectCreate, ReviewRequestStatus, RunCreate
from apps.api.app.repositories import as_utc
from packages.pipeline_kit.loader import PipelineLoader


def _store_at_first_gate(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'due-at.db'}")
    Base.metadata.create_all(engine)
    store = DatabaseStore(Session(engine))
    project = store.create_project(ProjectCreate(title="due date project"))
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
    requests = store.list_review_requests(run.run_id, stage_key="plan")
    assert requests is not None
    open_requests = [item for item in requests if item.status == ReviewRequestStatus.OPEN]
    assert open_requests, "run did not park at the plan gate"
    return store, open_requests[-1]


def test_due_at_keeps_the_instant_it_was_given(tmp_path) -> None:
    store, request = _store_at_first_gate(tmp_path)
    # 10:00 in UTC+8 is 02:00 UTC on the same day. The date is derived from the
    # clock rather than hardcoded: a fixed future date silently turns this into a
    # failing test the moment real time walks past it.
    due_at = (
        datetime.now(UTC) + timedelta(days=2)
    ).astimezone(timezone(timedelta(hours=8))).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    expected_utc = due_at.astimezone(UTC)
    updated = store.assign_review_request(
        request.review_request_id,
        assignee_user_id="user_deploy_check",
        due_at=due_at,
    )
    assert updated is not None
    assert updated.due_at is not None
    assert updated.due_at.tzinfo is None  # the column keeps naive UTC
    assert as_utc(updated.due_at) == expected_utc
    assert updated.overdue is False

    reread = store.get_review_request(request.review_request_id)
    assert reread is not None
    assert reread.due_at is not None
    assert as_utc(reread.due_at) == expected_utc


def test_overdue_uses_the_same_instant_semantics(tmp_path) -> None:
    store, request = _store_at_first_gate(tmp_path)
    past = datetime(2000, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    updated = store.assign_review_request(
        request.review_request_id,
        assignee_user_id="user_deploy_check",
        due_at=past,
    )
    assert updated is not None
    assert updated.due_at is not None
    assert as_utc(updated.due_at) == datetime(1999, 12, 31, 16, 0, tzinfo=UTC)
    assert updated.overdue is True
