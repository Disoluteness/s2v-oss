"""V8.2 observability: /metrics, request ids, structured logs, the panel.

The layers, cheapest first:

1. **The JSON formatter** — a pure function over a LogRecord.
2. **The request-id middleware** — driven through the real ASGI app, the same
   way the permission tests drive it, so what is asserted is what Starlette
   sends.
3. **The metrics endpoint** — open by design (scrapers have no actor), counts
   requests under their *route template*, and enriches itself with DB gauges.
4. **The project observability panel** — aggregates seeded through the real
   store, proving cost/latency/error views answer from existing tables only.
"""

from __future__ import annotations

import json
import logging

import pytest
from harness import build_client

from apps.api.app import db_models  # noqa: F401  (registers the tables)
from apps.api.app.actor import ActorContext
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.models import (
    BackgroundJobCreate,
    BackgroundJobStatus,
    EventSeverity,
    EventSourceType,
)
from apps.api.app.observability import (
    HTTP_REQUESTS_TOTAL,
    WORKER_JOBS_TOTAL,
    JsonFormatter,
    _request_id,
    configure_logging,
    new_request_id,
)
from apps.api.app.repositories import DEFAULT_TENANT_ID
from apps.worker.background_jobs import OUTCOME_FOR_STATUS

OWNER = "system"


def _default_actor() -> ActorContext:
    return ActorContext(
        actor_id=OWNER,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "observability.db")
    # The default actor only owns the default team once seated (V6.5 rule:
    # new identities do not join a team by themselves).
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _create_project(asgi) -> str:
    status, _, response = asgi.request(
        "POST",
        "/api/projects",
        json_body={"title": "观察项目"},
        actor_id=OWNER,
    )
    assert status == 200, response
    return response["project_id"]


def _create_run(asgi, project_id: str) -> str:
    status, _, response = asgi.request(
        "POST",
        f"/api/projects/{project_id}/runs",
        json_body={"raw_script": "观察一段脚本"},
        actor_id=OWNER,
    )
    assert status == 200, response
    return response["run_id"]


def _counter_total(counter, labels: tuple[str, ...]) -> float:
    return float(counter.labels(*labels)._value.get())


# --------------------------------------------------------------------------- #
# 1. structured logging
# --------------------------------------------------------------------------- #


def test_json_formatter_emits_one_object_with_request_id_and_extras():
    token = _request_id.set("req_abc123")
    try:
        record = logging.LogRecord(
            "s2v.test",
            logging.INFO,
            __file__,
            1,
            "handled %s",
            ("thing",),
            None,
        )
        record.http_status = 200
        line = JsonFormatter().format(record)
    finally:
        _request_id.reset(token)
    payload = json.loads(line)
    assert payload["message"] == "handled thing"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "s2v.test"
    assert payload["request_id"] == "req_abc123"
    assert payload["http_status"] == 200
    assert payload["ts"].endswith("Z")


def test_json_formatter_omits_request_id_when_outside_a_request():
    token = _request_id.set(None)
    try:
        record = logging.LogRecord("s2v.test", logging.INFO, __file__, 1, "idle", (), None)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        _request_id.reset(token)
    assert "request_id" not in payload


def test_configure_logging_json_swaps_handlers_and_is_restorable():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure_logging(format="json", level="WARNING")
        assert root.level == logging.WARNING
        assert any(
            isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
        )
        # Idempotent: a second call must not stack a second handler.
        configure_logging(format="json", level="WARNING")
        json_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler.formatter, JsonFormatter)
        ]
        assert len(json_handlers) == 1
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_new_request_ids_are_unique_and_prefixed():
    first, second = new_request_id(), new_request_id()
    assert first.startswith("req_") and second.startswith("req_")
    assert first != second


# --------------------------------------------------------------------------- #
# 2. request ids through the real app
# --------------------------------------------------------------------------- #


def test_every_response_carries_a_generated_request_id(asgi):
    status, headers, _ = asgi.request("GET", "/api/health", actor_id=OWNER)
    assert status == 200
    assert headers["x-request-id"].startswith("req_")


def test_an_inbound_request_id_is_echoed_verbatim(asgi):
    status, headers, _ = asgi.request(
        "GET",
        "/api/health",
        headers={"X-Request-Id": "req_upstream_42"},
        actor_id=OWNER,
    )
    assert status == 200
    assert headers["x-request-id"] == "req_upstream_42"


