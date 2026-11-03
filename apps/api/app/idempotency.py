"""Idempotent writes via the ``Idempotency-Key`` header (V7.1).

A client that wants to retry a create without risking a duplicate stamps the
request with a key of its own choosing::

    POST /api/projects
    Idempotency-Key: 8f1c...  (client-generated, unique per logical operation)

The first request claims the key, runs, and records its response. A retry with
the same key and the same body replays that recorded response (marked with
``Idempotency-Replayed: true``) instead of running the work twice. The same key
with a *different* body is a client bug, and is refused rather than silently
aliased to the first payload.

Requests without the header are untouched — that is what keeps every existing
caller working, and it is why this is opt-in rather than always-on.

Two pieces cooperate, because they need to see different moments of the request:

* :func:`enforce_idempotency` runs as an app-level dependency. It can read the
  body safely (FastAPI has already buffered it) and therefore can fingerprint
  the request, detect a replay, and claim a new key.
* :class:`IdempotencyRecorderMiddleware` runs outside the routing layer, so it
  is the only place that can see the *finished* response and record it.

They communicate through ``scope["state"]``, which the dependency writes and the
middleware reads. Both hold the same scope object, so no extra plumbing is
needed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any

from fastapi import Depends, Request

from apps.api.app.actor import resolve_actor
from apps.api.app.config import settings
from apps.api.app.db import SessionLocal
from apps.api.app.db_store import DatabaseStore
from apps.api.app.errors import ApiError, ErrorCode
from apps.api.app.repositories import IdempotencyClaim, IdempotencyOutcome, IdempotencyRepository
from apps.api.app.routes import get_store

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "idempotency-key"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MAX_KEY_LENGTH = 255
#: Where the dependency leaves its claim for the middleware to settle.
_CLAIM_STATE_KEY = "idempotency_claim_id"

# The middleware needs a session of its own: the request's session belongs to the
# endpoint and is closed as soon as it finishes. Exposed as a module attribute so
# tests (which run against their own engine) can point it at the right database.
_session_factory = SessionLocal


class IdempotentReplay(Exception):
    """Raised to short-circuit a request whose work was already done."""

    def __init__(self, status_code: int, body: Any, *, key: str) -> None:
        super().__init__(f"replaying recorded response for Idempotency-Key {key}")
        self.status_code = status_code
        self.body = body
        self.key = key


def request_fingerprint(method: str, path: str, body: bytes) -> str:
    """A stable digest of "the same request", used to spot key reuse."""
    digest = hashlib.sha256()
    digest.update(method.upper().encode("utf-8"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


async def enforce_idempotency(
    request: Request,
    store: Annotated[DatabaseStore, Depends(get_store)],
) -> None:
    """Claim, replay or refuse an ``Idempotency-Key`` request before it runs."""
    key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not key or request.method.upper() not in MUTATING_METHODS:
        return
    if not request.url.path.startswith("/api/"):
        return
    if len(key) > _MAX_KEY_LENGTH:
        raise ApiError(
            ErrorCode.BAD_REQUEST,
            f"Idempotency-Key must be at most {_MAX_KEY_LENGTH} characters",
            details={"idempotency_key_length": len(key)},
        )

    actor = resolve_actor(request)
    body = await request.body()
    claim = store.begin_idempotent_request(
        actor,
        key=key,
        method=request.method.upper(),
        path=request.url.path,
        fingerprint=request_fingerprint(request.method, request.url.path, body),
    )
    if claim.outcome is IdempotencyOutcome.REPLAYED:
        raise IdempotentReplay(
            claim.status_code or 200,
            claim.response_body,
            key=key,
        )
    if claim.outcome is IdempotencyOutcome.FINGERPRINT_MISMATCH:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "this Idempotency-Key was already used for a different request",
            details={"idempotency_key": key},
        )
    if claim.outcome is IdempotencyOutcome.IN_PROGRESS:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_IN_PROGRESS,
            "the original request for this Idempotency-Key is still being processed",
            status_code=409,
            details={"idempotency_key": key},
        )
    request.state.idempotency_claim_id = claim.record_id
    request.state.idempotency_key = key


class _ResponseCapture:
    """Accumulates the status and body the app sends back."""

    def __init__(self) -> None:
        self.status_code: int | None = None
        self.headers: dict[str, str] = {}
        self.body = bytearray()

    def send(self, send):
        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                self.status_code = message["status"]
                self.headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in message.get("headers") or []
                }
            elif message["type"] == "http.response.body":
                self.body.extend(message.get("body", b""))
            await send(message)

        return send_wrapper

    @property
    def is_replayable(self) -> bool:
        return (
            self.status_code is not None
            and 200 <= self.status_code < 300
            and self.headers.get("content-type", "").startswith("application/json")
        )

    def json_body(self) -> Any:
        try:
            return json.loads(bytes(self.body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None


class IdempotencyRecorderMiddleware:
    """Records (or releases) the claim once the response is known.

    Only a 2xx JSON response is worth replaying. Anything else releases the claim
    so the client can fix the request and retry the same key.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        capture = _ResponseCapture()
        try:
            await self.app(scope, receive, capture.send(send))
        except Exception:
            self._settle(scope, capture, succeeded=False)
            raise
        self._settle(scope, capture, succeeded=True)

    def _settle(self, scope, capture: _ResponseCapture, *, succeeded: bool) -> None:
        state = scope.get("state") or {}
        record_id = state.get(_CLAIM_STATE_KEY)
        if not record_id:
            return
        body = capture.json_body() if (succeeded and capture.is_replayable) else None
        try:
            session = _session_factory()
        except Exception:  # noqa: BLE001  # pragma: no cover - engine wiring failure
            logger.warning("idempotency: no database session available to settle %s", record_id)
            return
        try:
            repository = IdempotencyRepository(session)
            if body is None:
                repository.release(record_id)
            else:
                repository.complete(
                    record_id,
                    status_code=capture.status_code or 200,
                    response_body=body,
                )
            session.commit()
        except Exception:  # pragma: no cover - bookkeeping is best effort
            logger.warning("idempotency: could not settle claim %s", record_id, exc_info=True)
            session.rollback()
        finally:
            session.close()


def claim_ttl_seconds() -> int:
    return max(60, int(settings.idempotency_ttl_seconds))


__all__ = [
    "IDEMPOTENCY_HEADER",
    "IdempotencyClaim",
    "IdempotencyRecorderMiddleware",
    "IdempotentReplay",
    "claim_ttl_seconds",
    "enforce_idempotency",
    "request_fingerprint",
]
