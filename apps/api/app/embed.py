"""Embeddable workspace sessions (V7.4).

An **embed session** is how an external platform opens the Studio inside its own
UI for one specific run, without the end user having an account here. It is a
*stateless signed token* minted by us on request — there is no session table and
nothing to clean up:

    s2v_embed_<base64url(payload)>.<hex hmac_sha256(secret, base64url_payload)>

Three decisions are baked into that shape.

**The platform signs, the partner does not.** The partner calls the mint endpoint
with its own API key and receives a ready-made link; it never holds a signing
key. Putting the signature on our side means the partner cannot fabricate a
session for a run it was never granted, and there is exactly one implementation
of the format.

**Signature, not encryption.** The payload travels in the clear and the token
carries no secret — only ids the caller already holds. Signing buys integrity
(``exp`` cannot be extended, ``pid``/``rid`` cannot be swapped), which is what
actually matters for a value that lives in a URL.

**The token is an authorization source, not a seat.** The end user has no
``team_memberships`` row, so the seat chain would answer "no permissions" for
every request. Instead the token carries a scope set — capped by
``EMBED_ALLOWED_SCOPES`` at issue time *and* re-capped at request time, so
narrowing the allowlist in a later release takes effect immediately for tokens
that are already out in the wild.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode, urlparse

from apps.api.app.config import settings

#: Distinct from the ``s2v_`` API key prefix so the two credentials can never be
#: confused, and so "this is an embed session" is obvious in a log line.
EMBED_TOKEN_PREFIX = "s2v_embed_"

TOKEN_VERSION = 1

EMBED_PAGE_WORKSPACE = "workspace"
EMBED_PAGE_RUN_BOARD = "run-board"
EMBED_PAGES: frozenset[str] = frozenset({EMBED_PAGE_WORKSPACE, EMBED_PAGE_RUN_BOARD})

#: What a session gets when the caller does not narrow it: enough to *look*.
EMBED_DEFAULT_SCOPES: frozenset[str] = frozenset(
    {"project:read", "run:read", "asset:read", "library:read"}
)

#: The ceiling. ``run:review`` is the one deliberate write permission — reviewing
#: a waiting stage is exactly what an embed is for (V7.3's ``stage.waiting_review``
#: webhook -> the partner pops the embed -> a human approves there). ``run:write``
#: and ``run:start`` stay out: they can dispatch paid providers, and a link that
#: shows up in a third party's page should not be able to spend money.
EMBED_ALLOWED_SCOPES: frozenset[str] = EMBED_DEFAULT_SCOPES | {"run:review"}

#: Short by default because the value ends up in a URL. The ceiling exists so a
#: careless caller cannot mint a month-long capability.
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 86400

#: Where the embed shell lives. Kept alongside the token helpers so the mint
#: response and the HTML routes cannot drift apart.
EMBED_PATH = "/embed"

#: The header an embed page uses to present its token.
#:
#: Deliberately *not* ``Authorization: Bearer``: that header belongs to the API
#: key layer (V7.2), which answers any Bearer it does not recognize with 401
#: ``api_key_invalid``. Two credentials with two headers keeps both verdicts
#: honest — a browser can tell "your embed link expired" from "your API key is
#: wrong" instead of getting one indistinguishable 401.
EMBED_TOKEN_HEADER = "X-Embed-Token"


@dataclass(frozen=True)
class EmbedSession:
    """A decoded, signature-verified, unexpired embed session."""

    integration_id: str
    tenant_id: str
    actor_id: str
    project_id: str
    scopes: frozenset[str]
    page: str = EMBED_PAGE_WORKSPACE
    run_id: str | None = None
    display_name: str | None = None
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    token_id: str = ""

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def embed_signing_secret() -> bytes:
    """The deployment-level signing key.

    Rotating it invalidates every embed link in flight, which is the intended
    emergency lever — the same property ``API_KEY_PEPPER`` has for API keys.
    """
    return (settings.embed_token_secret or "dev-only-change-me").encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(encoded_payload: str) -> str:
    return hmac.new(
        embed_signing_secret(),
        encoded_payload.encode("ascii"),
        sha256,
    ).hexdigest()


def is_embed_token(token: str | None) -> bool:
    return bool(token) and token.strip().startswith(EMBED_TOKEN_PREFIX)


def normalize_scopes(scopes: Any) -> frozenset[str]:
    """Coerce a requested scope list into a set, dropping blanks and duplicates."""
    if scopes is None:
        return frozenset(EMBED_DEFAULT_SCOPES)
    if isinstance(scopes, str):
        scopes = [part.strip() for part in scopes.split(",")]
    return frozenset(str(scope).strip() for scope in scopes if str(scope).strip())


def unknown_scopes(scopes: frozenset[str]) -> list[str]:
    """Requested scopes that are outside the embed allowlist (sorted)."""
    return sorted(scope for scope in scopes if scope not in EMBED_ALLOWED_SCOPES)


def effective_embed_scopes(scopes: frozenset[str] | None) -> frozenset[str]:
    """What a session may actually do: its scopes, re-capped by today's allowlist."""
    requested = EMBED_DEFAULT_SCOPES if scopes is None else scopes
    # The cap applies on every path, including the default one: a scope removed
    # from the allowlist must stop being granted to tokens that already carry it,
    # whether they named their scopes or took the default set.
    return frozenset(scope for scope in requested if scope in EMBED_ALLOWED_SCOPES)


