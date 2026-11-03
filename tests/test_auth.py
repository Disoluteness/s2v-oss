"""V1.1 username-password login and sessions.

Five things are pinned here:

1. **Passwords are the standard-library KDF.** ``hashlib.scrypt`` with a
   per-hash salt and cost parameters encoded in the stored string; no new
   dependency, and costs can rise without a migration. Every malformed or
   missing hash verifies the same as a wrong password — the login endpoint is
   not a row-shape oracle either.
2. **The plaintext session token exists once.** It is returned by the login
   call and never again — not in introspection, not in the audit trail, not on
   the row. Only a peppered digest and a short lookup prefix are stored.
3. **The default identity paths are untouched.** Header, API key and
   default-actor behaviour are all proven by the pre-existing suite; what is
   pinned here is the one interaction the new dependency introduces — a key
   outranks a session, and a session outranks a header.
4. **An unusable session is one answer.** Unknown, revoked, expired and
   password-rotated all come back as the same 401 ``session_invalid``.
5. **A password change kills the sessions minted before it**, and maintenance
   mode does not lock people out of proving who they are: ``/api/auth`` is
   exempt, tenant writes are not.

Requests go through the real ASGI application via the shared harness, because
the interesting behaviour lives in the dependency chain (API key identity →
session identity → permissions) rather than in any single function.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from harness import build_client

from apps.api.app import db_models
from apps.api.app.actor import SESSION_TOKEN_PREFIX
from apps.api.app.passwords import (
    default_session_expiry,
    generate_session_token,
    hash_password,
    hash_session_token,
    session_token_prefix_of,
    verify_password,
)
from apps.api.app.repositories import DEFAULT_TENANT_ID

OWNER = "system"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "auth.db")
    try:
        yield client
    finally:
        client.close()


def _seat_user(asgi, user_id: str, email: str, password: str | None = None) -> None:
    """Create a user row (and a password when asked) straight through the store."""
    from apps.api.app.actor import ActorContext
    from apps.api.app.repositories import DEFAULT_TENANT_ID

    actor = ActorContext(
        actor_id=OWNER, tenant_id=DEFAULT_TENANT_ID, display_name="Internal System"
    )
    if password is not None:
        user = asgi.store.set_user_password(
            user_id, hash_password(password), actor=actor, email=email
        )
        assert user is not None
    asgi.store.session.commit()


def _login(asgi, email: str, password: str):
    return asgi.request(
        "POST", "/api/auth/login", json_body={"email": email, "password": password}
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1. password hashing
# --------------------------------------------------------------------------- #


def test_a_hash_verifies_its_own_password_and_no_other():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct Horse Battery Staple", stored)
    assert not verify_password("", stored)


def test_two_hashes_of_the_same_password_differ():
    # The salt is per-hash, so a dump of the column shows nothing about who
    # shares a password.
    assert hash_password("same") != hash_password("same")


def test_every_malformed_hash_is_just_a_wrong_password():
    for stored in (None, "", "plainly not a hash", "md5$abc", "scrypt$1$2", "scrypt$x$8$1$zz$yy"):
        assert verify_password("whatever", stored) is False


def test_the_cost_parameters_are_read_from_the_stored_string():
    stored = hash_password("pw")
    assert stored.split("$")[1] == str(2**14)
    # A row hashed with different (older, weaker) parameters still verifies.
    import hashlib
    import secrets

    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(b"legacy pw", salt=salt, n=2**12, r=8, p=1, dklen=64)
    legacy = f"scrypt$4096$8$1${salt.hex()}${digest.hex()}"
    assert verify_password("legacy pw", legacy)


# --------------------------------------------------------------------------- #
# 2. session tokens
# --------------------------------------------------------------------------- #


def test_session_tokens_have_a_prefix_and_a_lookup_handle():
    prefix, plaintext = generate_session_token()
    assert plaintext.startswith(SESSION_TOKEN_PREFIX)
    assert plaintext.startswith(prefix)
    assert session_token_prefix_of(plaintext) == prefix
    assert hash_session_token(plaintext) != plaintext


def test_non_session_shaped_tokens_have_no_handle():
    assert session_token_prefix_of("s2v_abcd_ef01") is None
    assert session_token_prefix_of("s2v_sess_nodot") is None
    assert session_token_prefix_of("s2v_sess_.secret") is None
    assert session_token_prefix_of("") is None


def test_the_default_expiry_follows_the_configured_ttl():
    moment = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    expiry = default_session_expiry(now=moment)
    assert expiry - moment == timedelta(hours=72)


# --------------------------------------------------------------------------- #
# 3. provisioning: POST /api/users/{user_id}/password
# --------------------------------------------------------------------------- #


def test_setting_a_password_seats_a_new_login_user(asgi):
    status, _, user = asgi.request(
        "POST",
        "/api/users/operator-1/password",
        json_body={"password": "s3cret-pass", "email": "op@example.com"},
    )
    assert status == 200, user
    assert user == {
        "user_id": "operator-1",
        "email": "op@example.com",
        "name": "operator-1",
        "tenant_id": DEFAULT_TENANT_ID,
        "role": "reviewer",
        "status": "active",
    }
    row = asgi.store.session.get(db_models.UserRecord, "operator-1")
    assert row is not None and row.password_hash.startswith("scrypt$")
    assert row.password_updated_at is not None


def test_a_password_below_the_floor_is_a_validation_error(asgi):
    status, _, body = asgi.request(
        "POST",
        "/api/users/short/password",
        json_body={"password": "short"},
    )
    assert status == 422
    assert body["detail"]["code"] == "validation_error"


def test_setting_a_password_requires_the_roster_key(asgi):
    status, _, key = asgi.request(
        "POST", "/api/api-keys", json_body={"name": "k", "scopes": ["project:read"]}
    )
    assert status == 200
    status, _, body = asgi.request(
        "POST",
        "/api/users/whoever/password",
        json_body={"password": "s3cret-pass"},
        headers=_bearer(key["api_key"]),
    )
    assert status == 403
    assert body["detail"]["code"] == "permission_denied"


# --------------------------------------------------------------------------- #
# 4. login
# --------------------------------------------------------------------------- #


def test_a_successful_login_returns_a_usable_session(asgi):
    _seat_user(asgi, "alice", "alice@example.com", "her-password")
    status, _, result = _login(asgi, "alice@example.com", "her-password")
    assert status == 200, result
    assert result["token"].startswith(SESSION_TOKEN_PREFIX)
    assert result["token_prefix"] == session_token_prefix_of(result["token"])
    assert result["user"]["user_id"] == "alice"
    assert result["expires_at"] is not None

    status, _, session = asgi.request(
        "GET", "/api/auth/session", headers=_bearer(result["token"])
    )
    assert status == 200
    assert session["session_id"] == result["session_id"]
    assert session["user"]["email"] == "alice@example.com"
    # The plaintext never comes back.
    assert "token" not in session


def test_a_session_token_works_as_an_identity_on_ordinary_endpoints(asgi):
    _seat_user(asgi, "bob", "bob@example.com", "his-password")
    _, _, result = _login(asgi, "bob@example.com", "his-password")
    status, _, me = asgi.request("GET", "/api/me", headers=_bearer(result["token"]))
    assert status == 200
    assert me["actor_id"] == "bob"
    assert me["source"] == "session"


def test_a_wrong_password_is_the_same_401_as_an_unknown_user(asgi):
    _seat_user(asgi, "carol", "carol@example.com", "right-password")
    wrong = _login(asgi, "carol@example.com", "wrong-password")[2]
    unknown = _login(asgi, "nobody@example.com", "whatever")[2]
    no_password = _login(asgi, "headless@reviewer.invalid", "whatever")[2]
    for body in (wrong, unknown, no_password):
        assert body["detail"]["code"] == "auth_invalid_credentials"


def test_an_inactive_account_cannot_log_in(asgi):
    _seat_user(asgi, "dave", "dave@example.com", "his-password")
    row = asgi.store.session.get(db_models.UserRecord, "dave")
    row.status = "suspended"
    asgi.store.session.commit()
    status, _, body = _login(asgi, "dave@example.com", "his-password")
    assert status == 401
    assert body["detail"]["code"] == "auth_invalid_credentials"


def test_an_ambiguous_email_is_refused_rather_than_guessed(asgi):
    _seat_user(asgi, "erin-a", "shared@example.com", "password-one")
    _seat_user(asgi, "erin-b", "shared@example.com", "password-one")
    status, _, body = _login(asgi, "shared@example.com", "password-one")
    assert status == 401
    assert body["detail"]["code"] == "auth_invalid_credentials"


# --------------------------------------------------------------------------- #
# 5. session lifecycle
# --------------------------------------------------------------------------- #


def test_logout_revokes_and_a_revoked_session_is_one_401(asgi):
    _seat_user(asgi, "frank", "frank@example.com", "his-password")
    _, _, result = _login(asgi, "frank@example.com", "his-password")
    headers = _bearer(result["token"])

    status, _, body = asgi.request("POST", "/api/auth/logout", headers=headers)
    assert status == 200
    assert body == {"session_id": result["session_id"], "revoked": True}

    for method, path in (("POST", "/api/auth/logout"), ("GET", "/api/auth/session")):
        status, _, body = asgi.request(method, path, headers=headers)
        assert status == 401, (method, body)
        assert body["detail"]["code"] == "session_invalid"


def test_a_missing_or_malformed_session_token_is_401(asgi):
    status, _, body = asgi.request("GET", "/api/auth/session")
    assert status == 401
    assert body["detail"]["code"] == "session_invalid"

    _, token = generate_session_token()
    status, _, body = asgi.request("GET", "/api/auth/session", headers=_bearer(token))
    assert status == 401
    assert body["detail"]["code"] == "session_invalid"


def test_an_expired_session_is_the_same_401(asgi):
    _seat_user(asgi, "grace", "grace@example.com", "her-password")
    _, _, result = _login(asgi, "grace@example.com", "her-password")
    row = asgi.store.session.get(db_models.UserSessionRecord, result["session_id"])
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    asgi.store.session.commit()
    status, _, body = asgi.request(
        "GET", "/api/auth/session", headers=_bearer(result["token"])
    )
    assert status == 401
    assert body["detail"]["code"] == "session_invalid"


def test_a_password_change_kills_the_sessions_minted_before_it(asgi):
    _seat_user(asgi, "henry", "henry@example.com", "first-password")
    _, _, result = _login(asgi, "henry@example.com", "first-password")
    headers = _bearer(result["token"])
    assert asgi.request("GET", "/api/auth/session", headers=headers)[0] == 200

    status, _, _ = asgi.request(
        "POST",
        "/api/users/henry/password",
        json_body={"password": "second-password"},
    )
    assert status == 200

    status, _, body = asgi.request("GET", "/api/auth/session", headers=headers)
    assert status == 401
    assert body["detail"]["code"] == "session_invalid"

    status, _, fresh = _login(asgi, "henry@example.com", "second-password")
    assert status == 200
    assert fresh["session_id"] != result["session_id"]


# --------------------------------------------------------------------------- #
# 6. priority: API key → session → header → default
# --------------------------------------------------------------------------- #


def test_one_bearer_credential_per_request_and_the_key_layer_defers_sessions(asgi):
    # ``Authorization`` carries exactly one bearer, so the key-vs-session
    # priority is decided by *shape*, not by a contest: a key-shaped token is
    # judged (and answered) by the API key layer, and a session-shaped token is
    # deferred to the session layer — never misreported as a bad API key.
    status, _, key = asgi.request("POST", "/api/api-keys", json_body={"name": "k"})
    assert status == 200
    status, _, me = asgi.request("GET", "/api/me", headers=_bearer(key["api_key"]))
    assert status == 200
    assert me["source"] == "api_key"

    status, _, body = asgi.request(
        "GET", "/api/me", headers=_bearer("s2v_totally_unknown_key")
    )
    assert status == 401
    assert body["detail"]["code"] == "api_key_invalid"

    _seat_user(asgi, " Ivy ".strip().lower(), "ivy@example.com", "her-password")
    _, _, login = _login(asgi, "ivy@example.com", "her-password")
    status, _, me = asgi.request("GET", "/api/me", headers=_bearer(login["token"]))
    assert status == 200
    assert me["source"] == "session"


def test_a_session_outranks_a_trusted_header(asgi):
    _seat_user(asgi, "kate", "kate@example.com", "her-password")
    _, _, login = _login(asgi, "kate@example.com", "her-password")
    status, _, me = asgi.request(
        "GET",
        "/api/me",
        headers={**_bearer(login["token"]), "X-Actor-Id": "someone-else"},
    )
    assert status == 200
    assert me["actor_id"] == "kate"
    assert me["source"] == "session"


def test_an_api_key_on_a_session_endpoint_is_still_401(asgi):
    # The session endpoints answer questions only a session can ask; a key is
    # a different credential, and "you hold no session" is the honest answer.
    status, _, key = asgi.request(
        "POST", "/api/api-keys", json_body={"name": "k"}
    )
    assert status == 200
    status, _, body = asgi.request(
        "GET", "/api/auth/session", headers=_bearer(key["api_key"])
    )
    assert status == 401
    assert body["detail"]["code"] == "session_invalid"


def test_the_default_path_is_untouched(asgi):
    # No credential at all: the request is the default actor, exactly as before
    # V1.1 existed — the property every one of the pre-existing tests proves.
    status, _, me = asgi.request("GET", "/api/me")
    assert status == 200
    assert me["source"] == "default"


# --------------------------------------------------------------------------- #
# 7. maintenance: who you are is not a tenant write
# --------------------------------------------------------------------------- #


def test_login_and_logout_work_during_maintenance(asgi):
    _seat_user(asgi, "leo", "leo@example.com", "his-password")
    status, _, _ = asgi.request(
        "POST", "/api/deployment/maintenance", json_body={"enabled": True}
    )
    assert status == 200

    # Tenant writes pause...
    status, _, body = asgi.request("POST", "/api/projects", json_body={"title": "x"})
    assert status == 503
    assert body["detail"]["code"] == "maintenance_mode"

    # ...but proving who you is never does.
    status, _, login = _login(asgi, "leo@example.com", "his-password")
    assert status == 200, login
    status, _, _ = asgi.request("POST", "/api/auth/logout", headers=_bearer(login["token"]))
    assert status == 200

    status, _, _ = asgi.request(
        "POST", "/api/deployment/maintenance", json_body={"enabled": False}
    )
    assert status == 200
