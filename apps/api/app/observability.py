"""V8.2 observability: Prometheus metrics, structured logs, request ids.

One module on purpose: the three pieces share the same plumbing. The
request-id contextvar is written by the middleware and read by the JSON log
formatter, so a log line can be tied back to the scrape/trace that produced
it; the metrics registry is shared by the API process and the worker so both
expose under one ``s2v_*`` namespace.

Tracing (OpenTelemetry) is deliberately out of scope — see
``docs/V8.2_OBSERVABILITY_PLAN.md``. The request id gives log correlation
across api and worker; a full tracer can be layered on later without moving
any of this code.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from apps.api.app.permission_policy import _request_template

if TYPE_CHECKING:
    from apps.api.app.db_store import DatabaseStore

REQUEST_ID_HEADER = "x-request-id"

_request_id: ContextVar[str | None] = ContextVar("s2v_request_id", default=None)

#: Set once per process so repeated ``configure_logging`` calls are harmless
#: (tests and ``create_app`` both run it).
_LOGGING_CONFIGURED = "_s2v_observability_configured"


def current_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


# --- metrics --------------------------------------------------------------
# A private registry rather than the prometheus_client default: the worker and
# the API are separate processes in deployment, but unit tests run both sides
# in one interpreter and an explicit registry keeps them from leaking series
# into whatever else imports the default registry.

REGISTRY = CollectorRegistry()

HTTP_REQUESTS_TOTAL = Counter(
    "s2v_http_requests_total",
    "HTTP requests handled by the API.",
    labelnames=["method", "path", "status"],
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "s2v_http_request_duration_seconds",
    "End-to-end request latency as seen by the API middleware.",
    labelnames=["method", "path"],
    registry=REGISTRY,
)
HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "s2v_http_requests_in_flight",
    "Requests currently inside the API middleware.",
    registry=REGISTRY,
)
BACKGROUND_JOBS_GAUGE = Gauge(
    "s2v_background_jobs",
    "Background jobs by status or queue, computed at scrape time.",
    labelnames=["dimension", "value"],
    registry=REGISTRY,
)
RUNS_GAUGE = Gauge(
    "s2v_runs",
    "Production runs by status, computed at scrape time.",
    labelnames=["status"],
    registry=REGISTRY,
)
WORKER_JOBS_TOTAL = Counter(
    "s2v_worker_jobs_total",
    "Background job outcomes handled by the worker process.",
    labelnames=["outcome"],
    registry=REGISTRY,
)
WORKER_JOB_DURATION = Histogram(
    "s2v_worker_job_duration_seconds",
    "Time the worker spends inside process_background_job.",
    registry=REGISTRY,
)

#: ``/metrics`` serves itself, so its own traffic is not counted — a scrape
#: loop would otherwise drown the signal it is measuring.
_METRICS_PATH = "/metrics"
_UNMATCHED = "unmatched"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Request ids, access logs and HTTP metrics for every request.

    Added outermost (after the idempotency middleware in ``create_app``), so
    the numbers include the whole stack. The path label is the *route
    template* (``/api/runs/{run_id}``), not the concrete URL: a scrape of a
    busy server must not create one time series per run id.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.access_logger = logging.getLogger("s2v.access")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
        request_id = incoming[:64] or new_request_id()
        token = _request_id.set(request_id)
        method = request.method.upper()
        started = time.perf_counter()
        HTTP_REQUESTS_IN_FLIGHT.inc()
        try:
            response = await call_next(request)
        except Exception:
            self._record(request, "500", started)
            _request_id.reset(token)
            raise
        duration = time.perf_counter() - started
        path = _route_template(request)
        if request.url.path != _METRICS_PATH:
            HTTP_REQUESTS_TOTAL.labels(method, path, str(response.status_code)).inc()
            HTTP_REQUEST_DURATION.labels(method, path).observe(duration)
        response.headers[REQUEST_ID_HEADER] = request_id
        if request.url.path != _METRICS_PATH:
            self.access_logger.info(
                "%s %s %s %.1fms request_id=%s",
                method,
                request.url.path,
                response.status_code,
                duration * 1000,
                request_id,
                extra={
                    "http_method": method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round(duration * 1000, 1),
                    "request_id": request_id,
                },
            )
        HTTP_REQUESTS_IN_FLIGHT.dec()
        _request_id.reset(token)
        return response

    def _record(self, request: Request, status: str, started: float) -> None:
        duration = time.perf_counter() - started
        method = request.method.upper()
        if request.url.path != _METRICS_PATH:
            HTTP_REQUESTS_TOTAL.labels(method, _route_template(request), status).inc()
            HTTP_REQUEST_DURATION.labels(method, _route_template(request)).observe(duration)
        HTTP_REQUESTS_IN_FLIGHT.dec()


def _route_template(request: Request) -> str:
    # ``_request_template`` is the one place that knows how to recover the
    # fully-prefixed POLICY key (FastAPI's included-router scope reports the
    # route relative to its mount prefix). A request that never matched a
    # route — a 404 — groups under one label instead of one per URL.
    return _request_template(request) or _UNMATCHED


# --- structured logging ---------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the request id when there is one.

    Extra keys a caller passes via ``extra={...}`` are carried through
    verbatim (a JSON log consumer prefers flat fields over parsing text),
    except the stdlib bookkeeping attributes that are not json-friendly.
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = _request_id.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(*, format: str = "text", level: str = "INFO") -> None:
    """Point the root logger at either the JSON or the plain-text formatter.

    Idempotent per (root logger) process: calling it twice with the same
    arguments does not stack handlers. ``json`` is an explicit opt-in
    (``LOG_FORMAT=json``) so existing text-based deployments see no change.
    """
    root = logging.getLogger()
    if format == "json":
        root.handlers.clear()
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    elif not root.handlers:
        logging.basicConfig(
            level=level.upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        setattr(root, _LOGGING_CONFIGURED, True)
        return
    root.setLevel(level.upper())
    setattr(root, _LOGGING_CONFIGURED, True)


# --- /metrics rendering ---------------------------------------------------


def render_metrics(store: DatabaseStore | None) -> bytes:
    """Serve the Prometheus exposition, enriching it with DB-side gauges.

    The scrape endpoint must always produce text: if the database is
    unavailable the process metrics above still answer, just without the
    queue/row gauges.
    """
    if store is not None:
        try:
            gauges = store.observability_gauges()
        except Exception:  # noqa: BLE001  # a scrape must answer even with a dead DB
            gauges = None
        if gauges:
            for value, count in sorted(gauges.get("jobs_by_status", {}).items()):
                BACKGROUND_JOBS_GAUGE.labels("status", value).set(count)
            for value, count in sorted(gauges.get("jobs_by_queue", {}).items()):
                BACKGROUND_JOBS_GAUGE.labels("queue", value).set(count)
            for value, count in sorted(gauges.get("runs_by_status", {}).items()):
                RUNS_GAUGE.labels(value).set(count)
    return generate_latest(REGISTRY)


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
