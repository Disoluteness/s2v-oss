"""Outbound webhooks (V7.3).

The platform already writes every state change into ``event_logs``, and that
table is the run's own timeline: names like ``job.final_composed`` are shaped for
this codebase, not for whoever subscribes to them. A webhook therefore gets a
**published event vocabulary** of its own — six names, frozen here — and a
mapping onto the internal event types that raise them. That indirection is the
whole point: renaming an internal event (or swapping the job that composes a
video) must not silently stop deliveries to a customer's endpoint, and a
subscriber must be able to read one page of documentation and know the complete
set of things it can be told about.

Two properties matter more than anything else here:

**Authenticity.** A delivery carries ``X-S2V-Signature: t=<unix>,v1=<hex>``, an
HMAC-SHA256 over ``"{t}." + body`` keyed by the subscription's secret. The
timestamp is inside the signed string, so a receiver that rejects signatures
older than a tolerance window gets replay protection for free — an attacker
cannot re-send a captured body later, and cannot re-date it either.

**At-least-once delivery.** Deliveries are enqueued in the same transaction as
the event that caused them (see ``EventRepository.append_event``) and retried
with backoff by the worker, so a subscriber must be idempotent. Every delivery
carries a stable ``X-S2V-Delivery-Id`` for exactly that purpose.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from enum import StrEnum
from secrets import token_hex
from typing import Any
from urllib.parse import urlparse

#: Header carrying ``t=<unix>,v1=<hex digest>``. One header rather than two so
#: the timestamp cannot be swapped independently of the digest it is bound to.
SIGNATURE_HEADER = "X-S2V-Signature"
DELIVERY_ID_HEADER = "X-S2V-Delivery-Id"
EVENT_ID_HEADER = "X-S2V-Event-Id"
EVENT_TYPE_HEADER = "X-S2V-Event-Type"
ATTEMPT_HEADER = "X-S2V-Attempt"

#: How far a signature timestamp may be from the receiver's clock. Five minutes
#: is the Stripe-era default: wide enough for NTP drift and a retry queue, narrow
#: enough that a leaked body is useless by the time it could be replayed.
SIGNATURE_TOLERANCE_SECONDS = 300

#: 32 bytes of entropy, hex-encoded, so the secret fits ``String(64)`` exactly.
WEBHOOK_SECRET_BYTES = 32

#: A receiver's error page is not worth unbounded storage. Bodies are kept for
#: debugging, so the head is the useful part.
MAX_RESPONSE_BODY_CHARS = 2000

#: ``ws``/``file``/``gopher`` and friends are how an SSRF turns a webhook into a
#: request forger; only the two schemes a real endpoint uses are accepted.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class WebhookEvent(StrEnum):
    """The public, frozen set of things a subscriber can be told about.

    These names are a contract. Adding one is a non-breaking change; renaming or
    removing one breaks every subscriber that filtered on it, so the mapping in
    :data:`WEBHOOK_EVENT_SOURCES` is the place to absorb internal churn.
    """

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    STAGE_WAITING_REVIEW = "stage.waiting_review"
    JOB_FAILED = "job.failed"
    RENDER_COMPLETED = "render.completed"
    PUBLISH_COMPLETED = "publish.completed"


#: Public event -> the internal ``event_logs.event_type`` values that raise it.
#: ``run.completed`` and ``render.completed`` are deliberately distinct: a run
#: can finish without producing a video (a pipeline that stops at a document
#: deliverable), and a render can be composed by a run that then waits on a human
#: before it is finished. Collapsing them would make one of the two unusable.
WEBHOOK_EVENT_SOURCES: dict[WebhookEvent, frozenset[str]] = {
    WebhookEvent.RUN_STARTED: frozenset({"run.started"}),
    WebhookEvent.RUN_COMPLETED: frozenset({"run.completed"}),
    WebhookEvent.STAGE_WAITING_REVIEW: frozenset({"stage.waiting_review"}),
    WebhookEvent.JOB_FAILED: frozenset({"job.failed"}),
    WebhookEvent.RENDER_COMPLETED: frozenset({"job.final_composed"}),
    WebhookEvent.PUBLISH_COMPLETED: frozenset({"job.publish_package_created"}),
}

#: The reverse index the outbox consults: internal event type -> public event.
INTERNAL_EVENT_TO_WEBHOOK_EVENT: dict[str, WebhookEvent] = {
    internal: public
    for public, internals in WEBHOOK_EVENT_SOURCES.items()
    for internal in internals
}

WEBHOOK_EVENT_DESCRIPTIONS: dict[WebhookEvent, str] = {
    WebhookEvent.RUN_STARTED: "run 通过预检并开始执行（触发于 run.started）",
    WebhookEvent.RUN_COMPLETED: "run 走到终态并完成（触发于 run.completed）",
    WebhookEvent.STAGE_WAITING_REVIEW: "某个阶段进入人工审核等待，需要外部介入（触发于 stage.waiting_review）",
    WebhookEvent.JOB_FAILED: "后台任务失败（触发于 job.failed）",
    WebhookEvent.RENDER_COMPLETED: "成片合成完成，可下载（触发于 job.final_composed）",
    WebhookEvent.PUBLISH_COMPLETED: "发布包已生成（触发于 job.publish_package_created）",
}


def webhook_event_catalog() -> list[dict[str, Any]]:
    """The published catalog, so a subscriber can discover events without guessing.

    Mirrors ``GET /api/error-codes``: the platform publishes its own vocabulary
    rather than making integrators read the source.
    """
    return [
        {
            "event_type": event.value,
            "description": WEBHOOK_EVENT_DESCRIPTIONS.get(event, ""),
            "sources": sorted(WEBHOOK_EVENT_SOURCES.get(event, frozenset())),
        }
        for event in WebhookEvent
    ]


def webhook_event_for_internal(event_type: str) -> WebhookEvent | None:
    """The public name for an internal event, or ``None`` if it is not published.

    ``None`` is the common case — the event log records far more than is worth
    waking a customer's endpoint for — so the outbox uses this as its first,
    cheapest filter.
    """
    return INTERNAL_EVENT_TO_WEBHOOK_EVENT.get(event_type)


def generate_webhook_secret() -> str:
    """A fresh signing secret. Hex, so it survives a JSON round trip untouched."""
    return token_hex(WEBHOOK_SECRET_BYTES)


def _signing_string(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def sign_webhook(secret: str, *, timestamp: int, body: bytes) -> str:
    """Produce the ``X-S2V-Signature`` header value for ``body``."""
    digest = hmac.new(secret.encode(), _signing_string(timestamp, body), hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def parse_signature_header(header: str | None) -> tuple[int, str] | None:
    """Split ``t=...,v1=...`` into ``(timestamp, digest)``.

    Returns ``None`` for anything malformed. Both parts are required: a bare
    digest has no timestamp and therefore no replay protection, so accepting one
    would quietly downgrade every subscriber that forgot to check.
    """
    if not header:
        return None
    fields: dict[str, str] = {}
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            fields[key.strip()] = value.strip()
    timestamp_text = fields.get("t")
    digest = fields.get("v1")
    if not timestamp_text or not digest:
        return None
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        return None
    return timestamp, digest


def verify_webhook_signature(
    secret: str,
    *,
    header: str | None,
    body: bytes,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Check a delivery's signature and its freshness.

    The tolerance window is what makes replay protection real: a valid digest
    over a stale timestamp is rejected, and because the timestamp is part of the
    signed string it cannot be refreshed without invalidating the digest.
    """
    parsed = parse_signature_header(header)
    if parsed is None:
        return False
    timestamp, digest = parsed
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if abs(moment.timestamp() - timestamp) > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), _signing_string(timestamp, body), hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), digest)


