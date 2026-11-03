"""V8.1 worker scaling: queues, priority, lease, timeout reaper, retry, DLQ."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_models import BackgroundJobRecord
from apps.api.app.db_store import DatabaseStore, _priority_for_job, _queue_for_job
from apps.api.app.models import (
    BackgroundJobCreate,
    BackgroundJobStatus,
    ProjectCreate,
    RunCreate,
)
from apps.worker.background_jobs import ROLE_QUEUES, queues_for_role
from packages.pipeline_kit.loader import PipelineLoader


def _make_store(tmp_path) -> tuple[DatabaseStore, object]:
    engine = create_engine(f"sqlite:///{tmp_path / 'scaling.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()
    store = DatabaseStore(session)
    return store, session


def _seed_run(store: DatabaseStore, title: str) -> str:
    project = store.projects.create_project(ProjectCreate(title=title))
    run = store.runs.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script=f"{title} 的脚本"),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )
    return run.run_id


def _make_job(
    store: DatabaseStore,
    run_id: str,
    *,
    job_type: str = "stage_tool_step",
    capability: str | None = None,
    max_attempts: int = 3,
) -> str:
    request_payload = {"capability": capability} if capability else {}
    job = store.background_jobs.create_job(
        run_id=run_id,
        payload=BackgroundJobCreate(
            job_type=job_type,
            request_payload=request_payload,
            max_attempts=max_attempts,
            priority=_priority_for_job(job_type, request_payload),
        ),
        queue=_queue_for_job(job_type, request_payload),
    )
    return job.job_id


# --- queue & priority derivation -------------------------------------------


def test_queue_derivation_maps_capability_to_lanes() -> None:
    assert _queue_for_job("avatar_from_assets", {}) == "provider"
    assert _queue_for_job("stage_tool_step", {"capability": "media_analysis"}) == "analysis"
    assert _queue_for_job("stage_tool_step", {"capability": "subtitle_generation"}) == "media"
    assert _queue_for_job("stage_tool_step", {"capability": "video_composition"}) == "render"
    assert _queue_for_job("stage_tool_step", {"capability": "storage"}) == "render"
    assert _queue_for_job("stage_tool_step", {"capability": "mystery"}) == "media"
    assert _queue_for_job("totally_unknown", {}) == "media"


def test_priority_defaults_are_layered_cheap_first() -> None:
    assert _priority_for_job("stage_tool_step", {"capability": "media_analysis"}) == 40
    assert _priority_for_job("stage_tool_step", {"capability": "subtitle_generation"}) == 50
    assert _priority_for_job("stage_tool_step", {"capability": "video_composition"}) == 60
    assert _priority_for_job("stage_tool_step", {"capability": "storage"}) == 60
    assert _priority_for_job("avatar_from_assets", {}) == 50


def test_create_job_persists_queue_priority_and_dto_echoes_them(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "DTO echo")
    job_id = _make_job(store, run_id, capability="media_analysis")

    job = store.get_background_job(job_id)
    session.close()

    assert job is not None
    assert job.queue == "analysis"
    assert job.priority == 40  # cheap analysis work defaults ahead of composition
    assert job.started_at is None
    assert job.lease_expires_at is None
    assert job.next_run_at is None


def test_claim_orders_by_priority_then_fifo(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Priority order")
    expensive = _make_job(store, run_id, capability="video_composition")  # 60
    cheap = _make_job(store, run_id, capability="media_analysis")  # 40
    middle = _make_job(store, run_id, capability="subtitle_generation")  # 50

    claimed = store.claim_processable_background_jobs(
        worker_id="worker-a",
        limit=10,
        lease_seconds=300,
    )
    session.close()

    # Same-second created_at ties fall back to insertion order; priority dominates.
    assert claimed[0].job_id == cheap
    assert {job.job_id for job in claimed} == {cheap, middle, expensive}
    assert [job.priority for job in claimed] == sorted(job.priority for job in claimed)


def test_claim_filters_by_queue_for_role_workers(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Role filter")
    provider_job = _make_job(store, run_id, job_type="avatar_from_assets")
    analysis_job = _make_job(store, run_id, capability="media_analysis")

    claimed = store.claim_processable_background_jobs(
        worker_id="worker-render",
        limit=10,
        lease_seconds=300,
        queues=["render"],
    )
    session.close()

    assert claimed == []
    assert provider_job and analysis_job  # both still queued, untouched


def test_role_mapping_is_explicit_and_rejects_unknown_roles() -> None:
    assert queues_for_role("all") is None
    assert queues_for_role("provider") == ["provider"]
    assert queues_for_role("analysis") == ["analysis"]
    assert queues_for_role("media") == ["media"]
    assert queues_for_role("render") == ["render"]
    assert set(ROLE_QUEUES) == {"all", "provider", "analysis", "media", "render"}
    with pytest.raises(ValueError):
        queues_for_role("planner")


# --- lease -------------------------------------------------------------------


def test_claim_sets_an_expiring_lease(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Lease set")
    _make_job(store, run_id)

    claimed = store.claim_processable_background_jobs(
        worker_id="worker-a",
        limit=1,
        lease_seconds=300,
    )
    session.close()

    assert len(claimed) == 1
    assert claimed[0].lease_expires_at is not None
    remaining = claimed[0].lease_expires_at - datetime.now(UTC).replace(tzinfo=None)
    assert timedelta(0) < remaining <= timedelta(seconds=300)


# --- retry / dead letter disposition ----------------------------------------


def test_transient_stage_tool_step_failure_schedules_a_backoff_retry(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Auto retry")
    job_id = _make_job(store, run_id, capability="subtitle_generation")

    job = store.get_background_job(job_id)
    assert job is not None
    result = store._fail_background_job(job, error_code="provider task failed", error_message="HTTP 500 upstream")
    events = [event.event_type for event in store.list_events(run_id)]
    session.close()

    assert result.status == BackgroundJobStatus.QUEUED
    assert result.attempt == 2
    assert result.error_code == "provider task failed"
    assert result.error_retryable is True
    assert result.next_run_at is not None
    assert result.next_run_at > datetime.now(UTC).replace(tzinfo=None)
    assert "job.retry_scheduled" in events


def test_backed_off_job_is_invisible_to_the_claimer_until_due(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Backoff visibility")
    job_id = _make_job(store, run_id, capability="subtitle_generation")
    job = store.get_background_job(job_id)
    assert job is not None
    store._fail_background_job(job, error_code="provider task failed", error_message="HTTP 500")

    not_yet = store.claim_processable_background_jobs(worker_id="worker-a", limit=10)
    record = session.get(BackgroundJobRecord, job_id)
    assert record is not None
    record.next_run_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    session.commit()
    due = store.claim_processable_background_jobs(worker_id="worker-a", limit=10)
    session.close()

    assert not_yet == []
    assert [claimed.job_id for claimed in due] == [job_id]


def test_exhausted_transient_retries_dead_letter(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Dead letter")
    job_id = _make_job(store, run_id, capability="subtitle_generation", max_attempts=2)

    first = store.get_background_job(job_id)
    assert first is not None
    second = store._fail_background_job(first, error_code="provider task failed", error_message="HTTP 500")
    assert second.status == BackgroundJobStatus.QUEUED
    assert second.attempt == 2

    third = store._fail_background_job(second, error_code="provider task failed", error_message="HTTP 500")
    events = [event.event_type for event in store.list_events(run_id)]
    session.close()

    assert third.status == BackgroundJobStatus.DEAD
    assert third.attempt == 2
    assert third.max_attempts == 2
    assert third.error_code == "provider task failed"
    assert "job.dead_lettered" in events
    assert "job.retry_scheduled" in events


def test_non_retryable_failure_stays_a_plain_terminal_failure(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Terminal failure")
    job_id = _make_job(store, run_id, capability="subtitle_generation")

    job = store.get_background_job(job_id)
    assert job is not None
    result = store._fail_background_job(job, error_code="missing required input", error_message="asset does not belong to this run")
    events = [event.event_type for event in store.list_events(run_id)]
    session.close()

    assert result.status == BackgroundJobStatus.FAILED
    assert result.error_retryable is False
    assert "job.failed" in events
    assert "job.retry_scheduled" not in events
    assert "job.dead_lettered" not in events


def test_paid_avatar_submission_never_retries_unattended(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Avatar no auto retry")
    job_id = _make_job(store, run_id, job_type="avatar_from_assets")

    job = store.get_background_job(job_id)
    assert job is not None
    result = store._fail_background_job(job, error_code="provider task failed", error_message="HTTP 500 upstream")
    events = [event.event_type for event in store.list_events(run_id)]
    session.close()

    # The error classifies as transient, but a paid resubmission is a
    # human decision: the job lands in `failed`, not back in the queue.
    assert result.status == BackgroundJobStatus.FAILED
    assert result.error_retryable is True
    assert "job.failed" in events
    assert "job.retry_scheduled" not in events


def test_requeue_replays_a_dead_job_with_a_fresh_attempt_count(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Requeue")
    job_id = _make_job(store, run_id, capability="subtitle_generation", max_attempts=1)
    job = store.get_background_job(job_id)
    assert job is not None
    dead = store._fail_background_job(job, error_code="provider task failed", error_message="HTTP 500")
    assert dead.status == BackgroundJobStatus.DEAD

    requeued = store.requeue_background_job(job_id)
    events = [event.event_type for event in store.list_events(run_id)]
    session.close()

    assert requeued is not None
    assert requeued.status == BackgroundJobStatus.QUEUED
    assert requeued.attempt == 1
    assert requeued.error_code is None
    assert requeued.next_run_at is None
    assert "job.requeued" in events


def test_requeue_rejects_jobs_that_are_not_dead(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Requeue guard")
    job_id = _make_job(store, run_id, capability="subtitle_generation")

    job = store.get_background_job(job_id)
    assert job is not None
    untouched = store.requeue_background_job(job_id)
    session.close()

    assert untouched is not None
    assert untouched.status == BackgroundJobStatus.QUEUED  # unchanged, route turns this into 409


def test_manual_retry_clears_the_backoff_and_bumps_the_attempt(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Manual retry")
    job_id = _make_job(store, run_id, capability="subtitle_generation")
    job = store.get_background_job(job_id)
    assert job is not None
    store._fail_background_job(job, error_code="provider task failed", error_message="HTTP 500")

    retried = store.retry_background_job(job_id)
    session.close()

    assert retried is not None
    assert retried.status == BackgroundJobStatus.QUEUED
    # The failure already scheduled an automatic retry (attempt 2); the manual
    # retry consumes one more attempt and clears the backoff.
    assert retried.attempt == 3
    assert retried.next_run_at is None
    assert retried.error_code is None


# --- timeout reaper ----------------------------------------------------------


def _start_job(store: DatabaseStore, job_id: str, *, minutes_ago: int) -> None:
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    store.background_jobs.update_job(
        job_id,
        status=BackgroundJobStatus.RUNNING,
        progress=10,
        locked_by="worker-a",
        started_at=started,
    )


def test_reaper_fails_a_stuck_running_tool_step_into_a_retry(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Reap retry")
    job_id = _make_job(store, run_id, capability="subtitle_generation")
    _start_job(store, job_id, minutes_ago=60)

    reaped = store.reap_stalled_background_jobs(timeout_seconds=1800)
    session.close()

    assert [job.job_id for job in reaped] == [job_id]
    assert reaped[0].status == BackgroundJobStatus.QUEUED
    assert reaped[0].error_code == "job_timeout"
    assert reaped[0].error_retryable is True
    assert reaped[0].next_run_at is not None


def test_reaper_sends_an_exhausted_stuck_job_to_the_dead_letter_queue(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Reap dead")
    job_id = _make_job(store, run_id, capability="subtitle_generation", max_attempts=1)
    _start_job(store, job_id, minutes_ago=60)

    reaped = store.reap_stalled_background_jobs(timeout_seconds=1800)
    session.close()

    assert reaped[0].status == BackgroundJobStatus.DEAD


def test_reaper_leaves_fresh_running_jobs_and_waiting_provider_alone(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Reap scope")
    fresh_id = _make_job(store, run_id, capability="subtitle_generation")
    _start_job(store, fresh_id, minutes_ago=5)
    waiting_id = _make_job(store, run_id, capability="subtitle_generation")
    _start_job(store, waiting_id, minutes_ago=600)
    store.background_jobs.update_job(waiting_id, status=BackgroundJobStatus.WAITING_PROVIDER)

    reaped = store.reap_stalled_background_jobs(timeout_seconds=1800)
    fresh = store.get_background_job(fresh_id)
    waiting = store.get_background_job(waiting_id)
    session.close()

    assert reaped == []
    assert fresh is not None and fresh.status == BackgroundJobStatus.RUNNING
    assert waiting is not None and waiting.status == BackgroundJobStatus.WAITING_PROVIDER


def test_reaper_fails_a_stuck_avatar_job_without_requeueing_it(tmp_path) -> None:
    store, session = _make_store(tmp_path)
    run_id = _seed_run(store, "Reap avatar")
    job_id = _make_job(store, run_id, job_type="avatar_from_assets")
    _start_job(store, job_id, minutes_ago=600)

    reaped = store.reap_stalled_background_jobs(timeout_seconds=1800)
    session.close()

    assert reaped[0].status == BackgroundJobStatus.FAILED
    assert reaped[0].error_code == "job_timeout"
