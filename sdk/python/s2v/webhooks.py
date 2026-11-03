"""Receiver-side verification for inbound webhook deliveries.

A subscriber's first job is to trust nothing: check that the body it received is
the body the platform signed, and that the signature is fresh. This module is
the whole checklist, dependency-free like the rest of the SDK::

    from s2v import Script2VideoClient, verify_webhook_signature, SIGNATURE_HEADER

    client = Script2VideoClient("https://studio.internal")
    subscription = client.create_webhook_subscription(
        "deploy-hook", "https://ops.internal/hooks/s2v", ["run.completed"]
    )

    # ... inside your HTTP handler, where ``request_body`` is the raw bytes:
    if not verify_webhook_signature(
        subscription.secret,
        header=request.headers.get(SIGNATURE_HEADER),
        body=request_body,
    ):
        return 403  # forged, tampered with, or replayed

Verification covers two failures at once. **Tampering**: the digest is an
HMAC-SHA256 over ``"{t}." + body`` keyed by the subscription's secret, so any
change to the body invalidates it. **Replay**: the timestamp ``t`` is inside the
signed string, so a receiver that rejects signatures older than
``tolerance_seconds`` cannot be fed a captured delivery later — an attacker
cannot re-date the body without invalidating the digest.

Deduplication is *not* part of verification. Delivery is at-least-once, so the
same event can legitimately arrive more than once; compare
``X-S2V-Delivery-Id`` (or the body's ``delivery_id``) against what you have
already processed.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

#: Header carrying ``t=<unix>,v1=<hex digest>``. One header rather than two so
#: the timestamp cannot be swapped independently of the digest it is bound to.
SIGNATURE_HEADER = "X-S2V-Signature"

#: Set on every delivery; the stable key for receiver-side deduplication.
DELIVERY_ID_HEADER = "X-S2V-Delivery-Id"

#: The originating event's id in the platform's event log.
EVENT_ID_HEADER = "X-S2V-Event-Id"

#: The published event name (same vocabulary as ``list_webhook_events``).
EVENT_TYPE_HEADER = "X-S2V-Event-Type"

#: 1-based attempt counter; a first delivery carries ``1``.
ATTEMPT_HEADER = "X-S2V-Attempt"

#: How far a signature timestamp may be from the receiver's clock. Matches the
#: platform's own default: wide enough for clock drift, narrow enough that a
#: leaked body is stale before it could be replayed.
SIGNATURE_TOLERANCE_SECONDS = 300

SIGNATURE_SCHEME = "v1"


def parse_signature_header(header: str | None) -> tuple[int, str] | None:
    """Split ``t=...,v1=...`` into ``(timestamp, digest)``, or ``None`` if malformed.

    Both parts are required. A bare digest with no timestamp has no replay
    protection, so accepting one would silently downgrade every receiver that
    forgot to check.
    """
    if not header:
        return None
    fields: dict[str, str] = {}
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            fields[key.strip()] = value.strip()
    timestamp_text = fields.get("t")
    digest = fields.get(SIGNATURE_SCHEME)
    if not timestamp_text or not digest:
        return None
    try:
        timestamp = int(timestamp_text)
    except ValueError:
        return None
    return timestamp, digest


def _signing_string(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def compute_signature(secret: str, *, timestamp: int, body: bytes) -> str:
    """The ``X-S2V-Signature`` value the platform would send for this body.

    Exposed so a receiver can build its own test fixtures without a live
    platform — the platform's signature scheme is deliberately simple enough to
    reproduce on both sides.
    """
    digest = hmac.new(secret.encode(), _signing_string(timestamp, body), hashlib.sha256)
    return f"t={timestamp},{SIGNATURE_SCHEME}={digest.hexdigest()}"


def verify_webhook_signature(
    secret: str,
    *,
    header: str | None,
    body: bytes,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Check a delivery's signature and its freshness.

    ``body`` must be the **raw request bytes**, before any JSON parsing — the
    digest is computed over exactly what was signed, and a framework that
    re-serialises the parsed body will change the bytes. ``tolerance_seconds``
    bounds how old (or how far in the future) the signature's timestamp may be;
    the default mirrors the platform's own window.
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
