"""HTTP transport.

The client talks to the API through a tiny protocol rather than through a
concrete library, for two reasons:

* the default implementation uses only the standard library, so installing this
  SDK adds no dependencies to the caller's environment;
* a caller (or a test) can swap in its own transport — a pooled ``requests``
  session, an in-process ASGI shim, a recording proxy — without the client
  knowing.

A transport does exactly one thing: perform one HTTP exchange and hand back the
status, the lower-cased headers and the raw body. It must **not** raise for a 4xx
or 5xx — those are responses, and interpreting them is the client's job. It
*signals* a genuine connection failure with :class:`TransportError`.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import NamedTuple, Protocol, runtime_checkable

from s2v.errors import TransportError


class Response(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@runtime_checkable
class Transport(Protocol):
    """One HTTP exchange."""

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:  # pragma: no cover - protocol declaration
        ...


class UrllibTransport:
    """Default transport: standard library only, one connection per call.

    Deliberately stateless. A caller that cares about connection reuse passes its
    own transport instead of asking this one to grow a pool.
    """

    def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Response(
                    int(response.status),
                    {key.lower(): value for key, value in response.headers.items()},
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            # An error status is still a response — the client decides what it
            # means. Reading the body here is what makes the error envelope
            # available to the caller.
            return Response(
                int(exc.code),
                {key.lower(): value for key, value in (exc.headers or {}).items()},
                exc.read(),
            )
        except urllib.error.URLError as exc:
            raise TransportError(
                f"{method.upper()} {url} failed: {exc.reason}",
                method=method,
                path=url,
            ) from exc
        except TimeoutError as exc:
            raise TransportError(
                f"{method.upper()} {url} timed out after {timeout:g}s",
                method=method,
                path=url,
            ) from exc
