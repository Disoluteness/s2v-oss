"""Username-password session identity (V1.1).

The platform has had three ways to answer "who is calling" — trusted header,
API key, embed token — and all of them assume someone *else* established the
identity: a gateway, an operator with a console, an external platform. This
module adds the one that needs nobody else: a user proves who they are with a
password (see ``passwords.py`` and ``POST /api/auth/login``) and then presents a
bearer session token, exactly the shape ``/login`` on any self-hosted service
takes.

Two decisions are worth recording:

* **The dependency runs after the API key one and before the embed one.**
  Priority is decided by the mounting order in ``main.py`` — API key → session
  → trusted header → default — so this module never has to argue about it: a
  request that already carries a resolved actor is left alone, and a session
  token simply becomes the actor when nothing outranks it.
* **An unusable session is one answer.** Unknown, revoked, expired and
  password-rotated all come back as the same 401 ``session_invalid``, so the
  endpoint is not a session oracle — the same one-verdict rule the API key and
  embed layers follow.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from apps.api.app.actor import (
    RESOLVED_ACTOR_STATE_KEY,
    SESSION_SOURCE,
    SESSION_STATE_KEY,
    ActorContext,
)
from apps.api.app.api_keys import parse_bearer
from apps.api.app.db_store import DatabaseStore
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.passwords import session_token_prefix_of
from apps.api.app.routes import get_store


async def enforce_session_identity(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """Turn ``Bearer s2v_sess_...`` into the request's actor.

    A request with no bearer, or with a bearer that is not session-shaped (the
    API key dependency has already judged those), is left completely alone —
    the same leave-no-trace property the other identity dependencies have.
    """
    if getattr(request.state, RESOLVED_ACTOR_STATE_KEY, None) is not None:
        # An API key outranks the session: the credential that resolved first is
        # the identity, exactly as keys outrank headers.
        return
    token = parse_bearer(request.headers.get("authorization"))
    if token is None or session_token_prefix_of(token) is None:
        return
    identity = store.authenticate_session_token(token)
    if identity is None:
        raise ApiError(
            ErrorCode.SESSION_INVALID,
            "the session token is unknown, revoked or expired",
        )
    setattr(
        request.state,
        RESOLVED_ACTOR_STATE_KEY,
        ActorContext(
            actor_id=identity.user_id,
            tenant_id=identity.tenant_id,
            display_name=identity.user.name,
            source=SESSION_SOURCE,
        ),
    )
    setattr(request.state, SESSION_STATE_KEY, identity)
