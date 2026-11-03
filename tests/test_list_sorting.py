"""V1.1 T95: controlled sorting for the paginated list endpoints.

Pins four things:

1. **Silence is legacy.** No ``sort`` parameter means the repository keeps its
   own default ordering — the no-parameter response is exactly what it was
   before controlled sorting existed (that is also what the whole pre-existing
   suite proves, this file just states it explicitly once).
2. **The whitelist is real.** A whitelisted field reorders; anything else —
   unknown field, the ``id`` tiebreaker itself, duplicates, empty tokens — is a
   422 with the stable ``validation_error`` code, not a silent fallback to
   server order.
3. **Pages are stable.** The primary key is appended as the final tiebreaker,
   so offset pagination over a non-unique sort key neither skips nor repeats
   rows; repeated identical queries return identical order.
4. **Every paginated endpoint behaves the same way.** The contract test pins
   the OpenAPI declaration; here each endpoint gets at least one happy-path
   sort and the shared 422 behaviour.
"""

from __future__ import annotations

import pytest
from harness import build_client

from apps.api.app.sorting import SortSpec, SortValidationError


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "list-sorting.db")
    try:
        yield client
    finally:
        client.close()


# --- SortSpec unit behaviour -------------------------------------------------


def test_resolve_without_parameter_defers_to_the_server_default() -> None:
    spec = SortSpec(allowed=("created_at", "name"), default="-created_at")
    assert spec.resolve(None) is None


def test_resolve_parses_single_multi_and_descending() -> None:
    spec = SortSpec(allowed=("created_at", "name"), default="-created_at")
    assert spec.resolve("name") == (("name", False), ("id", False))
    assert spec.resolve("-name") == (("name", True), ("id", False))
    assert spec.resolve("name,-created_at") == (
        ("name", False),
        ("created_at", True),
        ("id", False),
    )


def test_resolve_tolerates_surrounding_spaces() -> None:
    spec = SortSpec(allowed=("created_at", "name"), default="-created_at")
    assert spec.resolve(" name , -created_at ") == (
        ("name", False),
        ("created_at", True),
        ("id", False),
    )


def test_resolve_explicit_default_reproduces_legacy_tie_direction() -> None:
    spec = SortSpec(allowed=("created_at",), default="-created_at", pk_desc=True)
    assert spec.resolve("-created_at") == (("created_at", True), ("id", True))


@pytest.mark.parametrize(
    "raw",
    ["bogus", "id", "created_at,created_at", "created_at,,name", ""],
)
def test_resolve_rejects_unknown_tiebreaker_duplicate_and_empty(raw: str) -> None:
    spec = SortSpec(allowed=("created_at", "name"), default="-created_at")
    with pytest.raises(SortValidationError):
        spec.resolve(raw)


# --- api-keys: the full sorting story on one endpoint ------------------------


def _issue_key(asgi, name: str) -> dict:
    status, _, body = asgi.request("POST", "/api/api-keys", json_body={"name": name})
    assert status == 200, body
    return body


def test_api_keys_without_sort_keeps_the_legacy_default(asgi) -> None:
    for name in ("beta", "alpha", "gamma"):
        _issue_key(asgi, name)
    status, _, body = asgi.request("GET", "/api/api-keys")
    assert status == 200
    items = body["items"]
    # Legacy default: created_at descending. The row id is a random handle, so
    # rows created within the same clock tick may interleave — that is exactly
    # the instability the explicit ``sort`` parameter exists to fix.
    created = [item["created_at"] for item in items]
    assert created == sorted(created, reverse=True)


def test_api_keys_explicit_default_matches_the_implicit_order(asgi) -> None:
    for name in ("beta", "alpha", "gamma"):
        _issue_key(asgi, name)
    _, _, implicit = asgi.request("GET", "/api/api-keys")
    _, _, explicit = asgi.request("GET", "/api/api-keys?sort=-created_at")
    assert [item["api_key_id"] for item in implicit["items"]] == [
        item["api_key_id"] for item in explicit["items"]
    ]


def test_api_keys_sort_by_name_ascending_and_descending(asgi) -> None:
    for name in ("beta", "alpha", "gamma"):
        _issue_key(asgi, name)
    _, _, body = asgi.request("GET", "/api/api-keys?sort=name")
    assert [item["name"] for item in body["items"]] == ["alpha", "beta", "gamma"]
    _, _, body = asgi.request("GET", "/api/api-keys?sort=-name")
    assert [item["name"] for item in body["items"]] == ["gamma", "beta", "alpha"]


