"""V7.4 embeddable workspace: integrations, signed sessions, framing and pinning.

An embed session lets an external platform open the Studio inside its own UI for
one run, for an end user who has no account here. Five things are pinned:

1. **The token is ours to sign, the partner's to hold.** The token is a stateless
   ``s2v_embed_<payload>.<hmac>`` string, minted by the platform. A partner never
   holds a signing key, so it cannot fabricate a session for a run nobody granted
   it. The payload is signed but not encrypted — it carries no secret, only ids
   the caller already has.
2. **The ceiling is enforced twice.** ``run:review`` is the single write scope a
   session may carry; anything wider is refused at mint time with a 400 *and*
   re-capped at verification time, so narrowing the allowlist in a later release
   takes effect on links that are already circulating.
3. **Context is pinned before scope is judged.** A tenant-wide endpoint, another
   project, or a sibling run is refused as ``embed_context`` however wide the
   token's scopes are. Reporting ``embed_scope`` there would suggest that a wider
   scope would have helped; it would not.
4. **``disabled`` is the revocation lever.** Embed sessions are stateless, so the
   integration row is re-read on every request. Disabling stops every link that
   integration ever minted, immediately.
5. **The document request needs no credential, and leaks none.** The integration
   id travels in the path (so ``frame-ancestors`` is computable on the first GET)
   and the token travels in the fragment (so it is never sent to the server, never
   logged, and never forwarded in ``Referer``).

The page shell itself is asserted here too, because the framing header is a
security property of the response and not of the HTML.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from harness import build_client

from apps.api.app import embed as embed_module
from apps.api.app.actor import EMBED_SESSION_SOURCE, ActorContext
from apps.api.app.embed import (
    EMBED_ALLOWED_SCOPES,
    EMBED_DEFAULT_SCOPES,
    EMBED_TOKEN_HEADER,
    EMBED_TOKEN_PREFIX,
    MAX_TTL_SECONDS,
    EmbedSession,
    csp_frame_ancestors,
    decode_embed_session,
    effective_embed_scopes,
    embed_page_path,
    embed_url,
    is_embed_token,
    issue_embed_session,
    normalize_scopes,
    unknown_scopes,
    validate_origins,
)
from apps.api.app.models import TeamMemberAdd
from apps.api.app.permissions import TENANT_ONLY_PERMISSIONS, Permission
from apps.api.app.repositories import DEFAULT_TENANT_ID

OWNER = "system"
PIPELINE_KEY = "avatar_spokesperson"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "embed.db")
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
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


def _integration(asgi, **body) -> dict:
    payload = {
        "name": "Acme Studio",
        "allowed_origins": ["https://app.acme.test/"],
        **body,
    }
    status, _, response = asgi.request("POST", "/api/external-integrations", json_body=payload)
    assert status == 200, response
    return response


def _project(asgi, title: str = "P") -> dict:
    status, _, project = asgi.request("POST", "/api/projects", json_body={"title": title})
    assert status == 200, project
    return project


def _run(asgi, project_id: str, script: str = "十五秒口播") -> dict:
    status, _, run = asgi.request(
        "POST",
        f"/api/projects/{project_id}/runs",
        json_body={"raw_script": script, "pipeline_key": PIPELINE_KEY},
    )
    assert status == 200, run
    return run


def _started_run(asgi, project_id: str, script: str = "十五秒口播") -> dict:
    """A run that has been started, so it sits at its first review gate."""
    run = _run(asgi, project_id, script)
    status, _, started = asgi.request("POST", f"/api/runs/{run['run_id']}/start", json_body={})
    assert status == 200, started
    assert started["status"] == "waiting_review", started["status"]
    return started


def _mint(asgi, integration_id: str, **body) -> dict:
    payload = {"actor_id": "ext_user_1", **body}
    status, _, response = asgi.request(
        "POST",
        f"/api/external-integrations/{integration_id}/embed-sessions",
        json_body=payload,
    )
    assert status == 200, response
    return response


def _holder(token: str) -> dict:
    return {EMBED_TOKEN_HEADER: token}


def _error_code(body) -> str:
    assert isinstance(body, dict), body
    return body["detail"]["code"]


# --------------------------------------------------------------------------- #
# 1. the token: shape, signature, and what a failure looks like
# --------------------------------------------------------------------------- #


def test_a_minted_token_round_trips_the_facts_it_carries():
    token, issued = issue_embed_session(
        integration_id="extint_1",
        tenant_id="tenant_1",
        actor_id="ext_user_1",
        project_id="project_1",
        run_id="run_1",
        display_name="Ada",
        scopes=frozenset({"run:read", "run:review"}),
    )

    assert token.startswith(EMBED_TOKEN_PREFIX)
    assert token.count(".") == 1, "payload and signature are the only two parts"

    decoded = decode_embed_session(token)
    assert decoded is not None
    assert decoded.integration_id == "extint_1"
    assert decoded.tenant_id == "tenant_1"
    assert decoded.actor_id == "ext_user_1"
    assert decoded.project_id == "project_1"
    assert decoded.run_id == "run_1"
    assert decoded.display_name == "Ada"
    assert decoded.scopes == frozenset({"run:read", "run:review"})
    assert decoded.token_id == issued.token_id
    assert decoded.remaining_seconds > 0


def test_the_payload_uses_short_keys_and_a_sorted_scope_list():
    """The payload is public; keeping it small and canonical is the point."""
    token, _ = issue_embed_session(
        integration_id="extint_1",
        tenant_id="tenant_1",
        actor_id="ext_user_1",
        project_id="project_1",
        scopes=frozenset({"run:read", "project:read"}),
    )
    encoded = token[len(EMBED_TOKEN_PREFIX) :].split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    assert set(payload) == {"v", "iid", "tid", "sub", "pid", "pg", "scp", "exp", "jti"}
    assert payload["scp"] == sorted(payload["scp"])
    # A run is optional, so its key is absent rather than null.
    assert "rid" not in payload


def test_every_unusable_token_decodes_to_one_none():
    """Malformed, tampered-with and expired are one answer — no oracle."""
    token, _ = issue_embed_session(
        integration_id="extint_1",
        tenant_id="tenant_1",
        actor_id="ext_user_1",
        project_id="project_1",
    )
    encoded, _, signature = token[len(EMBED_TOKEN_PREFIX) :].partition(".")

    assert decode_embed_session(None) is None
    assert decode_embed_session("") is None
    assert decode_embed_session("not-a-token") is None
    # A first-party API key must never be mistaken for an embed session.
    assert decode_embed_session("s2v_0123456789ab_" + "0" * 48) is None
    assert decode_embed_session(f"{EMBED_TOKEN_PREFIX}{encoded}") is None, "missing signature"
    assert decode_embed_session(f"{EMBED_TOKEN_PREFIX}.{signature}") is None, "missing payload"
    assert decode_embed_session(f"{EMBED_TOKEN_PREFIX}{encoded}.deadbeef") is None, "bad signature"

    # A single flipped payload byte invalidates the signature, not just the JSON.
    flipped = encoded[:-2] + ("AA" if not encoded.endswith("AA") else "BB")
    assert decode_embed_session(f"{EMBED_TOKEN_PREFIX}{flipped}.{signature}") is None

    # A payload that is not even JSON must not raise.
    assert decode_embed_session(f"{EMBED_TOKEN_PREFIX}bm90anNvbg.{signature}") is None


def test_an_expired_token_does_not_verify():
    token, _ = issue_embed_session(
        integration_id="extint_1",
        tenant_id="tenant_1",
        actor_id="ext_user_1",
        project_id="project_1",
        ttl_seconds=60,
    )
    assert decode_embed_session(token) is not None
    # No grace window: the expiry is the expiry.
    assert decode_embed_session(token, now=datetime.now(UTC) + timedelta(seconds=61)) is None


def test_is_embed_token_separates_the_two_credentials():
    assert is_embed_token(f"{EMBED_TOKEN_PREFIX}abc.def")
    assert not is_embed_token("s2v_0123456789ab_" + "0" * 48)
    assert not is_embed_token(None)


# --------------------------------------------------------------------------- #
# 2. the ceiling: allowed scopes, capped at issue time *and* at request time
# --------------------------------------------------------------------------- #


def test_the_default_session_is_read_only():
    assert EMBED_DEFAULT_SCOPES < EMBED_ALLOWED_SCOPES
    assert EMBED_ALLOWED_SCOPES - EMBED_DEFAULT_SCOPES == {"run:review"}
    # The two spend-capable scopes stay out of an embed on purpose: a link that
    # shows up in a third party's page must not be able to dispatch paid work.
    assert "run:write" not in EMBED_ALLOWED_SCOPES
    assert "run:start" not in EMBED_ALLOWED_SCOPES


def test_a_scope_above_the_ceiling_is_refused_at_issue_time():
    with pytest.raises(ValueError) as excinfo:
        issue_embed_session(
            integration_id="extint_1",
            tenant_id="tenant_1",
            actor_id="u",
            project_id="project_1",
            scopes=frozenset({"run:read", "run:write"}),
        )
    assert "run:write" in str(excinfo.value)


def test_omitting_scopes_means_the_default_set_not_everything():
    assert normalize_scopes(None) == frozenset(EMBED_DEFAULT_SCOPES)
    assert unknown_scopes(normalize_scopes(None)) == []


def test_scopes_are_recapped_against_todays_allowlist(monkeypatch):
    """Narrowing the allowlist must reach links that are already out there."""
    token, _ = issue_embed_session(
        integration_id="extint_1",
        tenant_id="tenant_1",
        actor_id="u",
        project_id="project_1",
        scopes=frozenset({"run:read", "run:review"}),
    )
    assert decode_embed_session(token).scopes == frozenset({"run:read", "run:review"})

    monkeypatch.setattr(
        embed_module, "EMBED_ALLOWED_SCOPES", frozenset({"run:read"})
    )
    recapped = decode_embed_session(token)
    assert recapped is not None, "the token stays valid, it just does less"
    assert recapped.scopes == frozenset({"run:read"})
    assert effective_embed_scopes(None) <= embed_module.EMBED_ALLOWED_SCOPES


def test_the_ttl_is_bounded_in_both_directions():
    from apps.api.app.embed import DEFAULT_TTL_SECONDS, clamp_ttl

    assert clamp_ttl(None) == DEFAULT_TTL_SECONDS
    assert clamp_ttl(1) == 1
    # A careless caller cannot mint a month-long capability.
    assert clamp_ttl(MAX_TTL_SECONDS * 30) == MAX_TTL_SECONDS
    with pytest.raises(ValueError):
        clamp_ttl(0)
    with pytest.raises(ValueError):
        clamp_ttl(-1)


def test_an_unsupported_page_is_refused():
    with pytest.raises(ValueError):
        issue_embed_session(
            integration_id="extint_1",
            tenant_id="tenant_1",
            actor_id="u",
            project_id="project_1",
            page="admin",
        )


# --------------------------------------------------------------------------- #
# 3. framing: the allowlist, and the header it becomes
# --------------------------------------------------------------------------- #


def test_origins_are_normalised_and_deduplicated():
    normalized, error = validate_origins(
        ["https://app.acme.test/", "https://app.acme.test", " http://localhost:3000 "]
    )
    assert error is None
    assert normalized == ["https://app.acme.test", "http://localhost:3000"]


@pytest.mark.parametrize(
    "origin",
    ["*", "https://app.acme.test/embed", "app.acme.test", "ftp://app.acme.test", "https://"],
)
def test_an_origin_that_would_not_match_is_refused_at_write_time(origin):
    """A bad entry fails *closed* (nothing may frame), which is hard to debug."""
    normalized, error = validate_origins([origin])
    assert error is not None
    assert normalized == []


def test_the_framing_header_is_none_when_nobody_is_allowed():
    assert csp_frame_ancestors([]) == "frame-ancestors 'none'"
    assert csp_frame_ancestors(None) == "frame-ancestors 'none'"
    assert csp_frame_ancestors(["https://app.acme.test/"]) == (
        "frame-ancestors https://app.acme.test"
    )


# --------------------------------------------------------------------------- #
# 4. the tenant's control surface
# --------------------------------------------------------------------------- #


def test_registering_an_integration_publishes_the_embed_contract(asgi):
    integration = _integration(asgi, name="Acme Studio")

    assert integration["integration_id"].startswith("extint_")
    assert integration["slug"] == "acme-studio", "a slug is derived when omitted"
    assert integration["allowed_origins"] == ["https://app.acme.test"]
    assert integration["status"] == "active"

    capabilities = integration["capabilities"]
    assert capabilities["pages"] == ["run-board", "workspace"]
    assert capabilities["allowed_scopes"] == sorted(EMBED_ALLOWED_SCOPES)
    assert "run:write" not in capabilities["allowed_scopes"]
    assert capabilities["max_ttl_seconds"] == MAX_TTL_SECONDS
    assert capabilities["embed_path"] == "/embed"


def test_a_wildcard_origin_is_refused_at_registration(asgi):
    status, _, body = asgi.request(
        "POST",
        "/api/external-integrations",
        json_body={"name": "Wild", "allowed_origins": ["*"]},
    )
    assert status == 400
    assert _error_code(body) == "bad_request"


def test_two_integrations_cannot_share_a_slug(asgi):
    _integration(asgi, name="Acme", slug="shared")
    status, _, body = asgi.request(
        "POST",
        "/api/external-integrations",
        json_body={"name": "Other", "slug": "shared"},
    )
    assert status == 409
    assert _error_code(body) == "conflict"


def test_the_listing_is_a_page_envelope(asgi):
    _integration(asgi, name="One", slug="one")
    _integration(asgi, name="Two", slug="two")

    status, _headers, page = asgi.request("GET", "/api/external-integrations")
    assert status == 200
    assert page["total"] == 2
    # Rows are ordered by ``created_at, id`` and the timestamps are second-precise,
    # so two registrations made in the same second tie and the tie-break is the
    # opaque id. The envelope is the contract here, not the order.
    assert {item["slug"] for item in page["items"]} == {"one", "two"}
    assert page["limit"] >= 2

    status, _, filtered = asgi.request("GET", "/api/external-integrations?status=disabled")
    assert status == 200
    assert filtered["items"] == []


def test_disabling_and_deleting_are_both_reachable(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]

    status, _, disabled = asgi.request(
        "PATCH", f"/api/external-integrations/{iid}", json_body={"status": "disabled"}
    )
    assert status == 200
    assert disabled["status"] == "disabled"
    # A partial update leaves what it did not mention alone.
    assert disabled["allowed_origins"] == ["https://app.acme.test"]

    status, _, removed = asgi.request("DELETE", f"/api/external-integrations/{iid}")
    assert status == 200
    assert removed["integration_id"] == iid

    status, _, body = asgi.request("DELETE", f"/api/external-integrations/{iid}")
    assert status == 404
    assert _error_code(body) == "not_found"


def test_an_unknown_integration_reads_as_absent(asgi):
    status, _, body = asgi.request("GET", "/api/external-integrations/extint_absent")
    assert status == 404
    assert _error_code(body) == "not_found"


def test_an_external_platform_is_a_tenant_wide_capability(asgi):
    """A member may work in projects; issuing embed links is not that."""
    assert Permission.INTEGRATION_MANAGE in TENANT_ONLY_PERMISSIONS
    assert Permission.INTEGRATION_MANAGE not in _project_grantable_permissions()

    seat_holder = _default_actor()
    team_id, _ = asgi.store.actor_seat(seat_holder)
    asgi.store.add_team_member(
        team_id, TeamMemberAdd(user_id="member_user", role="member"), seat_holder
    )
    asgi.store.session.commit()

    status, _, body = asgi.request(
        "GET", "/api/external-integrations", actor_id="member_user"
    )
    assert status == 403
    assert _error_code(body) == "permission_denied"

    # A caller with no seat at all gets nothing, including reads.
    status, _, _ = asgi.request("GET", "/api/external-integrations", actor_id="ghost")
    assert status == 403


def _project_grantable_permissions() -> set[Permission]:
    from apps.api.app.permissions import PROJECT_ROLE_PERMISSIONS

    grantable: set[Permission] = set()
    for permissions in PROJECT_ROLE_PERMISSIONS.values():
        grantable |= set(permissions)
    return grantable


# --------------------------------------------------------------------------- #
# 5. minting a link
# --------------------------------------------------------------------------- #


def test_minting_returns_a_link_whose_credential_is_in_the_fragment(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://studio.example/")

    integration = _integration(asgi)
    project = _project(asgi)
    run = _run(asgi, project["project_id"])

    issued = _mint(
        asgi,
        integration["integration_id"],
        project_id=project["project_id"],
        run_id=run["run_id"],
        actor_name="Ada",
        scopes=["run:read", "project:read", "run:review"],
    )

    token = issued["token"]
    assert token.startswith(EMBED_TOKEN_PREFIX)
    assert issued["embed_url"] == (
        f"https://studio.example{embed_page_path(integration['integration_id'], run_id=run['run_id'])}"
        f"#token={token}"
    )
    # The token is the only place the view is stated: a query parameter next to a
    # signed fact would be a second, unsigned source free to disagree with it.
    assert "page=" not in issued["embed_url"]
    assert issued["scopes"] == ["project:read", "run:read", "run:review"]
    assert issued["context"] == {
        "project_id": project["project_id"],
        "run_id": run["run_id"],
        "page": "workspace",
    }


def test_the_link_origin_comes_from_configuration_not_the_host_header(asgi, monkeypatch):
    """Behind a gateway the ``Host`` header is the internal hop, not the public one."""
    from apps.api.app.config import settings

    integration = _integration(asgi)
    project = _project(asgi)

    monkeypatch.setattr(settings, "public_base_url", "https://studio.example/")
    configured = _mint(asgi, integration["integration_id"], project_id=project["project_id"])
    assert configured["embed_url"].startswith("https://studio.example/embed/")

    monkeypatch.setattr(settings, "public_base_url", None)
    fallback = _mint(asgi, integration["integration_id"], project_id=project["project_id"])
    assert fallback["embed_url"].startswith("http://testserver/embed/")


def test_minting_a_bad_link_is_refused_where_the_caller_can_see_why(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    run = _run(asgi, project["project_id"])

    status, _, body = asgi.request(
        "POST",
        f"/api/external-integrations/{iid}/embed-sessions",
        json_body={"project_id": "project_absent", "actor_id": "u"},
    )
    assert status == 404
    assert _error_code(body) == "not_found"

    status, _, body = asgi.request(
        "POST",
        f"/api/external-integrations/{iid}/embed-sessions",
        json_body={"project_id": project["project_id"], "run_id": "run_absent", "actor_id": "u"},
    )
    assert status == 404

    # A run that belongs to another project answers exactly like a missing one:
    # confirming it exists would leak the shape of another project's runs.
    other = _project(asgi, "Other")
    other_run = _run(asgi, other["project_id"], "另一个")
    status, _, body = asgi.request(
        "POST",
        f"/api/external-integrations/{iid}/embed-sessions",
        json_body={
            "project_id": project["project_id"],
            "run_id": other_run["run_id"],
            "actor_id": "u",
        },
    )
    assert status == 404

    status, _, body = asgi.request(
        "POST",
        f"/api/external-integrations/{iid}/embed-sessions",
        json_body={
            "project_id": project["project_id"],
            "run_id": run["run_id"],
            "actor_id": "u",
            "scopes": ["run:write"],
        },
    )
    assert status == 400
    assert _error_code(body) == "bad_request"


def test_a_disabled_integration_cannot_mint_and_says_so(asgi):
    """409, not 404: the row exists and the caller may re-enable it."""
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    asgi.request("PATCH", f"/api/external-integrations/{iid}", json_body={"status": "disabled"})

    status, _, body = asgi.request(
        "POST",
        f"/api/external-integrations/{iid}/embed-sessions",
        json_body={"project_id": project["project_id"], "actor_id": "u"},
    )
    assert status == 409
    assert _error_code(body) == "conflict"


def test_minting_for_an_unknown_integration_is_a_404(asgi):
    project = _project(asgi)
    status, _, body = asgi.request(
        "POST",
        "/api/external-integrations/extint_absent/embed-sessions",
        json_body={"project_id": project["project_id"], "actor_id": "u"},
    )
    assert status == 404
    assert _error_code(body) == "not_found"


def test_the_ttl_request_cannot_exceed_the_ceiling(asgi):
    integration = _integration(asgi)
    project = _project(asgi)
    issued = _mint(
        asgi,
        integration["integration_id"],
        project_id=project["project_id"],
        ttl_seconds=MAX_TTL_SECONDS * 10,
    )
    expires_at = datetime.fromisoformat(issued["expires_at"])
    remaining = (expires_at - datetime.now(UTC)).total_seconds()
    assert 0 < remaining <= MAX_TTL_SECONDS + 5
    # And the request did not silently become "no expiry".
    assert remaining > MAX_TTL_SECONDS / 2


# --------------------------------------------------------------------------- #
# 6. the other end: a token describing itself, and the two ways to be refused
# --------------------------------------------------------------------------- #


def test_a_session_describes_itself_without_a_seat(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    run = _run(asgi, project["project_id"])
    issued = _mint(
        asgi,
        iid,
        project_id=project["project_id"],
        run_id=run["run_id"],
        actor_id="ext_user_1",
        actor_name="Ada",
    )

    status, _, description = asgi.request(
        "GET", f"/api/embed/{iid}/session", headers=_holder(issued["token"])
    )
    assert status == 200
    assert description["source"] == EMBED_SESSION_SOURCE
    assert description["actor_id"] == "ext_user_1"
    assert description["display_name"] == "Ada"
    assert description["integration_id"] == iid
    assert description["integration_name"] == "Acme Studio"
    assert description["context"]["run_id"] == run["run_id"]
    assert description["remaining_seconds"] > 0
    # No seat, no team role, no permission list — an embed session has none, and
    # reporting empty ones would invite callers to read this as ``/api/me``.
    assert "permissions" not in description
    assert "team_role" not in description


def test_a_missing_or_unusable_token_is_one_401(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]

    status, _, body = asgi.request("GET", f"/api/embed/{iid}/session")
    assert status == 401
    assert _error_code(body) == "embed_session_invalid"

    status, _, body = asgi.request(
        "GET", f"/api/embed/{iid}/session", headers=_holder(f"{EMBED_TOKEN_PREFIX}nope.nope")
    )
    assert status == 401
    assert _error_code(body) == "embed_session_invalid"


def test_the_path_id_and_the_token_id_must_agree(asgi):
    """A host allowed to frame A must not be served B's pinned run."""
    first = _integration(asgi, name="First", slug="first")
    second = _integration(asgi, name="Second", slug="second")
    project = _project(asgi)
    issued = _mint(asgi, first["integration_id"], project_id=project["project_id"])

    status, _, body = asgi.request(
        "GET",
        f"/api/embed/{second['integration_id']}/session",
        headers=_holder(issued["token"]),
    )
    assert status == 401
    assert _error_code(body) == "embed_session_invalid"


