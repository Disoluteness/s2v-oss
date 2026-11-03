"""Embed session identity (V7.4).

The third way to answer "who is calling", next to V6.5's trusted header and V7.2's
API key — and the only one where the caller is *not* our user. An embed session
belongs to one end user of an external platform, and the platform (not that end
user) holds the API key that obtained it.

``X-Embed-Token`` is a separate header from ``Authorization`` on purpose. The API
key layer owns ``Authorization: Bearer`` and answers anything it does not
recognise with 401 ``api_key_invalid``; sharing the header would collapse "your
embed link expired" and "your API key is wrong" into one indistinguishable
verdict. A request carrying no embed header is left completely alone, which is
what keeps every existing caller's behaviour byte-identical.

Scope narrowing and context pinning live downstream, in
``DatabaseStore.require_permission``: an embed session is not a seat, it is a
second *authorization source*, and the two rules it is held to (pinned context,
capped scopes) are enforced in the same place as every other refusal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from apps.api.app.actor import (
    EMBED_SESSION_SOURCE,
    EMBED_SESSION_STATE_KEY,
    RESOLVED_ACTOR_STATE_KEY,
    ActorContext,
    resolved_actor,
)
from apps.api.app.db_store import DatabaseStore
from apps.api.app.embed import EMBED_TOKEN_HEADER, decode_embed_session
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.routes import get_store


def embed_session_invalid(message: str) -> ApiError:
    """The one failure an unusable embed token produces."""
    return ApiError(ErrorCode.EMBED_SESSION_INVALID, message)


async def enforce_embed_identity(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """Turn ``X-Embed-Token: s2v_embed_...`` into the request's actor.

    An already-resolved API key outranks this: the platform's own credential is the
    authority, and letting a link override it would make a scoped key escapable by
    pasting a token into a header.

    ``X-Actor-Id`` does *not* outrank it, and that asymmetry is deliberate rather
    than an accident of ordering. ``enforce_api_key_identity`` runs as an
    app-level dependency, before this one, and parks its result on
    ``request.state``; the trusted header is only read later, inside
    ``resolve_actor``, as a fallback for the identity nothing else established. So
    a token that arrives alongside a header still decides the request, and the
    embed's pin holds. The header is a weaker claim than a signature; letting it
    widen a session would make every pin advisory.

    A token that does not verify is an error, never a silent fallback. Falling back
    to the default actor would hand the default team owner's rights to whoever
    presented a garbage token, so a link that expired, was tampered with, or
    belongs to a disabled integration gets one 401 with one message — the same
    stance ``api_key_invalid`` takes, and for the same reason: distinguishing the
    cases turns the endpoint into an oracle.
    """
    if resolved_actor(request) is not None:
        return
    token = (request.headers.get(EMBED_TOKEN_HEADER) or "").strip()
    if not token:
        return
    session = decode_embed_session(token)
    if session is None:
        raise embed_session_invalid("the embed token is unknown, tampered with or expired")
    # Re-reading the integration on every request is what makes ``disabled`` a
    # revocation lever. A stateless token cannot be revoked one by one, so this row
    # is the only thing standing between an old link and the tenant's data.
    if store.describe_embed_session(session) is None:
        raise embed_session_invalid("the embed token's integration is no longer active")
    setattr(
        request.state,
        RESOLVED_ACTOR_STATE_KEY,
        ActorContext(
            actor_id=session.actor_id,
            tenant_id=session.tenant_id,
            display_name=session.display_name,
            source=EMBED_SESSION_SOURCE,
        ),
    )
    setattr(request.state, EMBED_SESSION_STATE_KEY, session)