# --------------------------------------------------------------------------- #
# 3. /metrics
# --------------------------------------------------------------------------- #


def test_metrics_is_open_and_serves_prometheus_text(asgi):
    status, headers, body = asgi.raw("GET", "/metrics")
    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    text = body.decode("utf-8")
    assert "s2v_http_requests_total" in text


def test_metrics_counts_requests_under_their_route_template(asgi):
    labels = ("GET", "/api/health", "200")
    before = _counter_total(HTTP_REQUESTS_TOTAL, labels)
    asgi.request("GET", "/api/health", actor_id=OWNER)
    assert _counter_total(HTTP_REQUESTS_TOTAL, labels) == before + 1


def test_the_metrics_scrape_itself_is_not_counted(asgi):
    asgi.raw("GET", "/metrics")
    text = asgi.raw("GET", "/metrics")[2].decode("utf-8")
    assert 'path="/metrics"' not in text


def test_metrics_is_absent_from_the_openapi_document():
    spec = app.openapi()
    assert "/metrics" not in spec["paths"]


def test_metrics_exposes_db_gauges_for_jobs_and_runs(asgi):
    project_id = _create_project(asgi)
    run_id = _create_run(asgi, project_id)
    asgi.store.background_jobs.create_job(
        run_id=run_id,
        payload=BackgroundJobCreate(job_type="stage_tool_step", request_payload={}),
    )
    asgi.store.session.commit()
    text = asgi.raw("GET", "/metrics")[2].decode("utf-8")
    assert 's2v_runs{status="draft"}' in text
    assert 's2v_background_jobs{dimension="queue",value="media"}' in text
    assert 's2v_background_jobs{dimension="status",value="queued"}' in text


# --------------------------------------------------------------------------- #
# 4. the project observability panel
# --------------------------------------------------------------------------- #


def _seed_costly_run(store: DatabaseStore, asgi) -> tuple[str, str]:
    project_id = _create_project(asgi)
    run_id = _create_run(asgi, project_id)
    store.usage_ledger.append_entry(
        run_id=run_id,
        source_type="tool",
        source_id="tool_1",
        operation="generate_tts",
        provider="azure_tts",
        tool_key="tts",
        estimated_cost=0.5,
        duration_ms=1200,
    )
    store.usage_ledger.append_entry(
        run_id=run_id,
        source_type="tool",
        source_id="tool_2",
        operation="generate_tts",
        provider="azure_tts",
        tool_key="tts",
        actual_cost=0.9,
        duration_ms=800,
    )
    store.usage_ledger.append_entry(
        run_id=run_id,
        source_type="provider",
        source_id="tool_3",
        operation="generate_avatar",
        provider="heygen",
        tool_key="avatar",
        estimated_cost=2.0,
        actual_cost=3.0,
        duration_ms=4000,
    )
    store.background_jobs.create_job(
        run_id=run_id,
        payload=BackgroundJobCreate(
            job_type="stage_tool_step",
            request_payload={"capability": "tts"},
            max_attempts=2,
        ),
        queue="media",
    )
    job = store.background_jobs.create_job(
        run_id=run_id,
        payload=BackgroundJobCreate(
            job_type="avatar_from_assets",
            request_payload={"capability": "avatar"},
            max_attempts=1,
        ),
        queue="provider",
    )
    store.background_jobs.update_job(
        job.job_id,
        status=BackgroundJobStatus.DEAD,
        error_code="provider_quota_exhausted",
    )
    store.events.append_event(
        run_id=run_id,
        event_type="job.dead_lettered",
        source_type=EventSourceType.SYSTEM,
        source_id=job.job_id,
        severity=EventSeverity.ERROR,
        message="重试耗尽",
    )
    store.events.append_event(
        run_id=run_id,
        event_type="run.start_blocked",
        source_type=EventSourceType.SYSTEM,
        source_id=run_id,
        severity=EventSeverity.ERROR,
        message="预算不足",
    )
    store.events.append_event(
        run_id=run_id,
        event_type="job.queued",
        source_type=EventSourceType.SYSTEM,
        source_id=job.job_id,
        message="排队",
    )
    store.session.commit()
    return project_id, run_id