def test_disabling_an_integration_kills_links_it_already_minted(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    issued = _mint(asgi, iid, project_id=project["project_id"])
    holder = _holder(issued["token"])

    status, _, _ = asgi.request("GET", f"/api/embed/{iid}/session", headers=holder)
    assert status == 200

    asgi.request("PATCH", f"/api/external-integrations/{iid}", json_body={"status": "disabled"})
    status, _, body = asgi.request("GET", f"/api/embed/{iid}/session", headers=holder)
    assert status == 401
    assert _error_code(body) == "embed_session_invalid"

    # Re-enabling restores it: the token itself was never the thing that expired.
    asgi.request("PATCH", f"/api/external-integrations/{iid}", json_body={"status": "active"})
    status, _, _ = asgi.request("GET", f"/api/embed/{iid}/session", headers=holder)
    assert status == 200


def test_deleting_an_integration_kills_links_too(asgi):
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    issued = _mint(asgi, iid, project_id=project["project_id"])

    asgi.request("DELETE", f"/api/external-integrations/{iid}")
    status, _, body = asgi.request(
        "GET", f"/api/embed/{iid}/session", headers=_holder(issued["token"])
    )
    assert status == 401
    assert _error_code(body) == "embed_session_invalid"


# --------------------------------------------------------------------------- #
# 7. context pinning comes before scope
# --------------------------------------------------------------------------- #


@pytest.fixture()
def pinned(asgi):
    """One integration, one project with two runs, and a run-pinned session."""
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    pinned_run = _started_run(asgi, project["project_id"], "钉定")
    sibling_run = _run(asgi, project["project_id"], "兄弟")
    issued = _mint(
        asgi,
        iid,
        project_id=project["project_id"],
        run_id=pinned_run["run_id"],
        scopes=["project:read", "run:read", "run:review"],
    )
    return {
        "iid": iid,
        "project": project,
        "run": pinned_run,
        "sibling": sibling_run,
        "token": issued["token"],
        "holder": _holder(issued["token"]),
    }


def test_the_session_reaches_its_own_run(asgi, pinned):
    status, _, run = asgi.request(
        "GET", f"/api/runs/{pinned['run']['run_id']}", headers=pinned["holder"]
    )
    assert status == 200
    assert run["run_id"] == pinned["run"]["run_id"]

    status, _, requests = asgi.request(
        "GET", f"/api/runs/{pinned['run']['run_id']}/review-requests", headers=pinned["holder"]
    )
    assert status == 200
    assert len(requests) == 1
    assert requests[0]["stage_key"] == "plan"


def test_a_sibling_run_of_the_same_project_is_out_of_context(asgi, pinned):
    """Same project is not the same run — the pin is narrower than the project."""
    status, _, body = asgi.request(
        "GET", f"/api/runs/{pinned['sibling']['run_id']}", headers=pinned["holder"]
    )
    assert status == 403
    assert _error_code(body) == "permission_denied"
    assert body["detail"]["reason"] == "embed_context"


def test_a_tenant_wide_endpoint_is_never_reachable_from_an_embed(asgi, pinned):
    """An embed session is one project's guest; the tenant is not theirs."""
    for method, path in (
        ("GET", "/api/teams"),
        ("GET", "/api/external-integrations"),
        ("GET", "/api/api-keys"),
        ("GET", "/api/webhooks"),
        ("GET", "/api/audit-logs"),
    ):
        status, _, body = asgi.request(method, path, headers=pinned["holder"])
        assert status == 403, (method, path)
        assert _error_code(body) == "permission_denied"
        assert body["detail"]["reason"] == "embed_context", (method, path)


def test_an_unscoped_writable_endpoint_is_refused_as_a_scope_problem(asgi, pinned):
    """Context is right; the scope is what is missing — the two are kept apart."""
    status, _, body = asgi.request(
        "POST", f"/api/runs/{pinned['run']['run_id']}/start", json_body={}, headers=pinned["holder"]
    )
    assert status == 403
    assert body["detail"]["reason"] == "embed_scope"


def test_run_review_is_the_one_write_scope_a_session_may_carry(asgi, pinned):
    """V7.3's ``stage.waiting_review`` webhook -> the partner pops the embed ->
    a human approves there. That is the loop the write scope exists for."""
    status, _, requests = asgi.request(
        "GET", f"/api/runs/{pinned['run']['run_id']}/review-requests", headers=pinned["holder"]
    )
    assert status == 200
    review_request_id = requests[0]["review_request_id"]

    status, _, detail = asgi.request(
        "POST",
        f"/api/review-requests/{review_request_id}/decisions",
        json_body={
            "decision": "approve",
            "comment": "在外宿主页面内通过",
            "actor_user_id": "ext_user_1",
            "actor_display_name": "Ada",
        },
        headers=pinned["holder"],
    )
    assert status == 200, detail
    assert detail["status"] == "approved"
    assert detail["current_decision"] == "approve"
    assert len(detail["decisions"]) == 1


def test_the_default_session_cannot_review(asgi):
    """``run:review`` is opt-in; a session minted without it is read-only."""
    integration = _integration(asgi)
    iid = integration["integration_id"]
    project = _project(asgi)
    run = _started_run(asgi, project["project_id"])
    issued = _mint(asgi, iid, project_id=project["project_id"], run_id=run["run_id"])
    assert "run:review" not in issued["scopes"]

    status, _, requests = asgi.request(
        "GET",
        f"/api/runs/{run['run_id']}/review-requests",
        headers=_holder(issued["token"]),
    )
    assert status == 200

    status, _, body = asgi.request(
        "POST",
        f"/api/review-requests/{requests[0]['review_request_id']}/decisions",
        json_body={"decision": "approve", "actor_user_id": "ext_user_1"},
        headers=_holder(issued["token"]),
    )
    assert status == 403
    assert body["detail"]["reason"] == "embed_scope"


def test_a_refusal_is_audited_with_the_reason(asgi, pinned):
    asgi.request("GET", "/api/teams", headers=pinned["holder"])
    status, _, logs = asgi.request("GET", "/api/audit-logs")
    assert status == 200
    denials = [
        entry
        for entry in logs
        if entry["action"] == "permission.denied"
        and entry.get("payload", {}).get("reason", "").startswith("embed_")
    ]
    assert denials, "an embed refusal must leave the same evidence as any other"
    assert denials[-1]["payload"]["integration_id"] == pinned["iid"]
    assert denials[-1]["payload"]["embed_project_id"] == pinned["project"]["project_id"]


def test_an_embed_session_is_not_a_seat(asgi, pinned):
    """The token *replaces* the seat chain; it does not sit inside it."""
    status, _, me = asgi.request("GET", "/api/me", headers=pinned["holder"])
    # ``/api/me`` is open, so it must still answer — but as the embed identity.
    assert status == 200
    assert me["actor_id"] == "ext_user_1"
    assert me["source"] == EMBED_SESSION_SOURCE
    assert me["permissions"] == []


# --------------------------------------------------------------------------- #
# 8. two credentials, two verdicts
# --------------------------------------------------------------------------- #


def test_an_api_key_outranks_the_token(asgi, pinned):
    """The platform's own credential is the authority; a link may not override it."""
    status, _, created = asgi.request("POST", "/api/api-keys", json_body={"name": "platform"})
    assert status == 200, created

    status, _, page = asgi.request(
        "GET",
        "/api/external-integrations",
        headers={**pinned["holder"], "Authorization": f"Bearer {created['api_key']}"},
    )
    assert status == 200, "the key, not the token, decided this request"
    assert page["total"] == 1

    status, _, me = asgi.request(
        "GET", "/api/me", headers={"Authorization": f"Bearer {created['api_key']}"}
    )
    assert me["source"] == "api_key"


def test_a_trusted_header_does_not_escape_the_pin(asgi, pinned):
    """``X-Actor-Id`` is resolved *after* the embed dependency, and deliberately so.

    The header is a weaker claim than a signature, so it cannot widen a session:
    the token still decides the request and the tenant-wide endpoint stays out of
    reach. Anything else would make every pin advisory.
    """
    status, _, body = asgi.request(
        "GET", "/api/teams", headers=pinned["holder"], actor_id=OWNER
    )
    assert status == 403
    assert body["detail"]["reason"] == "embed_context"

    status, _, description = asgi.request(
        "GET", f"/api/embed/{pinned['iid']}/session", headers=pinned["holder"], actor_id=OWNER
    )
    assert status == 200
    assert description["source"] == EMBED_SESSION_SOURCE


def test_an_embed_token_in_the_authorization_header_is_not_an_embed_session(asgi, pinned):
    """The two headers stay separate so the two 401s stay distinguishable."""
    status, _, body = asgi.request(
        "GET",
        f"/api/embed/{pinned['iid']}/session",
        headers={"Authorization": f"Bearer {pinned['token']}"},
    )
    assert status == 401
    assert _error_code(body) == "api_key_invalid", "an embed token is not an API key"


def test_no_embed_header_is_zero_behaviour_change(asgi):
    """The precondition for every existing page and gate test still passing."""
    status, _, me = asgi.request("GET", "/api/me")
    assert status == 200
    assert me["source"] == "default"

    status, _, _ = asgi.request("GET", "/api/projects")
    assert status == 200


# --------------------------------------------------------------------------- #
# 9. the page shell: routing, framing and what never reaches the document
# --------------------------------------------------------------------------- #


def test_the_embed_pages_are_served_but_not_documented(asgi):
    from apps.api.app.main import app

    registered = {getattr(route, "path", None) for route in app.routes}
    assert {"/embed/{integration_id}", "/embed/{integration_id}/run/{run_id}"} <= registered

    documented = set(app.openapi()["paths"])
    assert "/embed/{integration_id}" not in documented
    assert "/api/embed/{integration_id}/session" in documented


def test_the_document_request_carries_the_framing_allowlist(asgi):
    integration = _integration(asgi, allowed_origins=["https://app.acme.test/"])
    iid = integration["integration_id"]

    status, headers, body = asgi.raw("GET", f"/embed/{iid}")
    assert status == 200
    assert headers["content-security-policy"] == "frame-ancestors https://app.acme.test"
    # The shell is per-integration, so it must never be cached or shared.
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"].startswith("text/html")

    # The page cannot echo the credential it does not have: the token is in the
    # fragment, which the browser never sends.
    text = body.decode("utf-8")
    assert EMBED_TOKEN_PREFIX not in text


def test_an_unknown_or_disabled_integration_still_renders_a_page(asgi):
    """A blank frame tells the viewer nothing; our own error state tells them."""
    status, headers, _ = asgi.raw("GET", "/embed/extint_absent")
    assert status == 200
    assert headers["content-security-policy"] == "frame-ancestors 'none'"

    integration = _integration(asgi)
    iid = integration["integration_id"]
    asgi.request("PATCH", f"/api/external-integrations/{iid}", json_body={"status": "disabled"})

    status, headers, _ = asgi.raw("GET", f"/embed/{iid}")
    assert status == 200
    assert headers["content-security-policy"] == "frame-ancestors 'none'"


def test_the_run_board_page_is_framed_by_the_same_allowlist(asgi):
    integration = _integration(asgi)
    status, headers, _ = asgi.raw(
        "GET", f"/embed/{integration['integration_id']}/run/run_whatever"
    )
    assert status == 200
    assert headers["content-security-policy"] == "frame-ancestors https://app.acme.test"


def test_the_shell_posts_to_the_framing_origin_and_never_to_a_wildcard():
    from apps.api.app.main import embed_html_path

    text = embed_html_path().read_text(encoding="utf-8")
    # ``frame-ancestors`` already decided who may frame the page, so the referrer's
    # own origin is the only target worth trusting — and a page framed by nobody
    # stays silent instead of broadcasting.
    assert 'postMessage' in text
    assert 'postMessage(entry, "*")' not in text
    assert "\"*\"" not in text.split("postMessage")[-1][:200]
    assert "s2v.embed.ready" in text
    assert "s2v.embed.resize" in text


def test_the_shell_scrubs_the_token_from_the_address_bar():
    from apps.api.app.main import embed_html_path

    text = embed_html_path().read_text(encoding="utf-8")
    assert "replaceState" in text, "a token left in the URL survives in history"
    assert "URLSearchParams" in text


# --------------------------------------------------------------------------- #
# 10. the link builder
# --------------------------------------------------------------------------- #


def test_the_page_path_puts_the_integration_id_where_the_server_can_read_it():
    assert embed_page_path("extint_1") == "/embed/extint_1"
    assert embed_page_path("extint_1", run_id="run_1") == "/embed/extint_1/run/run_1"


def test_the_link_keeps_the_credential_out_of_the_query_string():
    url = embed_url(
        "s2v_embed_payload.sig",
        base_url="https://studio.example/",
        integration_id="extint_1",
        run_id="run_1",
    )
    assert url == "https://studio.example/embed/extint_1/run/run_1#token=s2v_embed_payload.sig"
    assert "?token=" not in url


def test_a_session_may_only_carry_a_run_it_is_pinned_to(asgi):
    """A run id on a project-scoped session would be a pin nobody asked for."""
    integration = _integration(asgi)
    project = _project(asgi)
    session = EmbedSession(
        integration_id=integration["integration_id"],
        tenant_id=DEFAULT_TENANT_ID,
        actor_id="ext_user_1",
        project_id=project["project_id"],
        scopes=frozenset({"run:read"}),
    )
    assert session.run_id is None
    assert session.page == "workspace"
