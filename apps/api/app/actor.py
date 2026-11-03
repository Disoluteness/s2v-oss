"""Request identity resolution.

A request's actor comes from one of two places, and every downstream check reads
the same ``ActorContext`` regardless of which:

* a **programmatic credential** — ``Authorization: Bearer s2v_...`` (V7.2). The
  key is verified by ``api_keys.enforce_api_key_identity``, which runs as an
  app-level dependency and parks the resolved actor on ``request.state``. This
  function picks that up first, so the permission layer never has to know how the
  caller proved who they were.
* a **trusted header** injected by whatever sits in front of the API (an internal
  gateway, an SSO proxy, a CLI wrapper). When no identity is present the request
  falls back to the configured default actor, which is a team owner — that is
  what keeps the existing unauthenticated callers (the deployment gate script,
  the MVP sample path, every unit test) working unchanged.

Trust is governed by ``settings.actor_trust_mode``:

- ``open``  — the header is honoured as-is. Fine for a single-tenant internal box.
- ``secret`` — the header is honoured only when ``X-Actor-Secret`` matches
  ``settings.actor_header_secret``; otherwise the request falls back to the default
  actor. Use this once the API is reachable by more than the gateway.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from apps.api.app.config import settings
from apps.api.app.embed import EmbedSession
from apps.api.app.repositories import DEFAULT_TENANT_ID

ACTOR_ID_HEADER = "x-actor-id"
ACTOR_NAME_HEADER = "x-actor-name"
ACTOR_SECRET_HEADER = "x-actor-secret"

# --- V7.2 API key identity ---------------------------------------------------
# ``api_keys.enforce_api_key_identity`` resolves a bearer credential and parks the
# result on ``request.state``; these helpers are the read side. They live here,
# next to ``resolve_actor``, so both the routes and the permission guard can ask
# "which credential was used?" without importing the module that verifies it —
# which would be a cycle, since that module needs ``get_store`` from the routes.
API_KEY_SOURCE = "api_key"
RESOLVED_ACTOR_STATE_KEY = "actor"
SCOPES_STATE_KEY = "api_key_scopes"
PREFIX_STATE_KEY = "api_key_prefix"

# --- V1.1 username-password session identity ----------------------------------
# ``auth.enforce_session_identity`` verifies ``Bearer s2v_sess_...`` and parks the
# resolved actor on the same ``request.state`` slot an API key uses, so the
# priority read in ``resolve_actor`` needs no new branch — whichever dependency
# resolved first wins, and the mounting order in ``main.py`` is the priority.
# The prefix lives here (not in ``auth.py``) so ``api_keys`` can recognise and
# defer session-shaped tokens without importing the session module.
SESSION_SOURCE = "session"
SESSION_TOKEN_PREFIX = "s2v_sess_"
SESSION_STATE_KEY = "auth_session"


@dataclass(frozen=True)
class SessionIdentity:
    """What a verified login session says about the caller.

    The ORM rows ride along because the auth endpoints need fields no
    ``ActorContext`` can carry — which user row backed the login, and which
    ``user_sessions`` row a logout must revoke. Same shape of problem the embed
    session solved by riding next to its actor, same solution.
    """

    session_id: str
    user_id: str
    tenant_id: str
    user: object  # db_models.UserRecord; typed loosely to keep this module leaf-light
    record: object  # db_models.UserSessionRecord


def session_identity(request: Request) -> SessionIdentity | None:
    """The verified login session behind this request, if any."""
    identity = getattr(request.state, SESSION_STATE_KEY, None)
    return identity if isinstance(identity, SessionIdentity) else None


def resolved_actor(request: Request) -> ActorContext | None:
    """The actor a bearer credential already established, if any."""
    resolved = getattr(request.state, RESOLVED_ACTOR_STATE_KEY, None)
    return resolved if isinstance(resolved, ActorContext) else None


def api_key_scopes(request: Request) -> frozenset[str] | None:
    """The narrowing scopes of the presented key, or ``None`` when unrestricted."""
    return getattr(request.state, SCOPES_STATE_KEY, None)


def api_key_prefix(request: Request) -> str | None:
    return getattr(request.state, PREFIX_STATE_KEY, None)


# --- V7.4 embed session identity ---------------------------------------------
# ``embed_identity.enforce_embed_identity`` resolves ``X-Embed-Token`` and parks the
# decoded session on ``request.state`` next to the actor. The session rides along
# because the permission guard needs two things from it that an ``ActorContext``
# cannot carry: the scope set it was minted with, and the project/run it is pinned
# to. Keeping the readers here mirrors the API key helpers above — the guard asks
# "what credential was used?" without importing the module that verified it.
EMBED_SESSION_SOURCE = "embed_session"
EMBED_SESSION_STATE_KEY = "embed_session"


def embed_session(request: Request) -> EmbedSession | None:
    """The embed session behind this request, or ``None`` for every other caller."""
    session = getattr(request.state, EMBED_SESSION_STATE_KEY, None)
    return session if isinstance(session, EmbedSession) else None


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    tenant_id: str
    display_name: str | None = None
    source: str = "default"

    @property
    def is_default(self) -> bool:
        return self.source == "default"


def _header_is_trusted(request: Request) -> bool:
    mode = (settings.actor_trust_mode or "open").strip().lower()
    if mode != "secret":
        return True
    expected = settings.actor_header_secret
    if not expected:
        # Secret mode without a configured secret trusts nobody; fall back to the
        # default actor rather than silently trusting every caller.
        return False
    return request.headers.get(ACTOR_SECRET_HEADER) == expected


def resolve_actor(request: Request) -> ActorContext:
    # An API key (V7.2) outranks the header: the credential is the authority, and
    # a header able to override it would make a scoped key trivially escapable.
    resolved = resolved_actor(request)
    if resolved is not None:
        return resolved
    if _header_is_trusted(request):
        raw_id = (request.headers.get(ACTOR_ID_HEADER) or "").strip()
        if raw_id:
            raw_name = (request.headers.get(ACTOR_NAME_HEADER) or "").strip()
            return ActorContext(
                actor_id=raw_id[:64],
                tenant_id=DEFAULT_TENANT_ID,
                display_name=raw_name[:160] or None,
                source="header",
            )
    return ActorContext(
        actor_id=settings.default_actor_id,
        tenant_id=DEFAULT_TENANT_ID,
        display_name=settings.default_actor_name,
        source="default",
    )