def default_ttl_seconds() -> int:
    return int(settings.embed_token_default_ttl_seconds or DEFAULT_TTL_SECONDS)


def max_ttl_seconds() -> int:
    return int(settings.embed_token_max_ttl_seconds or MAX_TTL_SECONDS)


def clamp_ttl(ttl_seconds: int | None) -> int:
    """Bound a requested lifetime; ``None`` means the default, not "forever"."""
    ceiling = max_ttl_seconds()
    if ttl_seconds is None:
        return min(default_ttl_seconds(), ceiling)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    return min(int(ttl_seconds), ceiling)


def issue_embed_session(
    *,
    integration_id: str,
    tenant_id: str,
    actor_id: str,
    project_id: str,
    run_id: str | None = None,
    page: str = EMBED_PAGE_WORKSPACE,
    display_name: str | None = None,
    scopes: frozenset[str] | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[str, EmbedSession]:
    """Mint a token and return it together with its decoded form.

    Raises ``ValueError`` for anything the caller must fix (bad page, out-of-range
    ttl, a scope above the ceiling) so the route can turn it into one 400 rather
    than silently downgrading the request.
    """
    if page not in EMBED_PAGES:
        raise ValueError(f"unsupported embed page: {page}")
    resolved_scopes = normalize_scopes(scopes)
    rejected = unknown_scopes(resolved_scopes)
    if rejected:
        raise ValueError(
            "embed sessions cannot carry these scopes: " + ", ".join(rejected)
        )
    ttl = clamp_ttl(ttl_seconds)
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl)
    token_id = secrets.token_hex(8)
    payload: dict[str, Any] = {
        "v": TOKEN_VERSION,
        "iid": integration_id,
        "tid": tenant_id,
        "sub": actor_id[:64],
        "pid": project_id,
        "pg": page,
        "scp": sorted(resolved_scopes),
        "exp": int(expires_at.timestamp()),
        "jti": token_id,
    }
    if run_id:
        payload["rid"] = run_id
    if display_name:
        payload["nam"] = display_name[:160]
    encoded = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    token = f"{EMBED_TOKEN_PREFIX}{encoded}.{_sign(encoded)}"
    session = EmbedSession(
        integration_id=integration_id,
        tenant_id=tenant_id,
        actor_id=payload["sub"],
        project_id=project_id,
        run_id=run_id,
        page=page,
        display_name=payload.get("nam"),
        scopes=resolved_scopes,
        expires_at=expires_at,
        token_id=token_id,
    )
    return token, session


