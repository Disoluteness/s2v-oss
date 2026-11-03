"""Regression guard for the SQLite/MySQL foreign-key parity gap.

Every reviewer id written by the review system (``assignee_user_id``,
``actor_user_id``, ``author_user_id``) is foreign-keyed to ``users.id``. MySQL
enforces that constraint; SQLite silently ignored it, so a deployed stack blew
up with IntegrityError 1452 -> HTTP 500 while the local suite stayed green. The
first test pins the pragma that closes the gap, the second proves the pragma is
load-bearing, and the third pins the behaviour that makes an unknown reviewer id
acceptable instead of fatal.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_models import UserRecord
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    CreateReviewComment,
    ProjectCreate,
    ReviewCommentSeverity,
    ReviewDecisionType,
    ReviewRequestStatus,
    RunCreate,
    SubmitReviewDecision,
)
from packages.pipeline_kit.loader import PipelineLoader


def _store_at_first_gate(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    project = store.create_project(ProjectCreate(title="reviewer registration"))
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
    requests = store.list_review_requests(run.run_id, stage_key="plan")
    assert requests is not None
    open_requests = [item for item in requests if item.status == ReviewRequestStatus.OPEN]
    assert open_requests, "run did not park at the plan gate"
    return session, store, open_requests[-1]


def test_test_suite_sqlite_engines_enforce_foreign_keys(tmp_path) -> None:
    """Tests must run with the same integrity rules as MySQL/InnoDB."""
    engine = create_engine(f"sqlite:///{tmp_path / 'pragma.db'}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_database_rejects_a_user_row_without_a_tenant(tmp_path) -> None:
    """The pragma from the previous test has to actually reject bad rows."""
    session, _store, _request = _store_at_first_gate(tmp_path, "fk-raw.db")
    session.add(
        UserRecord(
            id="user_orphan",
            tenant_id="tenant_does_not_exist",
            email="orphan@reviewer.invalid",
            name="orphan",
            role="reviewer",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_review_writes_register_external_reviewer_ids(tmp_path) -> None:
    """An id the deployment has never seen must be registered, not rejected."""
    session, store, request = _store_at_first_gate(tmp_path, "register.db")
    reviewer = "user_deploy_check"

    assigned = store.assign_review_request(
        request.review_request_id,
        assignee_user_id=reviewer,
        due_at=None,
    )
    assert assigned is not None
    assert assigned.assignee_user_id == reviewer

    comment = store.add_review_comment(
        request.review_request_id,
        CreateReviewComment(
            body="开场钩子不够强。",
            severity=ReviewCommentSeverity.CRITICAL,
            author_user_id=reviewer,
            author_display_name="部署核对",
        ),
    )
    assert comment is not None
    assert comment.author_user_id == reviewer

    store.submit_review_decision(
        request.review_request_id,
        SubmitReviewDecision(
            decision=ReviewDecisionType.APPROVE,
            actor_user_id=reviewer,
            actor_display_name="部署核对",
        ),
    )

    registered = session.get(UserRecord, reviewer)
    assert registered is not None
    assert registered.name == "部署核对"
    detail = store.get_review_request(request.review_request_id)
    assert detail is not None
    assert [item.actor_user_id for item in detail.decisions] == [reviewer]
    assert [item.author_user_id for item in detail.comments] == [reviewer]
