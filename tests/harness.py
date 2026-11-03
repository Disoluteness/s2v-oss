"""A shared ASGI harness for the API tests.

``httpx`` is not a dependency of this project, so requests are driven straight
into the real ASGI application with a hand-built scope. That is slightly more
code than ``fastapi.testclient`` but it means the tests exercise the actual
stack — routing, dependencies, middleware — with no extra install.

It is shared because two kinds of test need it: API-level tests that assert on
status codes and bodies, and SDK tests that need a transport speaking to the
same live app. Keeping one copy means the SDK is tested against exactly what the
API tests are.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

from s2v.transport import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401  (registers the tables)
from apps.api.app import idempotency as idempotency_module
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.routes import get_store


class AsgiClient:
    """Drive the real app through ASGI, capturing status, headers and body."""

    def __init__(self, store: DatabaseStore, engine) -> None:
        self.store = store
        self.engine = engine
        app.dependency_overrides[get_store] = lambda: store
        # The recorder middleware runs outside the dependency graph, so it has to
        # be pointed at the same test database.
        self._previous_factory = idempotency_module._session_factory
        idempotency_module._session_factory = lambda: Session(engine)

    def close(self) -> None:
        app.dependency_overrides.pop(get_store, None)
        idempotency_module._session_factory = self._previous_factory

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        raw_path, _, query = path.partition("?")
        sent = [(key.lower().encode(), str(value).encode()) for key, value in (headers or {}).items()]
        if body and not any(key == b"content-type" for key, _ in sent):
            sent.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": sent,
            "server": ("testserver", 80),
            "client": ("testclient", 1),
        }
        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)

        start = next(m for m in messages if m["type"] == "http.response.start")
        payload = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        response_headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start.get("headers") or []
        }
        return int(start["status"]), response_headers, payload

    def raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        return asyncio.run(self._call(method, path, body=body, headers=headers))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        actor_id: str | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        sent = dict(headers or {})
        if actor_id:
            sent["X-Actor-Id"] = actor_id
        status, response_headers, raw = self.raw(method, path, body=body, headers=sent)
        return status, response_headers, (json.loads(raw) if raw else None)


def build_client(db_path) -> AsgiClient:
    """Create a fresh database and an ASGI client bound to it."""
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return AsgiClient(DatabaseStore(Session(engine)), engine)


class AsgiTransport:
    """An SDK transport that talks to the in-process app instead of the network.

    This is the payoff of the SDK's pluggable transport: the client's own code —
    auth headers, retries, idempotency, error decoding, the business wrappers —
    runs unchanged against the real application, with no server to start and no
    HTTP library to install.
    """

    def __init__(self, client: AsgiClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str]] = []

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        parsed = urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.calls.append((method.upper(), path))
        status, response_headers, raw = self.client.raw(
            method,
            path,
            body=body or b"",
            headers=headers,
        )
        return Response(status, response_headers, raw)