def validate_webhook_url(url: str, *, max_length: int = 500) -> str | None:
    """Return why ``url`` is unusable, or ``None`` if it is fine.

    A message rather than a boolean because the caller turns it into a 400 whose
    ``message`` is read by whoever pasted the URL.
    """
    if len(url) > max_length:
        return f"url must be at most {max_length} characters"
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return "url must use http or https"
    if not parsed.netloc:
        return "url must include a host"
    if parsed.username or parsed.password:
        return "url must not embed credentials"
    if not urlparse(url).hostname:
        return "url must include a host"
    return None


def normalize_webhook_url(url: str) -> str:
    return url.strip()


def build_webhook_payload(
    *,
    delivery_id: str,
    event_id: str,
    event_type: str,
    run_id: str,
    project_id: str | None,
    sequence: int,
    occurred_at: datetime,
    severity: str,
    message: str | None,
    stage_key: str | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """The body a subscriber receives.

    ``data`` holds the origin event's own payload verbatim. It is a snapshot
    taken at enqueue time rather than a pointer to the event row, so a delivery
    stays reconstructable and self-describing even if it is redelivered long
    after the fact.
    """
    return {
        "delivery_id": delivery_id,
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "project_id": project_id,
        "sequence": sequence,
        "occurred_at": occurred_at.isoformat(),
        "severity": severity,
        "message": message,
        "stage_key": stage_key,
        "data": dict(data or {}),
    }


def signature_timestamp(*, now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def retry_delay_seconds(attempt: int, *, base: float = 30.0, cap: float = 3600.0) -> float:
    """Exponential backoff between delivery attempts.

    ``attempt`` is the number of attempts already made, so the first retry waits
    ``base``. Capped so a long-lived outage does not push the next attempt past
    the point where anyone still cares, and floored at ``base`` so a malformed
    attempt count cannot schedule a hot loop.
    """
    exponent = max(1, attempt) - 1
    return min(cap, base * (2**exponent))
