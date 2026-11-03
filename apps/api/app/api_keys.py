"""Programmatic request identity via API keys (V7.2).

V6.5 established one way to answer "who is calling": a trusted header injected by
whatever sits in front of the API. V7.2 adds a second — a credential the caller
holds itself — and the point of this module is that the rest of the system does
not care which was used. Both funnel into the same ``ActorContext``, the same
``resolve_actor`` and therefore the same permission check.

The resolution runs as an app-level dependency ahead of the permission guard, so
by the time any route or guard runs, ``request.state.actor`` already answers the
question. A key deliberately outranks the header: the credential is the
authority, and letting a header override it would make a scoped key trivially
escapable.

Scope narrowing lives downstream, in ``DatabaseStore.require_permission``: a key
can only ever hold *less* than its owner, never more.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from apps.api.app.actor import (
    API_KEY_SOURCE,
    PREFIX_STATE_KEY,
    RESOLVED_ACTOR_STATE_KEY,
    SCOPES_STATE_KEY,
    SESSION_TOKEN_PREFIX,
    ActorContext,
)
from apps.api.app.db_store import DatabaseStore
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.routes import get_store

AUTHORIZATION_HEADER = "authorization"
BEARER_SCHEME = "bearer"


def parse_bearer(header: str | None) -> str | None:
    """Return the token of ``Bearer <token>``, or ``None`` for anything else."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return None
    return token.strip() or None


async def enforce_api_key_identity(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """Turn ``Authorization: Bearer s2v_...`` into the request's actor.

    A request with no ``Authorization`` header is left completely alone, which is
    what keeps every existing header-based and unauthenticated caller working
    unchanged — the same property the ``Idempotency-Key`` header has.

    A session-shaped token (``s2v_sess_...``, V1.1) is deferred rather than
    judged: it belongs to the session dependency that runs immediately after
    this one, and answering it here with ``api_key_invalid`` would report the
    wrong credential type for every login-session caller.
    """
    token = parse_bearer(request.headers.get(AUTHORIZATION_HEADER))
    if token is None:
        return
    if token.startswith(SESSION_TOKEN_PREFIX):
        return
    record = store.authenticate_api_key(token)
    if record is None:
        # One message for unknown / revoked / expired / malformed: distinguishing
        # them would turn this endpoint into a key oracle.
        raise ApiError(
            ErrorCode.API_KEY_INVALID,
            "the supplied API key is unknown, revoked or expired",
        )
    setattr(
        request.state,
        RESOLVED_ACTOR_STATE_KEY,
        ActorContext(
            actor_id=record.actor_id,
            tenant_id=record.tenant_id,
            display_name=None,
            source=API_KEY_SOURCE,
        ),
    )
    setattr(request.state, PREFIX_STATE_KEY, record.key_prefix)
    setattr(
        request.state,
        SCOPES_STATE_KEY,
        frozenset(record.scopes) if record.scopes is not None else None,
    )
