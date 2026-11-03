"""V7.1 HTTP API contract: error envelope, error catalog, idempotency, pagination.

Three things are pinned here:

1. **One failure shape.** Whatever goes wrong — a missing resource, a body that
   fails validation, a domain gate — the response body is
   ``{"detail": {"code", "message", ...}}`` with a code from the published
   catalog. Before V7.1 a client had to know which endpoints returned which
   shape; the tests below assert the shape holds across all three sources.
2. **Idempotency is opt-in and honest.** A retry with the same key and body
   replays; the same key with a different body is refused rather than aliased;
   no key at all behaves exactly as before.
3. **Pagination convention.** Endpoints that predate the ``Page`` envelope keep
   their bare-array body and expose the row count in ``X-Total-Count``; new
   endpoints return ``Page``.

Requests are driven through the real ASGI application rather than
``fastapi.testclient``, because ``httpx`` is not a dependency of this project.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models, routes  # noqa: F401  (db_models registers the tables)
from apps.api.app import idempotency as idempotency_module
from apps.api.app.actor import ActorContext
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.errors import (
    ApiError,
    ErrorCategory,
    ErrorCode,
    code_for_status,
    default_status,
    error_catalog_table,
    error_payload,
)
from apps.api.app.main import app
from apps.api.app.models import Page, ProjectCreate, RunCreate, TeamMemberAdd
from apps.api.app.repositories import DEFAULT_TENANT_ID, IdempotencyOutcome
from apps.api.app.routes import get_store
from packages.pipeline_kit.loader import PipelineLoader

HEADER = "Idempotency-Key"


def _default_actor() -> ActorContext:
    """The fallback identity, matching what ``resolve_actor`` returns with no header."""
    return ActorContext(
        actor_id="system",
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


def _seat(store: DatabaseStore, *user_ids: str) -> str:
    """Put the default actor in the default team, then seat the given identities."""
    owner = _default_actor()
    store.ensure_default_seat(owner)
    store.session.commit()
    team_id = store.actor_seat(owner)[0]
    assert team_id is not None
    for user_id in user_ids:
        store.add_team_member(team_id, TeamMemberAdd(user_id=user_id, role="member"), owner)
    store.session.commit()
    return team_id


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


class _AsgiClient:
    """Drive the real app through ASGI, capturing status, headers and body."""

    def __init__(self, store: DatabaseStore, engine) -> None:
        self.store = store
        app.dependency_overrides[get_store] = lambda: store
        # The recorder middleware runs outside the dependency graph, so it has to
        # be pointed at the same test database.
        self._previous_factory = idempotency_module._session_factory
        idempotency_module._session_factory = lambda: Session(engine)

    def close(self) -> None:
        app.dependency_overrides.pop(get_store, None)
        idempotency_module._session_factory = self._previous_factory

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        raw_path, _, query = path.partition("?")
        sent = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
        if body:
            sent.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": sent,
            "server": ("testserver", 80),
            "client": ("testclient", 1),
        }
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await app(scope, receive, send)

        start = next(m for m in messages if m["type"] == "http.response.start")
        payload = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        response_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start.get("headers") or []
        }
        return int(start["status"]), response_headers, payload

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        actor_id: str | None = None,
    ):
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        sent = dict(headers or {})
        if actor_id:
            sent["X-Actor-Id"] = actor_id
        status, response_headers, raw = asyncio.run(
            self._call(method, path, body=body, headers=sent)
        )
        return status, response_headers, (json.loads(raw) if raw else None)


@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api-contract.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    asgi = _AsgiClient(DatabaseStore(Session(engine)), engine)
    try:
        yield asgi
    finally:
        asgi.close()


# --------------------------------------------------------------------------- #
# 1. the catalog itself
# --------------------------------------------------------------------------- #


def test_catalog_covers_every_code_exactly_once():
    entries = error_catalog_table()
    assert [entry["code"] for entry in entries] == [code.value for code in ErrorCode]
    assert len(entries) == len(ErrorCode)
    for entry in entries:
        assert isinstance(entry["status_code"], int) and 400 <= entry["status_code"] < 600
        assert entry["category"] in {category.value for category in ErrorCategory}
        assert isinstance(entry["retryable"], bool)
        assert entry["description"]


def test_status_codes_and_categories_agree_with_the_helpers():
    for entry in error_catalog_table():
        code = ErrorCode(entry["code"])
        assert default_status(code) == entry["status_code"]
        # Several codes legitimately share one status — a 400 body may be a plain
        # ``bad_request`` or one of the specific gate codes that the route raised
        # on purpose. What must hold is that the *fallback* for a status is a
        # registered code that agrees with that same status, so a handler that
        # only knows the status number can never invent a contradiction.
        fallback = ErrorCode(code_for_status(entry["status_code"]))
        assert default_status(fallback) == entry["status_code"]


def test_every_status_has_a_canonical_fallback_code():
    by_status: dict[int, list[str]] = {}
    for code in ErrorCode:
        by_status.setdefault(default_status(code), []).append(code.value)
    for status, codes in by_status.items():
        assert code_for_status(status) in codes, (status, codes)


def test_the_catalog_has_no_two_codes_that_mean_the_same_thing():
    # Guards against re-adding a synonym (e.g. a second 422 code alongside
    # ``validation_error``) that would make "which code does this failure carry"
    # ambiguous for an SDK. Sharing a status is fine; sharing a meaning is not.
    entries = error_catalog_table()
    assert len(entries) == len(ErrorCode)
    descriptions = [entry["description"] for entry in entries]
    assert len(descriptions) == len(set(descriptions))


def test_unknown_status_codes_still_produce_a_usable_code():
    assert code_for_status(418) == ErrorCode.BAD_REQUEST.value
    assert code_for_status(599) == ErrorCode.INTERNAL_ERROR.value


def test_error_payload_drops_empty_details_but_keeps_falsey_ones():
    payload = error_payload(ErrorCode.NOT_FOUND, "missing", project_id=None, retry_after=0)
    assert payload == {"code": "not_found", "message": "missing", "retry_after": 0}


def test_api_error_falls_back_to_the_catalog_status():
    error = ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, "clash")
    assert error.status_code == 409
    assert error.to_payload() == {"code": "idempotency_conflict", "message": "clash"}


def test_api_error_accepts_an_explicit_status_override():
    error = ApiError(ErrorCode.BAD_REQUEST, "nope", status_code=422, details={"field": "title"})
    assert error.status_code == 422
    assert error.to_payload()["field"] == "title"


# --------------------------------------------------------------------------- #
# 2. the envelope at the HTTP layer
# --------------------------------------------------------------------------- #


def test_error_codes_endpoint_publishes_the_catalog(client):
    status, _, body = client.request("GET", "/api/error-codes")
    assert status == 200
    codes = {entry["code"] for entry in body}
    assert {"not_found", "validation_error", "idempotency_conflict"} <= codes


def test_missing_resource_uses_the_shared_envelope(client):
    status, _, body = client.request("GET", "/api/projects/project_missing")
    assert status == 404
    assert body["detail"]["code"] == "not_found"
    assert body["detail"]["message"] == "project not found"


def test_validation_failure_uses_the_shared_envelope(client):
    status, _, body = client.request("POST", "/api/projects", json_body={})
    assert status == 422
    detail = body["detail"]
    assert detail["code"] == "validation_error"
    assert detail["errors"], "逐项校验原因必须保留，SDK 才能定位字段"
    assert any(item["loc"][-1] == "title" for item in detail["errors"])


def test_domain_gate_payload_passes_through_untouched(client):
    """A code-bearing dict detail must not be re-wrapped or flattened."""
    project = client.store.create_project(ProjectCreate(title="网关透传"))
    run = client.store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试", brand_name="品牌", product_name="产品"),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )
    client.store.session.commit()

    status, _, body = client.request(
        "POST",
        f"/api/runs/{run.run_id}/stages/storyboard/review",
        json_body={"decision": "approved"},
    )
    assert status == 409
    assert body["detail"]["code"] == "stage_not_awaiting_review"
    assert body["detail"]["stage_key"] == "storyboard"


def test_permission_refusal_keeps_its_gate_payload(client):
    status, _, body = client.request(
        "POST", "/api/projects", json_body={"title": "越权"}, actor_id="user_nobody"
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"
    assert body["detail"]["permission"] == "project:write"


# --------------------------------------------------------------------------- #
# 3. idempotency
# --------------------------------------------------------------------------- #


def test_a_retry_with_the_same_key_and_body_replays(client):
    headers = {HEADER: "retry-1"}
    first_status, _, first = client.request(
        "POST", "/api/projects", json_body={"title": "幂等项目"}, headers=headers
    )
    assert first_status == 200

    second_status, second_headers, second = client.request(
        "POST", "/api/projects", json_body={"title": "幂等项目"}, headers=headers
    )
    assert second_status == 200
    assert second == first
    assert second_headers.get("idempotency-replayed") == "true"
    assert len(client.store.list_project_summaries()) == 1


def test_reusing_a_key_with_a_different_body_is_refused(client):
    headers = {HEADER: "retry-2"}
    client.request("POST", "/api/projects", json_body={"title": "原请求"}, headers=headers)

    status, _, body = client.request(
        "POST", "/api/projects", json_body={"title": "改了内容"}, headers=headers
    )
    assert status == 409
    assert body["detail"]["code"] == "idempotency_conflict"
    assert body["detail"]["idempotency_key"] == "retry-2"
    assert len(client.store.list_project_summaries()) == 1


def test_requests_without_a_key_are_untouched(client):
    first = client.request("POST", "/api/projects", json_body={"title": "无 key"})[2]
    second = client.request("POST", "/api/projects", json_body={"title": "无 key"})[2]
    assert first["project_id"] != second["project_id"]
    assert client.store.list_idempotency_keys()[1] == 0


def test_keys_are_scoped_per_actor(client):
    _seat(client.store, "user_alpha", "user_beta")
    first_status, _, first = client.request(
        "POST",
        "/api/projects",
        json_body={"title": "A"},
        headers={HEADER: "shared-key"},
        actor_id="user_alpha",
    )
    second_status, _, second = client.request(
        "POST",
        "/api/projects",
        json_body={"title": "B"},
        headers={HEADER: "shared-key"},
        actor_id="user_beta",
    )
    # The same key from a different identity is a different logical operation.
    assert (first_status, second_status) == (200, 200)
    assert first["project_id"] != second["project_id"]


def test_an_in_flight_claim_is_refused(client):
    payload = json.dumps({"title": "挂起"}).encode()
    claim = client.store.begin_idempotent_request(
        _default_actor(),
        key="hang-1",
        method="POST",
        path="/api/projects",
        fingerprint=idempotency_module.request_fingerprint("POST", "/api/projects", payload),
    )
    assert claim.outcome is IdempotencyOutcome.CLAIMED

    status, _, body = client.request(
        "POST", "/api/projects", json_body={"title": "挂起"}, headers={HEADER: "hang-1"}
    )
    assert status == 409
    assert body["detail"]["code"] == "idempotency_in_progress"


def test_an_expired_claim_is_taken_over_not_refused(client):
    actor = _default_actor()
    client.store.begin_idempotent_request(
        actor,
        key="stale-1",
        method="POST",
        path="/api/projects",
        fingerprint="stale-fingerprint",
    )
    record = client.store.idempotency.find(
        tenant_id=actor.tenant_id, actor_id=actor.actor_id, idempotency_key="stale-1"
    )
    assert record is not None
    record.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    client.store.session.commit()

    status, _, body = client.request(
        "POST", "/api/projects", json_body={"title": "接手"}, headers={HEADER: "stale-1"}
    )
    assert status == 200, body
    assert body["title"] == "接手"


def test_a_failed_request_releases_its_claim(client):
    status, _, _ = client.request(
        "POST", "/api/projects", json_body={}, headers={HEADER: "bad-1"}
    )
    assert status == 422
    # The claim must not survive a refusal, otherwise the client could never fix
    # its payload under the same key.
    status, _, body = client.request(
        "POST", "/api/projects", json_body={"title": "修好了"}, headers={HEADER: "bad-1"}
    )
    assert status == 200, body


def test_a_long_key_is_refused_with_a_structured_error(client):
    status, _, body = client.request(
        "POST",
        "/api/projects",
        json_body={"title": "超长 key"},
        headers={HEADER: "x" * 300},
    )
    assert status == 400
    assert body["detail"]["code"] == "bad_request"


# --------------------------------------------------------------------------- #
# 4. pagination
# --------------------------------------------------------------------------- #


def test_page_model_reports_whether_more_rows_exist():
    assert Page[int](items=[1, 2], total=5, limit=2, offset=0).has_more
    assert not Page[int](items=[5], total=5, limit=2, offset=4).has_more


def _two_audited_teams(client) -> None:
    for slug in ("audit-a", "audit-b"):
        status, _, body = client.request(
            "POST", "/api/teams", json_body={"name": slug, "slug": slug}
        )
        assert status == 200, body


def test_audit_log_keeps_its_array_body_and_adds_a_total_count(client):
    _two_audited_teams(client)
    status, headers, body = client.request("GET", "/api/audit-logs?limit=1")
    assert status == 200
    assert isinstance(body, list) and len(body) == 1
    assert int(headers["x-total-count"]) >= 2


def test_audit_log_offset_walks_the_pages_without_gaps(client):
    _two_audited_teams(client)
    _, first_headers, first = client.request("GET", "/api/audit-logs?limit=1&offset=0")
    _, second_headers, second = client.request("GET", "/api/audit-logs?limit=1&offset=1")
    assert int(second_headers["x-total-count"]) == int(first_headers["x-total-count"]) >= 2
    assert first[0]["audit_id"] != second[0]["audit_id"]


def test_idempotency_keys_endpoint_returns_the_page_envelope(client):
    client.request(
        "POST", "/api/projects", json_body={"title": "分页"}, headers={HEADER: "page-1"}
    )
    status, _, body = client.request("GET", "/api/idempotency-keys")
    assert status == 200
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] >= 1
    assert body["items"][0]["idempotency_key"] == "page-1"
    assert body["items"][0]["state"] == "completed"


def test_idempotency_keys_can_be_filtered_by_state(client):
    client.request(
        "POST", "/api/projects", json_body={"title": "过滤"}, headers={HEADER: "filt-1"}
    )
    _, _, completed = client.request("GET", "/api/idempotency-keys?state=completed")
    _, _, in_progress = client.request("GET", "/api/idempotency-keys?state=in_progress")
    assert completed["total"] >= 1
    assert all(item["state"] == "completed" for item in completed["items"])
    assert in_progress["total"] == 0