def decode_embed_session(token: str | None, *, now: datetime | None = None) -> EmbedSession | None:
    """Verify and decode a token, or ``None`` if it is unusable in any way.

    One ``None`` for malformed / unknown-signature / expired is deliberate — the
    same stance ``api_key_invalid`` takes. Telling a caller *why* a token failed
    turns the endpoint into an oracle, and the holder of a valid token never sees
    this branch.
    """
    if not is_embed_token(token):
        return None
    raw = (token or "").strip()[len(EMBED_TOKEN_PREFIX) :]
    encoded, _, signature = raw.partition(".")
    if not encoded or not signature:
        return None
    if not hmac.compare_digest(signature, _sign(encoded)):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != TOKEN_VERSION:
        return None
    try:
        integration_id = str(payload["iid"])
        tenant_id = str(payload["tid"])
        actor_id = str(payload["sub"])
        project_id = str(payload["pid"])
        page = str(payload.get("pg") or EMBED_PAGE_WORKSPACE)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        raw_scopes = payload["scp"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(raw_scopes, list) or page not in EMBED_PAGES:
        return None
    current = now or datetime.now(UTC)
    if expires_at <= current:
        return None
    run_id = payload.get("rid")
    display_name = payload.get("nam")
    return EmbedSession(
        integration_id=integration_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        project_id=project_id,
        run_id=str(run_id) if run_id else None,
        page=page,
        display_name=str(display_name) if display_name else None,
        scopes=effective_embed_scopes(normalize_scopes(raw_scopes)),
        expires_at=expires_at,
        token_id=str(payload.get("jti") or ""),
    )


def embed_page_path(integration_id: str, *, run_id: str | None = None) -> str:
    """The page an embed link opens, with the integration id in the path.

    The *integration id* goes in the path rather than the token because the
    browser's very first GET cannot carry the token — it lives in the fragment,
    and a fragment is never sent to the server. So the document request needs
    something else it can look the row up by, and ``frame-ancestors`` is only
    computable if it has that. The id is not a secret: the partner holds it
    already, and it authorizes nothing on its own.
    """
    if run_id:
        return f"{EMBED_PATH}/{integration_id}/run/{run_id}"
    return f"{EMBED_PATH}/{integration_id}"


def embed_url(
    token: str,
    *,
    base_url: str,
    integration_id: str,
    page: str | None = None,
    run_id: str | None = None,
) -> str:
    """The link an external platform hands to its browser.

    Two decisions meet here. The token goes in the **fragment** (``#token=``)
    rather than the query string, because fragments are not sent to the server
    and are not forwarded in ``Referer`` — so the credential does not land in
    the access logs of the partner's own infrastructure. And the integration id
    goes in the **path**, because the server needs something it can actually
    read on that first document request in order to emit a ``frame-ancestors``
    allowlist. The run board form is a real path (not a query parameter) so the
    iframe ``src`` a human has to read stays legible and bookmarkable.
    """
    root = base_url.rstrip("/")
    path = embed_page_path(integration_id, run_id=run_id)
    query = "?" + urlencode({"page": page}) if page else ""
    return f"{root}{path}{query}#token={token}"


def normalize_origin(value: str) -> str:
    """Reduce an origin to ``scheme://host[:port]``, or return it unchanged.

    Only the origin matters for ``frame-ancestors``; a path or a trailing slash
    would make the directive match nothing, which fails *closed* (the page can no
    longer be framed) and is therefore confusing rather than dangerous. Trimming
    here keeps what an operator typed and what the browser compares identical.
    """
    return value.strip().rstrip("/")


def validate_origins(origins: list[str]) -> tuple[list[str], str | None]:
    """Validate an allowlist, returning ``(normalized, error)``.

    A complete origin is required — no wildcards, no paths — because the value is
    emitted verbatim into a security header. ``*`` in ``frame-ancestors`` would
    mean "any site may frame a page that carries tenant data", which is exactly
    the clickjacking scenario the allowlist exists to prevent, so it is refused
    at write time rather than merely documented.
    """
    normalized: list[str] = []
    for raw in origins:
        origin = normalize_origin(raw)
        if not origin:
            continue
        if origin == "*":
            return [], "allowed_origins must list complete origins, not '*'"
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return [], f"allowed_origins entry is not an origin: {raw}"
        if parsed.path or parsed.query or parsed.fragment:
            return [], f"allowed_origins entry must not carry a path: {raw}"
        candidate = f"{parsed.scheme}://{parsed.netloc}"
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized, None


def csp_frame_ancestors(origins: list[str] | None) -> str:
    """The ``frame-ancestors`` directive for an embed page.

    An unknown or disabled integration gets ``'none'``: the page still renders
    (so the browser shows our own error state instead of a blank frame) but no
    site can keep it embedded.
    """
    resolved = [normalize_origin(origin) for origin in (origins or []) if normalize_origin(origin)]
    if not resolved:
        return "frame-ancestors 'none'"
    return "frame-ancestors " + " ".join(resolved)