def test_the_panel_aggregates_cost_latency_jobs_and_errors(asgi):
    project_id, _ = _seed_costly_run(asgi.store, asgi)
    status, _, body = asgi.request(
        "GET", f"/api/projects/{project_id}/observability", actor_id=OWNER
    )
    assert status == 200, body
    assert body["project_id"] == project_id
    assert body["generated_at"] is not None

    cost = body["cost"]
    assert cost["estimated_total"] == pytest.approx(2.5)
    assert cost["actual_total"] == pytest.approx(3.9)
    # actual wins when present, estimate stands in when not
    assert cost["by_provider"]["azure_tts"] == pytest.approx(0.9 + 0.5)
    assert cost["by_provider"]["heygen"] == pytest.approx(3.0)
    assert cost["by_tool_key"]["avatar"] == pytest.approx(3.0)
    assert cost["by_source_type"]["provider"] == pytest.approx(3.0)

    latency = body["latency"]
    assert latency["sample_count"] == 3
    assert latency["avg_ms"] == pytest.approx(2000.0)
    assert latency["p50_ms"] == pytest.approx(1200.0)
    assert latency["p95_ms"] == pytest.approx(4000.0)

    jobs = body["jobs"]
    assert jobs["by_status"] == {"dead": 1, "queued": 1}
    assert jobs["by_queue"] == {"media": 1, "provider": 1}
    assert jobs["dead_letter"] == 1
    assert jobs["top_error_codes"] == {"provider_quota_exhausted": 1}

    events = body["events"]
    assert events["errors_by_type"] == {"job.dead_lettered": 1, "run.start_blocked": 1}
    recent_types = [error["event_type"] for error in events["recent_errors"]]
    assert set(recent_types) == {"job.dead_lettered", "run.start_blocked"}
    assert all(error["run_id"] for error in events["recent_errors"])


def test_an_empty_project_answers_zeroed_aggregates(asgi):
    project_id = _create_project(asgi)
    status, _, body = asgi.request(
        "GET", f"/api/projects/{project_id}/observability", actor_id=OWNER
    )
    assert status == 200
    assert body["cost"] == {
        "estimated_total": 0.0,
        "actual_total": 0.0,
        "by_provider": {},
        "by_tool_key": {},
        "by_source_type": {},
    }
    assert body["latency"] == {
        "sample_count": 0,
        "avg_ms": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
    }
    assert body["jobs"] == {
        "by_status": {},
        "by_queue": {},
        "dead_letter": 0,
        "top_error_codes": {},
    }
    assert body["events"] == {"errors_by_type": {}, "recent_errors": []}


def test_the_panel_refuses_an_unknown_project_without_confirming_it(asgi):
    # A seated identity asking for a project that does not exist gets the same
    # 404 any unknown id gets; the denial only differs for identities with no
    # seat at all (the test above).
    status, _, body = asgi.request(
        "GET", "/api/projects/proj_missing/observability", actor_id=OWNER
    )
    assert status == 404
    assert body["detail"]["code"] == "not_found"


def test_the_panel_refuses_an_unseated_identity(asgi):
    project_id = _create_project(asgi)
    status, _, body = asgi.request(
        "GET",
        f"/api/projects/{project_id}/observability",
        headers={"X-Actor-Id": "stranger"},
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"


def test_the_store_gauges_count_every_job_and_run(asgi):
    project_id = _create_project(asgi)
    run_id = _create_run(asgi, project_id)
    asgi.store.background_jobs.create_job(
        run_id=run_id,
        payload=BackgroundJobCreate(job_type="stage_tool_step", request_payload={}),
    )
    asgi.store.session.commit()
    gauges = asgi.store.observability_gauges()
    assert gauges["runs_by_status"].get("draft") == 1
    assert gauges["jobs_by_status"].get("queued") == 1
    # A job with no explicit queue falls back to the column default.
    assert gauges["jobs_by_queue"].get("media") == 1


# --------------------------------------------------------------------------- #
# 5. worker counters
# --------------------------------------------------------------------------- #


def test_every_job_status_has_an_outcome_label():
    # ``running`` is a lease state a worker claims, never an outcome of a
    # processed job — it is the one status deliberately unmapped.
    mapped = {status.value for status in BackgroundJobStatus} - {"running"}
    assert set(OUTCOME_FOR_STATUS) == mapped


def test_worker_outcome_counters_accept_every_mapped_label():
    for outcome in OUTCOME_FOR_STATUS.values():
        WORKER_JOBS_TOTAL.labels(outcome=outcome).inc()
        assert WORKER_JOBS_TOTAL.labels(outcome=outcome)._value.get() >= 1.0