def test_api_keys_tied_sort_key_is_stable_across_repeated_queries(asgi) -> None:
    _issue_key(asgi, "same")
    _issue_key(asgi, "same")
    _issue_key(asgi, "same")
    _, _, first = asgi.request("GET", "/api/api-keys?sort=name")
    _, _, second = asgi.request("GET", "/api/api-keys?sort=name")
    order = [item["api_key_id"] for item in first["items"]]
    assert len(order) == 3
    assert order == [item["api_key_id"] for item in second["items"]]


@pytest.mark.parametrize(
    ("path", "raw"),
    [
        ("/api/api-keys", "key_hash"),
        ("/api/api-keys", "id"),
        ("/api/api-keys", "name,name"),
        ("/api/audit-logs", "payload"),
        ("/api/idempotency-keys", "response_body"),
        ("/api/webhooks", "secret"),
        ("/api/external-integrations", "tenant_id"),
    ],
)
def test_sort_outside_the_whitelist_is_422(asgi, path: str, raw: str) -> None:
    status, _, body = asgi.request("GET", f"{path}?sort={raw}")
    assert status == 422, body
    assert body["detail"]["code"] == "validation_error"
    assert body["detail"]["message"]


def test_sort_unknown_field_lists_the_allowed_fields(asgi) -> None:
    _, _, body = asgi.request("GET", "/api/api-keys?sort=nope")
    message = body["detail"]["message"]
    for allowed in ("created_at", "name", "status", "actor_id"):
        assert allowed in message


# --- the remaining paginated endpoints ---------------------------------------


def test_audit_logs_sort_by_action(asgi) -> None:
    _issue_key(asgi, "trails")
    _issue_key(asgi, "trails")
    status, _, body = asgi.request("GET", "/api/audit-logs?sort=action")
    assert status == 200
    entries = body["items"] if isinstance(body, dict) else body
    actions = [entry["action"] for entry in entries]
    assert actions == sorted(actions)


def test_audit_logs_without_sort_still_lists_newest_first(asgi) -> None:
    _issue_key(asgi, "trails")
    _issue_key(asgi, "trails")
    status, _, body = asgi.request("GET", "/api/audit-logs")
    assert status == 200
    entries = body["items"] if isinstance(body, dict) else body
    assert len(entries) >= 2
    assert entries[0]["created_at"] >= entries[-1]["created_at"]


def test_idempotency_keys_accepts_whitelisted_sort(asgi) -> None:
    status, _, body = asgi.request("GET", "/api/idempotency-keys?sort=state,-created_at")
    assert status == 200
    assert body["items"] == []


def test_webhooks_sort_by_name(asgi) -> None:
    for name in ("beta-hook", "alpha-hook"):
        status, _, body = asgi.request(
            "POST",
            "/api/webhooks",
            json_body={"name": name, "url": f"https://{name}.example.com/hook"},
        )
        assert status == 200, body
    _, _, body = asgi.request("GET", "/api/webhooks?sort=name")
    assert [item["name"] for item in body["items"]] == ["alpha-hook", "beta-hook"]


def test_webhook_deliveries_sort_is_declared_and_empty_safe(asgi) -> None:
    status, _, subscription = asgi.request(
        "POST",
        "/api/webhooks",
        json_body={"name": "ops", "url": "https://ops.example.com/hook"},
    )
    assert status == 200, subscription
    status, _, body = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries?sort=-status"
    )
    assert status == 200, body
    assert body["items"] == []
    status, _, body = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries?sort=attempt_count"
    )
    assert status == 422


def test_external_integrations_sort_by_name(asgi) -> None:
    for name in ("zeta-portal", "alpha-portal"):
        status, _, body = asgi.request(
            "POST",
            "/api/external-integrations",
            json_body={"name": name, "allowed_origins": ["https://portal.example.com"]},
        )
        assert status == 200, body
    _, _, body = asgi.request("GET", "/api/external-integrations?sort=name")
    assert [item["name"] for item in body["items"]] == ["alpha-portal", "zeta-portal"]
