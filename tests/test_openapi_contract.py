"""V7.1 OpenAPI contract.

The spec is the artefact an SDK is generated from, so it is treated as a
published interface rather than a build by-product. These tests pin the things a
generated client depends on:

* the shape of a failure (every documented error response is the V7.1
  ``ErrorEnvelope``, never FastAPI's stock ``HTTPValidationError``),
* the set of operations, so a route cannot appear or vanish without a failing
  test and a deliberate decision,
* operation ids being unique and present — duplicates silently break generators,
* the HTML pages staying out of the schema, and
* pagination inputs being declared where the ``Page`` envelope is returned.

``POLICY`` + ``OPEN_ENDPOINTS`` must partition the documented operations exactly.
That is the strongest statement available here: it means every endpoint the API
admits to having is one the permission layer has an explicit opinion about, and
every endpoint the permission layer governs is one the API admits to having.
"""

from __future__ import annotations

import re

from apps.api.app.main import API_VERSION, app
from apps.api.app.permission_policy import OPEN_ENDPOINTS, POLICY

HTTP_METHODS = frozenset({"get", "post", "patch", "put", "delete"})
#: Templates published for the HTML pages are deliberately absent.
PAGE_ROUTES = frozenset(
    {
        "/",
        "/workspace",
        "/dashboard",
        "/run-board",
        "/asset-library",
        "/team",
        # Operator settings: provider credential maintenance in the browser.
        # Same reasoning as the rest of the pages — it serves HTML, so it is not
        # part of the JSON contract the permission tables describe.
        "/settings",
        # V7.4: the embed shell. The integration id sits in the path because the
        # first document request carries no credential at all — without it the
        # server could not compute ``frame-ancestors`` for the response.
        "/embed/{integration_id}",
        "/embed/{integration_id}/run/{run_id}",
    }
)
#: A curated set of operations an SDK is actually built around. If one of these
#: disappears or is renamed, clients break even though the count still matches.
CRITICAL_OPERATIONS = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/error-codes"),
        ("GET", "/api/me"),
        ("GET", "/api/permissions"),
        ("POST", "/api/projects"),
        ("GET", "/api/projects"),
        ("GET", "/api/projects/{project_id}"),
        ("POST", "/api/projects/{project_id}/runs"),
        ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/runs/{run_id}/board"),
        ("POST", "/api/runs/{run_id}/start"),
        ("GET", "/api/audit-logs"),
        ("GET", "/api/idempotency-keys"),
        ("GET", "/api/teams"),
        ("POST", "/api/runs/{run_id}/provider-consents"),
        ("GET", "/api/runs/{run_id}/review-requests"),
        # V7.2: the SDK's own credential lifecycle, which a client bootstraps on.
        ("GET", "/api/api-keys"),
        ("POST", "/api/api-keys"),
        ("POST", "/api/api-keys/{api_key_id}/revoke"),
        # V7.3: registering an endpoint and replaying a dead delivery are the two
        # webhook operations an integrator cannot work around.
        ("POST", "/api/webhooks"),
        ("POST", "/api/webhook-deliveries/{delivery_id}/requeue"),
        # V7.4: an embeddable workspace lives or dies on these two — one turns an
        # integration into a shareable link, the other is how the shell learns
        # who it is and what it may do.
        ("POST", "/api/external-integrations/{integration_id}/embed-sessions"),
        ("GET", "/api/embed/{integration_id}/session"),
    }
)


def _spec() -> dict:
    return app.openapi()


