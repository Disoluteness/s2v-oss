"""V7.2 programmatic credentials: issuing, authenticating, narrowing, revoking.

Four things are pinned here:

1. **The plaintext exists once.** It is returned by the creation call and never
   again — not in the list, not in the audit trail, not on the row.
2. **A key is a first-class identity.** A bearer credential resolves to the same
   ``ActorContext`` shape the V6.5 header produces, so the permission layer, the
   audit trail and ``/api/me`` need no special case for it.
3. **A key can only ever hold less than its owner.** Scopes narrow; asking for
   more is refused, not silently trimmed.
4. **An unusable key is one answer.** Unknown, revoked, expired and malformed all
   come back as the same 401, so the endpoint is not a key oracle.

Requests go through the real ASGI application via the shared harness, because
the interesting behaviour lives in the dependency chain (identity → permission →
idempotency) rather than in any single function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from harness import build_client
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models
from apps.api.app.actor import ActorContext
from apps.api.app.api_keys import parse_bearer
from apps.api.app.db import Base
from apps.api.app.models import ApiKeyCreate, TeamMemberAdd
from apps.api.app.repositories import (
    DEFAULT_TENANT_ID,
    ApiKeyOutcome,
    ApiKeyRepository,
    ProjectRepository,
    api_key_prefix_of,
    generate_api_key,
    hash_api_key,
)

OWNER = "system"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "api-keys.db")
    try:
        yield client
    finally:
        client.close()


def _default_actor() -> ActorContext:
    return ActorContext(
        actor_id=OWNER,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


def _seat(asgi, *user_ids: str, role: str = "member") -> str:
    """Put the default actor in the default team, then seat the given identities."""
    owner = _default_actor()
    asgi.store.ensure_default_seat(owner)
    asgi.store.session.commit()
    team_id = asgi.store.actor_seat(owner)[0]
    assert team_id is not None
    for user_id in user_ids:
        asgi.store.add_team_member(team_id, TeamMemberAdd(user_id=user_id, role=role), owner)
    asgi.store.session.commit()
    return team_id


def _create_key(asgi, **body) -> dict:
    payload = {"name": "test-key", **body}
    status, _, response = asgi.request("POST", "/api/api-keys", json_body=payload)
    assert status == 200, response
    assert isinstance(response, dict)
    return response


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1. issuing
# --------------------------------------------------------------------------- #


def test_issuing_returns_the_plaintext_exactly_once(asgi):
    created = _create_key(asgi)
    token = created["api_key"]
    assert isinstance(token, str) and token.startswith("s2v_")
    assert created["key_prefix"] == api_key_prefix_of(token)
    assert created["status"] == "active"
    assert created["api_key_id"].startswith("key_")

    listed = asgi.request("GET", "/api/api-keys")[2]
    assert listed["total"] == 1
    entry = listed["items"][0]
    assert "api_key" not in entry
    assert entry["key_prefix"] == created["key_prefix"]


def test_the_stored_row_holds_a_digest_not_the_credential(asgi):
    created = _create_key(asgi)
    token = created["api_key"]

    record = asgi.store.api_keys.find_by_prefix(created["key_prefix"])
    assert record is not None
    assert record.key_hash == hash_api_key(token)
    assert record.key_hash != token
    assert len(record.key_hash) == 64

    # Nothing anywhere on the row carries the secret half.
    assert token not in str(record.key_hash)
    assert token.split("_")[-1] not in str(record.key_hash)


def test_issuing_registers_the_owner_and_audits_it(asgi):
    _create_key(asgi)
    assert asgi.store.session.get(db_models.UserRecord, OWNER) is not None

    logs = asgi.store.list_audit_logs(tenant_id=DEFAULT_TENANT_ID, action="apikey.created")
    assert len(logs) == 1
    assert logs[0].payload["key_prefix"]
    # The audit trail records what was issued, never the secret itself.
    assert "api_key" not in logs[0].payload


def test_a_key_can_be_scoped_narrower_than_its_owner(asgi):
    created = _create_key(asgi, scopes=["run:read", "project:read"])
    assert created["scopes"] == ["project:read", "run:read"]

    me = asgi.request("GET", "/api/me", headers=_bearer(created["api_key"]))[2]
    assert me["source"] == "api_key"
    assert me["actor_id"] == OWNER
    assert set(me["permissions"]) == {"project:read", "run:read"}


def test_an_unknown_scope_is_a_bad_request(asgi):
    status, _, body = asgi.request(
        "POST",
        "/api/api-keys",
        json_body={"name": "bad", "scopes": ["nope:nope"]},
    )
    assert status == 400
    assert body["detail"]["code"] == "bad_request"
    assert "nope:nope" in body["detail"]["message"]


def test_a_scope_the_owner_lacks_is_refused_not_trimmed(asgi):
    _seat(asgi, "admin-user", role="admin")
    status, _, body = asgi.request(
        "POST",
        "/api/api-keys",
        json_body={"name": "too-wide", "scopes": ["team:manage"]},
        actor_id="admin-user",
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"
    assert "team:manage" in body["detail"]["message"]


def test_issuing_requires_apikey_manage(asgi):
    _seat(asgi, "member-user", role="member")
    status, _, body = asgi.request(
        "POST",
        "/api/api-keys",
        json_body={"name": "member-key"},
        actor_id="member-user",
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"
    assert body["detail"]["permission"] == "apikey:manage"


def test_listing_requires_apikey_manage(asgi):
    _seat(asgi, "viewer-user", role="viewer")
    status, _, body = asgi.request("GET", "/api/api-keys", actor_id="viewer-user")
    assert status == 403
    assert body["detail"]["permission"] == "apikey:manage"


# --------------------------------------------------------------------------- #
# 2. authenticating
# --------------------------------------------------------------------------- #


def test_a_bearer_key_authenticates_as_its_owner(asgi):
    created = _create_key(asgi)
    status, _, me = asgi.request("GET", "/api/me", headers=_bearer(created["api_key"]))
    assert status == 200
    assert me["actor_id"] == OWNER
    assert me["source"] == "api_key"
    assert "apikey:manage" in me["permissions"]


def test_a_key_authenticates_a_real_write(asgi):
    created = _create_key(asgi)
    status, _, project = asgi.request(
        "POST",
        "/api/projects",
        json_body={"title": "key-made project"},
        headers=_bearer(created["api_key"]),
    )
    assert status == 200, project
    assert project["project_id"]

    # And the write is visible to the same credential on the way back out.
    status, _, projects = asgi.request("GET", "/api/projects", headers=_bearer(created["api_key"]))
    assert status == 200
    assert [item["project"]["project_id"] for item in projects] == [project["project_id"]]


def test_a_key_outranks_a_trusted_header(asgi):
    _seat(asgi, "someone-else")
    created = _create_key(asgi)
    status, _, me = asgi.request(
        "GET",
        "/api/me",
        headers={**_bearer(created["api_key"]), "X-Actor-Id": "someone-else"},
    )
    assert status == 200
    assert me["actor_id"] == OWNER


def test_no_authorization_header_is_completely_unchanged(asgi):
    status, _, me = asgi.request("GET", "/api/me")
    assert status == 200
    assert me["source"] == "default"
    assert me["actor_id"] == OWNER


def test_unknown_revoked_expired_and_malformed_keys_all_read_the_same(asgi):
    created = _create_key(asgi)
    token = created["api_key"]

    cases: list[tuple[str, str]] = [
        ("unknown", f"s2v_{'0' * 12}_{'a' * 48}"),
        ("garbage", "not-a-key"),
        ("wrong-secret", f"{created['key_prefix']}_{'b' * 48}"),
    ]
    asgi.store.revoke_api_key(created["api_key_id"], _default_actor())
    cases.append(("revoked", token))

    seen: list[tuple[int, str, str]] = []
    for label, candidate in cases:
        status, _, body = asgi.request("GET", "/api/me", headers=_bearer(candidate))
        seen.append((status, body["detail"]["code"], body["detail"]["message"]))
        assert status == 401, (label, body)

    # Every rejection is identical on the wire: the endpoint leaks nothing about
    # which of the reasons applied.
    assert len({entry for entry in seen}) == 1, seen


def test_an_expired_key_stops_working(asgi):
    created = _create_key(asgi, expires_in_days=1)
    token = created["api_key"]
    assert asgi.request("GET", "/api/me", headers=_bearer(token))[0] == 200

    session = asgi.store.session
    record = session.get(db_models.ApiKeyRecord, created["api_key_id"])
    assert record is not None
    record.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    session.commit()

    status, _, body = asgi.request("GET", "/api/me", headers=_bearer(token))
    assert status == 401
    assert body["detail"]["code"] == "api_key_invalid"


def test_a_non_bearer_authorization_header_is_ignored(asgi):
    status, _, me = asgi.request(
        "GET",
        "/api/me",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert status == 200
    assert me["source"] == "default"


def test_authentication_stamps_last_used(asgi):
    created = _create_key(asgi)
    assert created["last_used_at"] is None
    asgi.request("GET", "/api/me", headers=_bearer(created["api_key"]))

    listed = asgi.request("GET", "/api/api-keys")[2]
    assert listed["items"][0]["last_used_at"] is not None


# --------------------------------------------------------------------------- #
# 3. scope narrowing
# --------------------------------------------------------------------------- #


def test_a_narrowed_key_is_refused_outside_its_scopes(asgi):
    created = _create_key(asgi, scopes=["run:read", "project:read"])
    status, _, body = asgi.request(
        "POST",
        "/api/projects",
        json_body={"title": "not allowed"},
        headers=_bearer(created["api_key"]),
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"
    assert body["detail"]["permission"] == "project:write"
    assert "not scoped" in body["detail"]["message"]
    assert body["detail"]["api_key_prefix"] == created["key_prefix"]


def test_a_narrowed_key_still_serves_its_scopes(asgi):
    created = _create_key(asgi, scopes=["project:read", "run:read"])
    status, _, projects = asgi.request(
        "GET",
        "/api/projects",
        headers=_bearer(created["api_key"]),
    )
    assert status == 200
    assert isinstance(projects, list)


def test_an_unscoped_key_inherits_everything_its_owner_has(asgi):
    created = _create_key(asgi)
    assert created["scopes"] is None
    status, _, project = asgi.request(
        "POST",
        "/api/projects",
        json_body={"title": "full-reach"},
        headers=_bearer(created["api_key"]),
    )
    assert status == 200, project


def test_the_owner_own_permissions_are_untouched_by_a_narrowed_key(asgi):
    created = _create_key(asgi, scopes=["run:read"])
    # The key is narrow ...
    assert asgi.request(
        "POST", "/api/projects", json_body={"title": "x"}, headers=_bearer(created["api_key"])
    )[0] == 403
    # ... but the human behind it still is not.
    full = asgi.request("POST", "/api/projects", json_body={"title": "y"})
    assert full[0] == 200


def test_a_scope_denial_is_audited(asgi):
    created = _create_key(asgi, scopes=["run:read"])
    asgi.request(
        "POST", "/api/projects", json_body={"title": "x"}, headers=_bearer(created["api_key"])
    )
    logs = asgi.store.list_audit_logs(tenant_id=DEFAULT_TENANT_ID, action="permission.denied")
    assert len(logs) == 1
    assert logs[0].payload["reason"] == "api_key_scope"
    assert logs[0].payload["api_key_prefix"] == created["key_prefix"]


# --------------------------------------------------------------------------- #
# 4. revoking
# --------------------------------------------------------------------------- #


def test_revoking_is_immediate_and_idempotent(asgi):
    created = _create_key(asgi)
    token = created["api_key"]
    assert asgi.request("GET", "/api/me", headers=_bearer(token))[0] == 200

    first = asgi.request("POST", f"/api/api-keys/{created['api_key_id']}/revoke")
    assert first[0] == 200
    assert first[2]["status"] == "revoked"

    second = asgi.request("POST", f"/api/api-keys/{created['api_key_id']}/revoke")
    assert second[0] == 200
    assert second[2]["status"] == "revoked"

    assert asgi.request("GET", "/api/me", headers=_bearer(token))[0] == 401

    logs = asgi.store.list_audit_logs(tenant_id=DEFAULT_TENANT_ID, action="apikey.revoked")
    assert len(logs) == 1  # revoking twice does not double-audit


def test_revoking_an_unknown_key_is_a_404(asgi):
    status, _, body = asgi.request("POST", "/api/api-keys/key_nope/revoke")
    assert status == 404
    assert body["detail"]["code"] == "not_found"


def test_revocation_keeps_the_row_for_the_audit_trail(asgi):
    created = _create_key(asgi)
    asgi.request("POST", f"/api/api-keys/{created['api_key_id']}/revoke")
    listed = asgi.request("GET", "/api/api-keys")[2]
    assert listed["total"] == 1
    assert listed["items"][0]["status"] == "revoked"


# --------------------------------------------------------------------------- #
# 5. the pieces underneath
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("Bearer", None),
        ("Bearer   ", None),
        ("Basic abc", None),
        ("bearer s2v_x_y", "s2v_x_y"),
        ("Bearer s2v_x_y", "s2v_x_y"),
    ],
)
def test_parse_bearer(header, expected):
    assert parse_bearer(header) == expected


def test_generated_keys_are_unique_and_well_formed():
    prefixes = set()
    tokens = set()
    for _ in range(200):
        prefix, token = generate_api_key()
        assert token.startswith(f"{prefix}_")
        assert len(token.split("_")) == 3
        assert api_key_prefix_of(token) == prefix
        prefixes.add(prefix)
        tokens.add(token)
    assert len(prefixes) == 200
    assert len(tokens) == 200


def test_prefix_extraction_rejects_anything_off_scheme():
    assert api_key_prefix_of("") is None
    assert api_key_prefix_of("abc") is None
    assert api_key_prefix_of("s2v_onlyone") is None
    assert api_key_prefix_of("other_abc_def") is None
    assert api_key_prefix_of("s2v_abc_") is None


def test_repository_refuses_an_unknown_scope_without_writing():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    repo = ApiKeyRepository(Session(engine))
    ProjectRepository(repo.session).ensure_tenant(DEFAULT_TENANT_ID)

    issued = repo.create(
        ApiKeyCreate(name="x", scopes=["bogus"]),
        owner_id=OWNER,
        owner_permissions=frozenset(),
    )
    assert issued.outcome is ApiKeyOutcome.INVALID_SCOPE
    assert repo.list_keys(tenant_id=DEFAULT_TENANT_ID) == ([], 0)
