"""T97: tenant isolation on id-addressed resources.

Before this converged, the resource getters fetched by primary key alone: any
caller who could reach an endpoint could read any tenant's row by holding its
id. The guard (``apps/api/app/tenant_scope.py``) closes that at one point, and
these tests pin the contract it must keep:

* a cross-tenant id reads as **404 with the handler's own detail** — the same
  wire answer an absent id gets, so existence is never confirmed;
* a caller's own tenant is untouched (every pre-existing test proves that at
  scale; the controls here make it explicit);
* the expected tenant follows the identity actually presented — default actor,
  login session, or embed token.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harness import AsgiClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models
from apps.api.app.actor import SESSION_TOKEN_PREFIX
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.passwords import hash_password

TENANT_B = "tenant-b"

# (method, path, json_body) — one probe per guarded resolver. The cross-tenant
# row ids are seeded by _tenant_b_rows; the assertion is always 404 + the
# handler's own detail, because "foreign" and "absent" must be indistinguishable.
CROSS_TENANT_PROBES = [
    ("GET", "/api/projects/proj-b1", None, "project not found"),
    ("GET", "/api/projects/proj-b1/dashboard", None, "project not found"),
    ("GET", "/api/runs/run-b1", None, "run not found"),
    ("GET", "/api/runs/run-b1/board", None, "run not found"),
    ("POST", "/api/runs/run-b1/start", None, "run not found"),
    ("GET", "/api/runs/run-b1/assets", None, "run not found"),
    ("GET", "/api/assets/asset-b1", None, "asset not found"),
    ("GET", "/api/jobs/job-b1", None, "background job not found"),
    ("GET", "/api/tool-invocations/inv-b1", None, "tool invocation not found"),
    ("GET", "/api/review-requests/rr-b1", None, "review request not found"),
    ("GET", "/api/brand-kits/bk-b1", None, "brand kit not found"),
    ("GET", "/api/style-presets/sp-b1", None, "style preset not found"),
    ("GET", "/api/asset-collections/col-b1", None, "asset collection not found"),
    (
        "PATCH",
        "/api/asset-library/items/item-b1",
        {"usage_role": "hero"},
        "asset library item not found",
    ),
    ("GET", "/api/provider-credentials/cred-b1", None, "provider credential not found"),
    ("GET", "/api/webhooks/ws-b1", None, "webhook subscription not found"),
    ("GET", "/api/teams/team-b1", None, "team not found"),
    (
        "PATCH",
        "/api/team-memberships/tm-b1",
        {"role": "member"},
        "team membership not found",
    ),
]


def _tenant_b_rows(session: Session) -> None:
    """Seed one complete tenant-B resource tree, every FK satisfied.

    All ids carry the ``-b1``/``-b`` shape so a typo in a probe reads as a
    missing row (404 either way — that is the point) instead of silently
    hitting a default-tenant row. Each row is flushed before the next is added:
    parents must be visible to the FK checks while the batch is still open.
    """
    session.add(db_models.TenantRecord(id=TENANT_B, name="Tenant B"))
    session.flush()
    session.add(
        db_models.UserRecord(
            id="user-b1",
            tenant_id=TENANT_B,
            email="b1@tenant-b.test",
            name="b1",
            password_hash=hash_password("pw-b1"),
            password_updated_at=datetime.now(UTC),
        )
    )
    session.flush()
    session.add(
        db_models.ProjectRecord(id="proj-b1", tenant_id=TENANT_B, title="B project")
    )
    session.flush()
    session.add(
        db_models.ProductionRunRecord(
            id="run-b1",
            tenant_id=TENANT_B,
            project_id="proj-b1",
            pipeline_key="ugc_short",
        )
    )
    session.flush()
    session.add(
        db_models.AssetRecord(
            id="asset-b1",
            run_id="run-b1",
            asset_type="audio",
            storage_url="s3://bucket/tenant-b/asset-b1.wav",
        )
    )
    session.flush()
    session.add(
        db_models.BackgroundJobRecord(
            id="job-b1",
            tenant_id=TENANT_B,
            run_id="run-b1",
            job_type="tts",
        )
    )
    session.flush()
    session.add(
        db_models.ToolInvocationRecord(
            id="inv-b1", run_id="run-b1", tool_key="tts", provider="local"
        )
    )
    session.flush()
    session.add(
        db_models.StageRunRecord(
            id="stage-b1", run_id="run-b1", stage_key="script_review", stage_order=1
        )
    )
    session.flush()
    session.add(
        db_models.ReviewRequestRecord(
            id="rr-b1",
            tenant_id=TENANT_B,
            project_id="proj-b1",
            run_id="run-b1",
            stage_run_id="stage-b1",
            stage_key="script_review",
            pipeline_key="ugc_short",
        )
    )
    session.flush()
    session.add(
        db_models.BrandKitRecord(
            id="bk-b1", tenant_id=TENANT_B, project_id="proj-b1", name="B brand"
        )
    )
    session.flush()
    session.add(
        db_models.StylePresetRecord(
            id="sp-b1", tenant_id=TENANT_B, project_id="proj-b1", name="B preset"
        )
    )
    session.flush()
    session.add(
        db_models.AssetCollectionRecord(
            id="col-b1",
            tenant_id=TENANT_B,
            project_id="proj-b1",
            kind="person",
            name="B collection",
        )
    )
    session.flush()
    session.add(
        db_models.AssetCollectionItemRecord(
            id="item-b1",
            tenant_id=TENANT_B,
            collection_id="col-b1",
            asset_id="asset-b1",
        )
    )
    session.flush()
    session.add(
        db_models.ProviderCredentialRecord(
            id="cred-b1", tenant_id=TENANT_B, provider="dashscope"
        )
    )
    session.flush()
    session.add(
        db_models.WebhookSubscriptionRecord(
            id="ws-b1",
            tenant_id=TENANT_B,
            name="B webhook",
            url="https://tenant-b.example/hook",
            secret="b" * 32,
            event_types=["run.completed"],
        )
    )
    session.flush()
    session.add(db_models.WebhookDeliveryRecord(
        id="wd-b1",
        tenant_id=TENANT_B,
        subscription_id="ws-b1",
        event_id="evt-b1",
        event_type="run.completed",
    ))
    session.add(
        db_models.TeamRecord(
            id="team-b1", tenant_id=TENANT_B, name="B team", slug="b-team"
        )
    )
    session.flush()
    session.add(
        db_models.TeamMembershipRecord(
            id="tm-b1",
            tenant_id=TENANT_B,
            team_id="team-b1",
            user_id="user-b1",
        )
    )
    session.commit()


@pytest.fixture()
def asgi(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tenants.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    _tenant_b_rows(Session(engine))
    client = AsgiClient(DatabaseStore(Session(engine)), engine)
    try:
        yield client
    finally:
        client.close()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login_b1(asgi) -> str:
    status, _, body = asgi.request(
        "POST",
        "/api/auth/login",
        json_body={"email": "b1@tenant-b.test", "password": "pw-b1"},
    )
    assert status == 200, body
    return body["token"]


@pytest.mark.parametrize(("method", "path", "payload", "detail"), CROSS_TENANT_PROBES)
def test_a_cross_tenant_id_reads_as_the_handler_s_own_404(asgi, method, path, payload, detail):
    """Foreign id and absent id are indistinguishable: same status, same detail."""
    foreign = asgi.request(method, path, json_body=payload)
    missing = asgi.request(method, path.replace("-b1", "absent"), json_body=payload)
    assert foreign[0] == 404, (foreign, missing)
    # The app renders every 404 through the shared envelope; the guard's message
    # is the handler's own, and the two shapes match field for field.
    assert foreign[2]["detail"]["message"] == detail
    assert foreign[2]["detail"]["code"] == "not_found"
    assert missing[0] == 404
    assert missing[2]["detail"] == foreign[2]["detail"]


def test_an_own_tenant_read_is_untouched(asgi):
    status, _, project = asgi.request(
        "POST", "/api/projects", json_body={"title": "mine"}
    )
    assert status == 200
    project_id = project["project_id"]
    status, _, fetched = asgi.request("GET", f"/api/projects/{project_id}")
    assert status == 200
    assert fetched["project_id"] == project_id


def test_the_project_list_shows_only_the_callers_tenant(asgi):
    asgi.request("POST", "/api/projects", json_body={"title": "mine"})
    status, _, summaries = asgi.request("GET", "/api/projects")
    assert status == 200
    ids = {s["project"]["project_id"] for s in summaries}
    assert "proj-b1" not in ids
    assert len([i for i in ids if i.startswith("proj")]) == len(ids)


def test_the_credential_list_shows_only_the_callers_tenant(asgi):
    status, _, credentials = asgi.request("GET", "/api/provider-credentials")
    assert status == 200
    assert all(c["credential_id"] != "cred-b1" for c in credentials)


def test_a_session_identity_reads_its_own_tenant_and_not_yours(asgi):
    token = _login_b1(asgi)
    # Tenant B's user reads tenant B's project — the row the default actor gets
    # a 404 for is this caller's own resource.
    status, _, project = asgi.request(
        "GET", "/api/projects/proj-b1", headers=_bearer(token)
    )
    assert status == 200
    assert project["project_id"] == "proj-b1"
    # A create under a session identity lands in the session's tenant, so the
    # guard reads back the same tenant on the way out.
    status, _, created = asgi.request(
        "POST", "/api/projects", json_body={"title": "b-side"}, headers=_bearer(token)
    )
    assert status == 200
    status, _, _fetched = asgi.request(
        "GET", f"/api/projects/{created['project_id']}", headers=_bearer(token)
    )
    assert status == 200
    # ...and the default actor is fenced out of the session's tenant exactly as
    # the session is fenced out of the default one (next test).
    status, _, _ = asgi.request("GET", f"/api/projects/{created['project_id']}")
    assert status == 404


def test_a_session_identity_cannot_reach_the_default_tenant(asgi):
    token = _login_b1(asgi)
    status, _, project = asgi.request(
        "POST", "/api/projects", json_body={"title": "default-side"}
    )
    assert status == 200
    foreign_to_b = project["project_id"]
    status, _, body = asgi.request(
        "GET", f"/api/projects/{foreign_to_b}", headers=_bearer(token)
    )
    assert status == 404
    assert body["detail"]["message"] == "project not found"
    status, _, listed = asgi.request("GET", "/api/projects", headers=_bearer(token))
    assert status == 200
    assert all(s["project"]["project_id"] != foreign_to_b for s in listed)


def test_login_tokens_keep_their_prefix(asgi):
    token = _login_b1(asgi)
    assert token.startswith(SESSION_TOKEN_PREFIX)


def test_a_cross_tenant_password_set_is_refused_without_touching_the_user(asgi):
    """The password endpoint is an upsert for *your* tenant (T96); across the
    fence it is a 404, and the foreign user's credential is untouched."""
    status, _, body = asgi.request(
        "POST",
        "/api/users/user-b1/password",
        json_body={"password": "attacker-choice"},
    )
    assert status == 404
    assert body["detail"]["message"] == "user not found"
    # The seeded tenant-B user still logs in with the original password.
    status, _, login = asgi.request(
        "POST",
        "/api/auth/login",
        json_body={"email": "b1@tenant-b.test", "password": "pw-b1"},
    )
    assert status == 200, login
