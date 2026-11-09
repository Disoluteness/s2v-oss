"""V6.5 teams, memberships and request identity.

Three layers are pinned here, from cheapest to most end-to-end:

1. **The kernel** — the role→permission matrices and the union rule. These are
   pure functions, so they get asserted directly.
2. **The policy tables** — ``POLICY`` plus the explicit ``OPEN_ENDPOINTS`` must
   partition the registered routes exactly. That is what stops a new endpoint
   from quietly shipping without a decision about who may call it.
3. **Enforcement** — requests are driven through the real ASGI application (no
   ``httpx``: it is not a dependency, see ``tests/test_review_api.py``), so the
   guard sees exactly what Starlette hands it. This is the layer that would have
   caught the mount-prefix bug, and the one that proves *every* gated endpoint
   refuses an unregistered identity.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.routing import _IncludedRouter
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models, routes  # noqa: F401  (db_models registers the tables)
from apps.api.app.actor import ActorContext
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.models import (
    ActorProfile,
    ProjectCreate,
    ProjectMemberAdd,
    ProjectMemberUpdate,
    RunCreate,
    TeamCreate,
    TeamMemberAdd,
    TeamMemberUpdate,
    TeamUpdate,
)
from apps.api.app.permission_policy import (
    OPEN_ENDPOINTS,
    POLICY,
    _request_template,
)
from apps.api.app.permissions import (
    ALL_PERMISSIONS,
    PROJECT_ROLE_PERMISSIONS,
    READ_ONLY_PERMISSIONS,
    TEAM_ROLE_PERMISSIONS,
    TENANT_ONLY_PERMISSIONS,
    Permission,
    PermissionDenied,
    ProjectRole,
    TeamRole,
    effective_permissions,
    has_permission,
    permission_matrix,
)
from apps.api.app.repositories import (
    DEFAULT_TENANT_ID,
    ProjectMembershipOutcome,
    TeamMembershipOutcome,
)
from apps.api.app.routes import get_store
from packages.pipeline_kit.loader import PipelineLoader

DEFAULT_ACTOR = "system"
GHOST_ACTOR = "ghost_actor"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _store(tmp_path, name: str = "team-permissions.db") -> DatabaseStore:
    # ``check_same_thread=False`` is required because sync endpoints run in a
    # worker thread while this session was created on the test thread.
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return DatabaseStore(Session(engine))


def _default_actor() -> ActorContext:
    """The fallback identity: no membership yet, and therefore unseated."""
    return ActorContext(
        actor_id=DEFAULT_ACTOR,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


def _header_actor(actor_id: str) -> ActorContext:
    return ActorContext(actor_id=actor_id, tenant_id=DEFAULT_TENANT_ID, source="header")


def _seeded(tmp_path):
    """A project + run + a team holding one viewer and one reviewer seat."""
    store = _store(tmp_path)
    project = store.create_project(ProjectCreate(title="团队权限项目"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶", brand_name="茶小朵", product_name="冲泡奶茶"),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )
    owner = _default_actor()
    store.ensure_default_seat(owner)
    store.session.commit()
    team_id = store.actor_seat(owner)[0]
    assert team_id is not None

    store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_viewer", role=TeamRole.VIEWER.value), owner
    )
    store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_reviewer", role=TeamRole.VIEWER.value), owner
    )
    store.session.commit()
    store.add_project_member(
        project.project_id,
        ProjectMemberAdd(user_id="user_reviewer", role=ProjectRole.REVIEWER.value),
        owner,
    )
    store.session.commit()
    return store, project, run, team_id


class _AsgiClient:
    """Drive the real app straight through ASGI, bypassing ``httpx``.

    ``fastapi.testclient`` needs ``httpx``, which this project does not depend
    on, so requests are built by hand. Going through the actual application is
    the point: it is the only way to see what the framework really puts in
    ``scope`` for the guard to read.
    """

    def __init__(self, store: DatabaseStore) -> None:
        self.store = store
        app.dependency_overrides[get_store] = lambda: store

    def close(self) -> None:
        app.dependency_overrides.pop(get_store, None)

    async def _call(self, method, path, *, actor_id=None, body=b"") -> tuple[int, bytes]:
        headers = [(b"x-actor-id", actor_id.encode())] if actor_id else []
        if body:
            headers.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("testserver", 80),
            "client": ("testclient", 1),
        }
        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await app(scope, receive, send)

        status = next(
            (m.get("status") for m in messages if m["type"] == "http.response.start"),
            None,
        )
        payload = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return int(status or 0), payload

    def request(self, method, path, *, actor_id=None, json_body: dict | None = None):
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        return asyncio.run(self._call(method, path, actor_id=actor_id, body=body))

    def status(self, method, path, *, actor_id=None) -> int:
        return self.request(method, path, actor_id=actor_id)[0]


@pytest.fixture()
def client(tmp_path):
    store = _store(tmp_path)
    asgi = _AsgiClient(store)
    try:
        yield asgi
    finally:
        asgi.close()


# --------------------------------------------------------------------------- #
# 1. the kernel: role matrices and the union rule
# --------------------------------------------------------------------------- #


def test_every_permission_key_is_reachable_from_the_matrix():
    # V8.5 added two: deployment:read (granted broadly — reading posture is
    # ordinary operational self-service) and deployment:manage (tenant-only, so a
    # project role cannot flip the whole install into maintenance mode).
    assert len(Permission) == 23
    # Every key is granted to at least one role, and the union of all roles
    # covers the whole vocabulary — otherwise a key would be unassignable and
    # dead code in the policy.
    union = set()
    for role in TEAM_ROLE_PERMISSIONS:
        union |= TEAM_ROLE_PERMISSIONS[role]
    for role in PROJECT_ROLE_PERMISSIONS:
        union |= PROJECT_ROLE_PERMISSIONS[role]
    assert union == ALL_PERMISSIONS


def test_team_owner_holds_every_permission_but_admin_lacks_team_manage():
    assert TEAM_ROLE_PERMISSIONS[TeamRole.OWNER] == ALL_PERMISSIONS
    assert Permission.TEAM_MANAGE not in TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN]
    assert TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN] == ALL_PERMISSIONS - {Permission.TEAM_MANAGE}
    # An admin may not dissolve the team, but may still issue credentials: that
    # is an operational capability, not an ownership one. Webhook subscriptions
    # ride the same line — an operational, tenant-wide capability.
    assert Permission.APIKEY_MANAGE in TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN]
    assert Permission.APIKEY_MANAGE not in TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER]
    assert Permission.APIKEY_MANAGE not in TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER]
    assert Permission.WEBHOOK_MANAGE in TEAM_ROLE_PERMISSIONS[TeamRole.ADMIN]
    assert Permission.WEBHOOK_MANAGE not in TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER]
    assert Permission.WEBHOOK_MANAGE not in TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER]


def test_viewer_is_read_only():
    assert TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER] == READ_ONLY_PERMISSIONS
    assert TEAM_ROLE_PERMISSIONS[TeamRole.VIEWER] == PROJECT_ROLE_PERMISSIONS[
        ProjectRole.VIEWER
    ]
    for read in sorted(READ_ONLY_PERMISSIONS, key=lambda p: p.value):
        assert has_permission(read, team_role=TeamRole.VIEWER)
    assert not has_permission(Permission.RUN_START, team_role=TeamRole.VIEWER)
    assert not has_permission(Permission.RUN_WRITE, team_role=TeamRole.VIEWER)


def test_team_member_never_reaches_delete_or_member_management():
    member = TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER]
    assert Permission.PROJECT_DELETE not in member
    assert Permission.MEMBER_MANAGE not in member
    assert Permission.TEAM_MANAGE not in member
    assert Permission.RUN_REVIEW in member


def test_project_role_can_never_grant_a_tenant_only_permission():
    for role in PROJECT_ROLE_PERMISSIONS:
        for tenant_only in TENANT_ONLY_PERMISSIONS:
            assert tenant_only not in PROJECT_ROLE_PERMISSIONS[role]
    # Even the strongest project role cannot escalate to tenant administration,
    # which is what keeps project membership from being a back door. Issuing an
    # API key is tenant-wide too, so it rides the same rule.
    for tenant_only in TENANT_ONLY_PERMISSIONS:
        assert not has_permission(
            tenant_only,
            team_role=None,
            project_role=ProjectRole.OWNER,
        )


def test_effective_permissions_is_a_union_not_an_intersection():
    baseline = effective_permissions(TeamRole.VIEWER)
    widened = effective_permissions(TeamRole.VIEWER, ProjectRole.REVIEWER)

    assert baseline < widened
    assert baseline | PROJECT_ROLE_PERMISSIONS[ProjectRole.REVIEWER] == widened
    assert Permission.RUN_REVIEW in widened
    assert Permission.RUN_REVIEW not in baseline

    # A project role that is narrower than the team role must not revoke anything.
    assert effective_permissions(TeamRole.OWNER, ProjectRole.VIEWER) == ALL_PERMISSIONS


def test_effective_permissions_tolerates_unknown_and_missing_roles():
    assert effective_permissions(None) == frozenset()
    assert effective_permissions("nonsense") == frozenset()
    assert effective_permissions(None, "nonsense") == frozenset()
    assert effective_permissions(TeamRole.MEMBER) == TEAM_ROLE_PERMISSIONS[TeamRole.MEMBER]


def test_permission_matrix_exposes_every_role_and_key():
    matrix = permission_matrix()
    assert len(matrix["permissions"]) == len(Permission)
    assert {entry["role"] for entry in matrix["team_roles"]} == {r.value for r in TeamRole}
    assert {entry["role"] for entry in matrix["project_roles"]} == {
        r.value for r in ProjectRole
    }
    assert Permission.TEAM_MANAGE not in {
        p for entry in matrix["project_roles"] for p in entry["permissions"]
    }
    assert Permission.APIKEY_MANAGE not in {
        p for entry in matrix["project_roles"] for p in entry["permissions"]
    }
    assert Permission.WEBHOOK_MANAGE not in {
        p for entry in matrix["project_roles"] for p in entry["permissions"]
    }


def test_permission_denied_payload_is_flat_and_machine_readable():
    exc = PermissionDenied(
        Permission.RUN_START,
        actor_id="user_viewer",
        actor_source="header",
        team_role="viewer",
        project_role=None,
        details={"project_id": "project_1"},
    )
    assert exc.code == "permission_denied"
    assert exc.status_code == 403
    payload = exc.to_payload()
    assert payload["code"] == "permission_denied"
    assert payload["permission"] == "run:start"
    assert payload["actor_id"] == "user_viewer"
    assert payload["project_id"] == "project_1"


# --------------------------------------------------------------------------- #
# 2. the policy tables partition the route table
# --------------------------------------------------------------------------- #


def _registered_routes() -> set[tuple[str, str]]:
    """Every ``(METHOD, full path template)`` the API actually serves."""
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, _IncludedRouter):
            for context in route.effective_route_contexts():
                for method in context.methods or set():
                    found.add((method.upper(), context.path))
    return found


def test_policy_and_open_endpoints_partition_every_route():
    registered = _registered_routes()
    gated = set(POLICY)
    open_paths = set(OPEN_ENDPOINTS)

    assert gated & open_paths == set(), "端点不能同时受控与开放"
    assert gated | open_paths == registered, (
        "每个端点都必须显式地二选一：纳入 POLICY 或登记进 OPEN_ENDPOINTS，"
        "否则新端点会在无人决策的情况下悄悄上线"
    )
    # V7.1 added two endpoints: /api/error-codes (open, reference data) and
    # /api/idempotency-keys (gated, member:read). V7.2 added three gated ones:
    # GET/POST /api/api-keys and POST /api/api-keys/{id}/revoke (apikey:manage).
    # V7.3 added nine: eight gated webhook endpoints (webhook:manage) plus the
    # open /api/webhook-events catalog (reference data, no tenant rows).
    # V7.4 added seven: six gated integration endpoints (integration:manage —
    # the CRUD surface plus the embed-session mint) and the open
    # /api/embed/{id}/session bootstrap. Minting is gated on purpose — it is the
    # tenant's control surface, not the embed's.
    # V6.6 added three review-attachment endpoints; they ride the existing
    # run:read / run:review permissions, so no new permission key.
    # V8.1 added one gated endpoint: POST /api/jobs/{id}/requeue replays a
    # dead-lettered job (run:write + SCOPE_JOB), no new permission key.
    # V8.3 added six: four tenant-scoped quota endpoints (list, read, upsert,
    # delete), one run-scoped override, and one project-scoped attribution
    # endpoint. The last two ride ``quota:manage`` — a tenant-only key — so a
    # project role can neither exempt its own run nor move a project onto a team
    # with room, which is the same thing as raising its own ceiling.
    # V8.4 moved five provider-credential endpoints out of ``OPEN_ENDPOINTS``
    # (V6.5 deferred them here) and added one more: POST .../rotate seals secrets
    # into the vault. All six ride ``provider:manage``, a new tenant-only key, so
    # a project role cannot read partial secret previews or point the tenant's
    # paid calls at another account.
    # V8.5 added four gated endpoints under /api/deployment: read the overview,
    # update the label, flip maintenance mode, record a backup. Reads ride
    # ``deployment:read`` and writes ``deployment:manage`` (tenant-only).
    # V1.1 added four: POST /api/users/{id}/password is gated (member:manage at
    # tenant scope — seating a login identity is roster administration), and the
    # three /api/auth operations are open (login is identity bootstrap; logout
    # and introspection take the session token itself as the credential).
    # P2 added one: GET /api/runs/{run_id}/decision-log rides ``run:read`` at run
    # scope, the same key that already guards the board and the artifact list.
    assert len(registered) == 157
    assert len(gated) == 138
    assert len(open_paths) == 19


def test_every_policy_entry_names_a_real_permission_and_scope():
    from apps.api.app.permission_policy import (
        SCOPE_ASSET,
        SCOPE_BRAND_KIT,
        SCOPE_COLLECTION,
        SCOPE_COMMENT,
        SCOPE_JOB,
        SCOPE_PROJECT,
        SCOPE_REVIEW,
        SCOPE_REVIEW_ATTACHMENT,
        SCOPE_RUN,
        SCOPE_STYLE_PRESET,
        SCOPE_TENANT,
        SCOPE_TOOL_INVOCATION,
    )

    known_scopes = {
        SCOPE_TENANT,
        SCOPE_PROJECT,
        SCOPE_RUN,
        SCOPE_REVIEW,
        SCOPE_REVIEW_ATTACHMENT,
        SCOPE_COMMENT,
        SCOPE_COLLECTION,
        SCOPE_ASSET,
        SCOPE_JOB,
        SCOPE_TOOL_INVOCATION,
        SCOPE_BRAND_KIT,
        SCOPE_STYLE_PRESET,
    }
    for key, (permission, scope) in POLICY.items():
        assert isinstance(permission, Permission), key
        assert scope in known_scopes, key
        assert key[0] == key[0].upper(), key
        assert key[1].startswith("/api/"), key


def test_open_endpoints_all_carry_a_reason():
    for key, reason in OPEN_ENDPOINTS.items():
        assert isinstance(reason, str) and reason.strip(), key


def test_no_route_uses_a_multi_segment_path_converter():
    """The prefix recovery assumes one placeholder == one path segment."""
    for _method, template in _registered_routes():
        assert ":path" not in template, template


@pytest.mark.parametrize(
    ("request_path", "template", "expected"),
    [
        # The guard must rebuild the *fully prefixed* template: ``scope["route"]``
        # only knows the router-relative path, and policy keys are written in
        # full request form.
        ("/api/projects", "/projects", "/api/projects"),
        (
            "/api/projects/project_1/runs",
            "/projects/{project_id}/runs",
            "/api/projects/{project_id}/runs",
        ),
        (
            "/api/runs/run_1/stages/plan/review",
            "/runs/{run_id}/stages/{stage_key}/review",
            "/api/runs/{run_id}/stages/{stage_key}/review",
        ),
        ("/api/asset-library/items/item_1", "/asset-library/items/{item_id}",
         "/api/asset-library/items/{item_id}"),
        # A route mounted at the root has no prefix to recover.
        ("/health", "/health", "/health"),
        ("/", "/", "/"),
    ],
)
def test_request_template_rebuilds_the_prefixed_path(request_path, template, expected):
    request = _fake_request(request_path, route_path=template)
    assert _request_template(request) == expected


def test_request_template_without_a_route_is_none():
    assert _request_template(_fake_request("/api/projects", route_path=None)) is None


def _fake_request(path: str, *, route_path: str | None):
    from starlette.requests import Request

    route = type("_Route", (), {"path": route_path})() if route_path else None
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "path_params": {},
    }
    if route is not None:
        scope["route"] = route
    return Request(scope)


# --------------------------------------------------------------------------- #
# 3. enforcement, driven through the real application
# --------------------------------------------------------------------------- #


def test_every_gated_endpoint_refuses_an_unregistered_identity(tmp_path):
    """No seat means no permissions, on every single gated endpoint.

    This doubles as the reachability proof: an unregistered actor is denied by
    the *team* baseline alone, so a 403 here means the guard resolved a policy
    entry for that endpoint. A miss in the lookup (the old mount-prefix bug)
    would show up as a 200/404/422 instead.
    """
    store = _store(tmp_path, "gated.db")
    asgi = _AsgiClient(store)
    try:
        failures = []
        for method, template in sorted(POLICY):
            path = re.sub(r"\{[^}]+\}", "probe_id", template)
            status = asgi.status(method, path, actor_id=GHOST_ACTOR)
            if status != 403:
                failures.append((method, template, status))
        assert failures == []
    finally:
        asgi.close()


def test_seated_roles_are_allowed_and_unseated_identities_are_not(tmp_path):
    store, project, run, _team_id = _seeded(tmp_path)
    asgi = _AsgiClient(store)
    try:
        # The default identity is auto-seated as owner and keeps working, which
        # is what preserves the deployment gate and every pre-V6.5 caller.
        assert asgi.status("GET", "/api/projects") == 200
        assert asgi.status("GET", f"/api/projects/{project.project_id}") == 200
        assert asgi.status("GET", f"/api/runs/{run.run_id}/board") == 200

        # A read-only seat reads but never writes.
        assert asgi.status("GET", "/api/projects", actor_id="user_viewer") == 200
        assert asgi.status("GET", "/api/teams", actor_id="user_viewer") == 200
        assert (
            asgi.status("POST", "/api/projects", actor_id="user_viewer") == 403
        )

        # A header identity with no seat at all gets nothing, not even reads.
        assert asgi.status("GET", "/api/projects", actor_id="user_stranger") == 403
    finally:
        asgi.close()


def test_project_role_widens_without_leaking_into_tenant_scope(tmp_path):
    store, project, run, _team = _seeded(tmp_path)
    asgi = _AsgiClient(store)
    try:
        # ``user_reviewer`` is a team viewer whose project seat is ``reviewer``.
        assert (
            asgi.status("GET", f"/api/runs/{run.run_id}/board", actor_id="user_reviewer")
            == 200
        )
        # ``run:review`` comes from the project seat (the team seat is read-only).
        assert Permission.RUN_REVIEW in store.permissions_for_project(
            _header_actor("user_reviewer"), project.project_id
        )
        # …but ``run:start`` still needs the team baseline it does not have.
        assert (
            asgi.status(
                "POST", f"/api/runs/{run.run_id}/start", actor_id="user_reviewer"
            )
            == 403
        )
        # The project seat must not leak into tenant-scoped endpoints.
        assert asgi.status("POST", "/api/teams", actor_id="user_reviewer") == 403
    finally:
        asgi.close()


def test_refusal_is_audited_before_the_request_is_aborted(tmp_path):
    store, _project, _run, _team = _seeded(tmp_path)
    asgi = _AsgiClient(store)
    try:
        assert asgi.status("POST", "/api/projects", actor_id="user_viewer") == 403
        entries = store.list_audit_logs(action="permission.denied", limit=50)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.actor_id == "user_viewer"
        assert entry.entity_type == "permission"
        assert entry.entity_id == Permission.PROJECT_WRITE.value
        assert entry.payload["permission"] == Permission.PROJECT_WRITE.value
        assert entry.payload["team_role"] == TeamRole.VIEWER.value
    finally:
        asgi.close()


def test_denied_response_carries_the_structured_payload(tmp_path):
    store, _project, _run, _team = _seeded(tmp_path)
    asgi = _AsgiClient(store)
    try:
        status, body = asgi.request("POST", "/api/projects", actor_id="user_viewer")
        assert status == 403
        detail = _json_loads(body)["detail"]
        assert detail["code"] == "permission_denied"
        assert detail["permission"] == "project:write"
        assert detail["actor_id"] == "user_viewer"
        assert detail["team_role"] == TeamRole.VIEWER.value
    finally:
        asgi.close()


def _json_loads(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def test_health_and_reference_endpoints_stay_open(tmp_path):
    store = _store(tmp_path, "open.db")
    asgi = _AsgiClient(store)
    try:
        for method, template in sorted(OPEN_ENDPOINTS):
            if template != "/api/health":
                continue
            assert asgi.status(method, template, actor_id=GHOST_ACTOR) == 200
        # The identity bootstrap endpoint has to answer before permissions exist.
        status, body = asgi.request("GET", "/api/me", actor_id=GHOST_ACTOR)
        assert status == 200
        profile = ActorProfile(**_json_loads(body))
        assert profile.actor_id == GHOST_ACTOR
        assert profile.source == "header"
        assert profile.team_role is None
        assert profile.permissions == []
    finally:
        asgi.close()


def test_default_actor_is_auto_seated_as_team_owner(tmp_path):
    store = _store(tmp_path, "seat.db")
    asgi = _AsgiClient(store)
    try:
        status, body = asgi.request("GET", "/api/me")
        assert status == 200
        profile = ActorProfile(**_json_loads(body))
        assert profile.actor_id == DEFAULT_ACTOR
        assert profile.source == "default"
        assert profile.team_role == TeamRole.OWNER.value
        assert profile.team_id is not None
        assert sorted(profile.permissions) == sorted(p.value for p in ALL_PERMISSIONS)
    finally:
        asgi.close()


# --------------------------------------------------------------------------- #
# 4. the team / membership facade
# --------------------------------------------------------------------------- #


def test_ensure_default_seat_is_idempotent(tmp_path):
    store = _store(tmp_path, "seat-idempotent.db")
    owner = _default_actor()
    store.ensure_default_seat(owner)
    store.session.commit()
    first = store.actor_seat(owner)

    store.ensure_default_seat(owner)
    store.session.commit()
    second = store.actor_seat(owner)

    assert first == second
    assert first[1] == TeamRole.OWNER
    members = store.list_team_members(first[0])
    assert members is not None
    assert [m.user_id for m in members] == [DEFAULT_ACTOR]


def test_header_identity_is_never_auto_seated(tmp_path):
    store = _store(tmp_path, "no-autoseat.db")
    store.ensure_default_seat(_header_actor("user_stranger"))
    store.session.commit()
    assert store.actor_seat(_header_actor("user_stranger")) == (None, None)
    assert store.permissions_for_project(_header_actor("user_stranger"), None) == frozenset()


def test_adding_a_team_member_twice_updates_in_place(tmp_path):
    store, _project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()

    outcome, membership = store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_viewer", role=TeamRole.MEMBER.value), owner
    )
    assert outcome is TeamMembershipOutcome.UPDATED
    assert membership is not None
    assert membership.role == TeamRole.MEMBER.value
    assert membership.status == "active"

    members = store.list_team_members(team_id)
    assert members is not None
    assert [m.user_id for m in members].count("user_viewer") == 1


def test_team_member_role_change_and_soft_removal(tmp_path):
    store, _project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()
    members = store.list_team_members(team_id)
    assert members is not None
    viewer = next(m for m in members if m.user_id == "user_viewer")

    updated = store.update_team_member(
        viewer.membership_id, TeamMemberUpdate(role=TeamRole.ADMIN.value), owner
    )
    assert updated is not None
    assert updated.role == TeamRole.ADMIN.value

    removed = store.remove_team_member(viewer.membership_id, owner)
    assert removed is not None
    assert removed.status == "removed"

    # A removed seat is gone from the active list but recoverable.
    active = store.list_team_members(team_id)
    assert active is not None
    assert "user_viewer" not in [m.user_id for m in active]
    with_removed = store.list_team_members(team_id, include_removed=True)
    assert with_removed is not None
    assert "user_viewer" in [m.user_id for m in with_removed]
    assert store.actor_seat(_header_actor("user_viewer")) == (None, None)


def test_removing_a_member_revokes_permissions_immediately(tmp_path):
    store, _project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()
    actor = _header_actor("user_viewer")
    assert Permission.PROJECT_READ in store.permissions_for_project(actor, None)

    members = store.list_team_members(team_id)
    assert members is not None
    viewer = next(m for m in members if m.user_id == "user_viewer")
    store.remove_team_member(viewer.membership_id, owner)

    assert Permission.PROJECT_READ not in store.permissions_for_project(actor, None)
    with pytest.raises(PermissionDenied):
        store.require_permission(actor, Permission.PROJECT_READ)


def test_invalid_roles_are_rejected_rather_than_stored(tmp_path):
    store, project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()

    outcome, membership = store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_bad", role="superuser"), owner
    )
    assert outcome is TeamMembershipOutcome.INVALID_ROLE
    assert membership is None

    outcome, membership = store.add_project_member(
        project.project_id,
        ProjectMemberAdd(user_id="user_bad", role="superuser"),
        owner,
    )
    assert outcome is ProjectMembershipOutcome.INVALID_ROLE
    assert membership is None


def test_membership_against_unknown_parents_reports_not_found(tmp_path):
    store = _store(tmp_path, "unknown-parent.db")
    owner = _default_actor()

    outcome, _ = store.add_team_member(
        "team_missing", TeamMemberAdd(user_id="user_x", role="member"), owner
    )
    assert outcome is TeamMembershipOutcome.TEAM_NOT_FOUND
    assert store.list_team_members("team_missing") is None

    outcome, _ = store.add_project_member(
        "project_missing", ProjectMemberAdd(user_id="user_x", role="editor"), owner
    )
    assert outcome is ProjectMembershipOutcome.PROJECT_NOT_FOUND
    assert store.list_project_members("project_missing") is None

    assert store.update_team_member("membership_missing", TeamMemberUpdate(role="admin"), owner) is None
    assert store.remove_team_member("membership_missing", owner) is None
    assert (
        store.update_project_member(
            "membership_missing", ProjectMemberUpdate(role="editor"), owner
        )
        is None
    )
    assert store.remove_project_member("membership_missing", owner) is None


def test_project_member_role_change_frees_the_wider_seat(tmp_path):
    store, project, _run, _team = _seeded(tmp_path)
    owner = _default_actor()
    members = store.list_project_members(project.project_id)
    assert members is not None
    reviewer = next(m for m in members if m.user_id == "user_reviewer")

    actor = _header_actor("user_reviewer")
    assert Permission.RUN_REVIEW in store.permissions_for_project(actor, project.project_id)

    store.update_project_member(
        reviewer.membership_id, ProjectMemberUpdate(role=ProjectRole.VIEWER.value), owner
    )
    assert Permission.RUN_REVIEW not in store.permissions_for_project(
        actor, project.project_id
    )
    # The team baseline is untouched by a project-side demotion.
    assert Permission.PROJECT_READ in store.permissions_for_project(actor, project.project_id)


def test_team_management_actions_are_audited(tmp_path):
    store = _store(tmp_path, "audit.db")
    owner = _default_actor()
    team = store.create_team(TeamCreate(name="内容组", slug="content"), owner)
    assert team.slug == "content"

    store.update_team(team.team_id, TeamUpdate(description="短视频组"), owner)
    _outcome, membership = store.add_team_member(
        team.team_id, TeamMemberAdd(user_id="user_a", role=TeamRole.MEMBER.value), owner
    )
    assert membership is not None
    store.update_team_member(
        membership.membership_id, TeamMemberUpdate(role=TeamRole.ADMIN.value), owner
    )
    store.remove_team_member(membership.membership_id, owner)

    # The audit trail is append-only but its read order is newest-first, and all
    # of these land in the same clock tick, so assert the set rather than a
    # sequence that would flake on the id tie-break.
    actions = {entry.action for entry in store.list_audit_logs(limit=50)}
    assert actions == {
        "team.created",
        "team.updated",
        "team.member_added",
        "team.member_role_changed",
        "team.member_removed",
    }


def test_membership_writes_register_the_user_row(tmp_path):
    """``team_memberships.user_id`` is a real foreign key to ``users.id``.

    SQLite ignores foreign keys unless they are switched on, which ``conftest``
    does for the whole suite; this pins that adding a member auto-registers the
    user rather than relying on a pre-existing row (the T77 failure class).
    """
    store, project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()
    store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_fresh", role=TeamRole.MEMBER.value), owner
    )
    store.add_project_member(
        project.project_id,
        ProjectMemberAdd(user_id="user_fresh", role=ProjectRole.EDITOR.value),
        owner,
    )
    assert store.session.get(db_models.UserRecord, "user_fresh") is not None


def test_team_slug_is_explicit_when_given_and_derived_otherwise(tmp_path):
    store = _store(tmp_path, "slug.db")
    owner = _default_actor()

    explicit = store.create_team(TeamCreate(name="内容组", slug="Content Group"), owner)
    assert explicit.slug == "content-group"

    # An explicit slug is a caller decision, so a collision is a conflict the
    # route turns into 409 rather than something silently renamed.
    with pytest.raises(ValueError):
        store.create_team(TeamCreate(name="另一个组", slug="Content Group"), owner)

    # A derived slug, by contrast, is generated and must never collide.
    derived_a = store.create_team(TeamCreate(name="内容组"), owner)
    derived_b = store.create_team(TeamCreate(name="内容组"), owner)
    assert derived_a.slug != derived_b.slug
    assert derived_b.slug.startswith(derived_a.slug)


def test_audit_log_filters_narrow_by_actor_action_and_entity(tmp_path):
    store, project, _run, team_id = _seeded(tmp_path)
    owner = _default_actor()
    store.add_team_member(
        team_id, TeamMemberAdd(user_id="user_audited", role=TeamRole.VIEWER.value), owner
    )
    store.add_project_member(
        project.project_id,
        ProjectMemberAdd(user_id="user_audited", role=ProjectRole.VIEWER.value),
        owner,
    )
    # Produce a refusal under a different actor so the filter has something to
    # exclude as well as something to keep.
    with pytest.raises(PermissionDenied):
        store.require_permission(_header_actor("user_audited"), Permission.TEAM_MANAGE)

    by_actor = store.list_audit_logs(actor_id=DEFAULT_ACTOR, limit=50)
    assert by_actor
    assert {entry.actor_id for entry in by_actor} == {DEFAULT_ACTOR}

    denials = store.list_audit_logs(
        actor_id="user_audited", action="permission.denied", limit=50
    )
    assert len(denials) == 1
    assert denials[0].entity_id == Permission.TEAM_MANAGE.value

    project_rows = store.list_audit_logs(project_id=project.project_id, limit=50)
    assert project_rows
    assert {entry.project_id for entry in project_rows} == {project.project_id}

    assert store.list_audit_logs(entity_type="nope", limit=50) == []
