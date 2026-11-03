"""Tenant scope enforcement (V1.1 T97).

Every id-shaped path parameter names a row, and every one of those rows (save
the deliberately platform-global tables) belongs to a tenant. Before T97 the
resource getters fetched by primary key alone, so a caller who could reach the
endpoint at all could read any tenant's row by guessing — or by holding — an id.

This module is the single point where that closes. ``enforce_tenant_scope`` is
mounted as an app-level dependency in ``main.py``, immediately after the
identity chain (API key → login session → embed token) and before the
permission guard, and it applies one rule to every matched route:

    a row addressed by the path must belong to the caller's tenant, or the
    request is answered with the same 404 a missing row would get.

Three properties are decisions, and each is load-bearing:

* **404, not 403.** A 403 confirms the row exists; a 404 refuses without
  saying whether the refusal was "absent" or "none of your business". The
  detail string is copied from the handler's own not-found message per
  resource, so the two 404s are indistinguishable on the wire.
* **A missing row stays the handler's problem.** The resolvers answer ``None``
  for an id that matches nothing, and the guard steps aside — the handler then
  produces the exact 404 it always has. The guard never invents a second
  not-found shape for a resource.
* **Embed sessions are callers too.** An embed token carries its integration's
  tenant, so the comparison uses it, not the default actor the raw request
  would otherwise resolve to. Embed context pinning (V7.4) continues to handle
  the finer-grained project/run promises on top.

Two id-shaped params are *deliberately* absent from the table:

* ``integration_id`` — the external-integration endpoints already scope their
  reads by the actor's tenant at the handler (T9.4), and
  ``GET /api/embed/{integration_id}/session`` is the embed *bootstrap*: it runs
  before any actor exists and its authority is the signed token itself.
* ``api_key_id`` — revocation is already tenant-scoped inside the store
  (``api_keys.revoke(..., tenant_id=actor.tenant_id)``).

One route is exempted outright: ``GET /api/assets/{asset_id}/content``. Its
authority model is the signed link (V8.4), not a seat — the signature is a
bearer capability minted for consumers who may hold no identity at all, and
gating it on a tenant the consumer cannot present would break the feature for
every non-default tenant. The signature still proves the right to the bytes;
the guard simply does not add a second, unsatisfiable test.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from apps.api.app.actor import embed_session, resolve_actor
from apps.api.app.db_store import DatabaseStore
from apps.api.app.permission_policy import _request_template
from apps.api.app.routes import get_store

#: path parameter → (store resolver, the handler's own not-found detail).
#: The detail is what makes the guard's 404 indistinguishable from the
#: handler's: an attacker comparing the two learns nothing about existence.
_RESOLVERS: dict[str, tuple[str, str]] = {
    "project_id": ("tenant_id_for_project", "project not found"),
    "run_id": ("tenant_id_for_run", "run not found"),
    "brand_kit_id": ("tenant_id_for_brand_kit", "brand kit not found"),
    "style_preset_id": ("tenant_id_for_style_preset", "style preset not found"),
    "collection_id": ("tenant_id_for_asset_collection", "asset collection not found"),
    "item_id": ("tenant_id_for_library_item", "asset library item not found"),
    "credential_id": (
        "tenant_id_for_provider_credential",
        "provider credential not found",
    ),
    "asset_id": ("tenant_id_for_asset", "asset not found"),
    "job_id": ("tenant_id_for_job", "background job not found"),
    "invocation_id": ("tenant_id_for_tool_invocation", "tool invocation not found"),
    "review_request_id": ("tenant_id_for_review_request", "review request not found"),
    "comment_id": ("tenant_id_for_review_comment", "review comment not found"),
    "attachment_id": ("tenant_id_for_review_attachment", "review attachment not found"),
    "team_id": ("tenant_id_for_team", "team not found"),
    "membership_id": ("tenant_id_for_team_membership", "team membership not found"),
    "subscription_id": (
        "tenant_id_for_webhook_subscription",
        "webhook subscription not found",
    ),
    "delivery_id": ("tenant_id_for_webhook_delivery", "webhook delivery not found"),
    "user_id": ("tenant_id_for_user", "user not found"),
}

#: The one route whose authority is a signature rather than a seat. See the
#: module docstring — the exemption is a property of the V8.4 link design, not
#: an oversight.
_SIGNATURE_AUTHORITATIVE_TEMPLATES = frozenset({"/assets/{asset_id}/content"})


async def enforce_tenant_scope(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """App-level dependency: refuse cross-tenant resource addressing with 404."""
    template = _request_template(request)
    if template in _SIGNATURE_AUTHORITATIVE_TEMPLATES:
        return
    embed = embed_session(request)
    expected = embed.tenant_id if embed is not None else resolve_actor(request).tenant_id
    for param, (resolver_name, detail) in _RESOLVERS.items():
        value = request.path_params.get(param)
        if value is None:
            continue
        row_tenant = getattr(store, resolver_name)(value)
        if row_tenant is None or row_tenant == expected:
            continue
        raise HTTPException(status_code=404, detail=detail)
