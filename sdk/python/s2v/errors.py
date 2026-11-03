"""Exceptions raised by the Script2Video Studio client.

The server answers every failure with one envelope (V7.1)::

    {"detail": {"code": "...", "message": "...", ...details}}

:class:`ApiError` is that envelope as an exception. ``code`` is the stable field
to branch on — it comes from the catalog the server publishes at
``GET /api/error-codes`` — while ``message`` is for humans and may change.
"""

from __future__ import annotations

from typing import Any


class Script2VideoError(Exception):
    """Base class for everything this client raises."""


#: Codes where retrying the *identical* request can succeed, mirroring the
#: server's published catalog. The client uses this to decide on auto-retry, and
#: it is exposed so callers can write the same policy themselves.
RETRYABLE_CODES: frozenset[str] = frozenset(
    {
        "rate_limited",
        "internal_error",
        "bad_gateway",
        "service_unavailable",
        "idempotency_in_progress",
    }
)


class TransportError(Script2VideoError):
    """The request never produced an HTTP response (DNS, refused, timeout).

    Kept distinct from :class:`ApiError` on purpose: "the server said no" and
    "the server was never reached" need different handling, and conflating them
    makes a retry policy impossible to reason about.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.method = method
        self.path = path


class ApiError(Script2VideoError):
    """A structured failure returned by the API."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        method: str | None = None,
        path: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(f"HTTP {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})
        self.method = method
        self.path = path
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable

    @property
    def status(self) -> int:
        """Alias for ``status_code``; reads better at a glance."""
        return self.status_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "code": self.code,
            "message": self.message,
            **self.detail,
        }


class FinalVideoNotFound(Script2VideoError):
    """The run has no render output yet — usually because it is not finished."""


class RunWaitTimeout(Script2VideoError):
    """``wait_run`` gave up before the run reached a stopping state."""

    def __init__(self, run: Any, *, timeout: float) -> None:
        status = getattr(run, "status", None)
        super().__init__(f"run did not settle within {timeout:g}s (last status: {status})")
        self.run = run
        self.timeout = timeout