def _operations(spec: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in spec["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }


def test_the_spec_builds_and_declares_the_api_version():
    spec = _spec()
    info = spec["info"]
    assert info["version"] == API_VERSION
    assert info["title"]
    assert info["description"]


def test_the_documented_operation_set_is_pinned():
    """Adding or removing an operation must be a conscious edit to this file."""
    spec = _spec()
    operations = _operations(spec)
    # V8.5 added the four /api/deployment operations (117 -> 120 paths,
    # 148 -> 152 operations). V1.1 added the three /api/auth operations and the
    # user password endpoint (120 -> 124 paths, 152 -> 156 operations).
    # P2 added the run decision log (124 -> 125 paths, 156 -> 157 operations).
    assert len(spec["paths"]) == 125
    assert len(operations) == 157
    assert CRITICAL_OPERATIONS <= operations


def test_the_spec_and_the_permission_tables_partition_each_other():
    spec = _spec()
    operations = _operations(spec)
    declared = set(POLICY) | set(OPEN_ENDPOINTS)
    assert operations == declared, (
        f"spec only: {sorted(operations - declared)} | "
        f"tables only: {sorted(declared - operations)}"
    )
    assert not (set(POLICY) & set(OPEN_ENDPOINTS))


def test_the_html_pages_are_not_part_of_the_api_contract():
    spec = _spec()
    documented = set(spec["paths"])
    for page in PAGE_ROUTES:
        assert page not in documented, page


def test_failures_are_documented_as_the_v7_1_envelope():
    """The spec must not advertise a shape the app never returns."""
    spec = _spec()
    schemas = spec["components"]["schemas"]
    assert "ErrorEnvelope" in schemas
    assert "HTTPValidationError" not in schemas

    assert schemas["ErrorEnvelope"]["properties"]["detail"]["$ref"].endswith("/ErrorDetail")
    detail = schemas["ErrorDetail"]
    assert {"code", "message"} <= set(detail["properties"])
    assert {"code", "message"} <= set(detail["required"])
    # Gate failures carry extra keys next to code/message.
    assert detail.get("additionalProperties") is not False

    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            for status, response in operation.get("responses", {}).items():
                if not status.startswith("4") and not status.startswith("5"):
                    continue
                content = response.get("content", {}).get("application/json")
                assert content is not None, (method, path, status)
                assert content["schema"]["$ref"].endswith("/ErrorEnvelope"), (
                    method,
                    path,
                    status,
                )


def test_operation_ids_are_present_and_unique():
    spec = _spec()
    ids = [
        item[method]["operationId"]
        for item in spec["paths"].values()
        for method in item
        if method in HTTP_METHODS
    ]
    assert all(ids)
    assert len(ids) == len(set(ids)), sorted(
        {value for value in ids if ids.count(value) > 1}
    )


def test_every_operation_declares_a_success_response():
    spec = _spec()
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            assert any(status.startswith("2") for status in operation["responses"]), (
                method,
                path,
            )


def test_path_templates_never_leak_a_concrete_identifier():
    """A literal id in a template would mean a route was declared with one."""
    concrete_id = re.compile(
        r"\b(?:project|run|asset|team|tmem|pmem|job|acol|ach|rr|inv)_[0-9a-f]{6,}\b"
    )
    for path in _spec()["paths"]:
        assert not concrete_id.search(path), path
        assert path.count("{") == path.count("}"), path
        for segment in path.split("/"):
            if "{" in segment or "}" in segment:
                assert segment.startswith("{") and segment.endswith("}"), (path, segment)


def test_paginated_endpoints_declare_their_paging_inputs():
    spec = _spec()
    operation = spec["paths"]["/api/idempotency-keys"]["get"]
    parameters = {parameter["name"] for parameter in operation["parameters"]}
    assert {"limit", "offset", "sort"} <= parameters
    schema_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert schema_ref.endswith("/Page_IdempotencyKeyEntry_")

    # Endpoints that predate the Page envelope still accept the same inputs and
    # report the row count in a header instead of the body.
    audit = spec["paths"]["/api/audit-logs"]["get"]
    audit_parameters = {parameter["name"] for parameter in audit["parameters"]}
    assert {"limit", "offset", "sort"} <= audit_parameters
    assert "Page" not in str(audit["responses"]["200"])

    # V1.1 T95: every offset-paginated list declares the controlled ``sort``
    # parameter — six endpoints, one contract.
    sortable = [
        "/api/api-keys",
        "/api/webhooks",
        "/api/external-integrations",
    ]
    for path in sortable:
        names = {p["name"] for p in spec["paths"][path]["get"]["parameters"]}
        assert "sort" in names, path
    delivery_params = {
        p["name"] for p in spec["paths"]["/api/webhooks/{subscription_id}/deliveries"]["get"]["parameters"]
    }
    assert "sort" in delivery_params
