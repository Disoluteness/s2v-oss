"""Draining the webhook outbox (V7.3).

Delivery is at-least-once and therefore a receiver must deduplicate. What the
platform guarantees is narrower but still useful: **every event that reached
``event_logs`` and matched an active subscription gets delivered at least once or
ends up visible as ``abandoned``** — it is never silently dropped.

The HTTP exchange sits behind :class:`WebhookTransport` for the same reason the
SDKs put their transport behind an interface: the retry, backoff, signature and
abandonment logic is where the bugs live, and it should be exercisable without a
socket. The default implementation is ``urllib`` from the standard library —
the API has no HTTP client dependency and does not need one for this.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import WebhookDelivery
from apps.api.app.webhooks import (
    ATTEMPT_HEADER,
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    MAX_RESPONSE_BODY_CHARS,
    SIGNATURE_HEADER,
    retry_delay_seconds,
    sign_webhook,
    signature_timestamp,
)

logger = logging.getLogger("script2video.webhooks")

#: Sent so a receiver can tell platform traffic apart from a browser's.
USER_AGENT = "s2v-webhook/1.0"

#: Only 2xx counts as delivered. A 3xx is not followed — the endpoint's own
#: configuration decides where it lives, and chasing a redirect would let an
#: endpoint silently relocate deliveries somewhere the subscriber never
#: registered.
SUCCESS_STATUS_FLOOR = 200
SUCCESS_STATUS_CEILING = 300


class TransportError(Exception):
    """The exchange never produced an HTTP response (DNS, connect, timeout)."""


@dataclass
class WebhookResponse:
    status: int
    body: str = ""

    @property
    def ok(self) -> bool:
        return SUCCESS_STATUS_FLOOR <= self.status < SUCCESS_STATUS_CEILING


class WebhookTransport(Protocol):
    def send(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> WebhookResponse: ...


def truncate_body(text: str, *, limit: int = MAX_RESPONSE_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


class UrllibWebhookTransport:
    """The default transport: one POST, no redirects, no third-party client."""

    def send(
        self,
        *,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: float,
    ) -> WebhookResponse:
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
                return WebhookResponse(status=int(response.status), body=truncate_body(payload))
        except urllib.error.HTTPError as exc:
            # An error response is still a response: the receiver answered, it
            # just said no. That distinction is what the retry policy keys on.
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001  # pragma: no cover - a body that cannot be read
                detail = ""
            return WebhookResponse(status=int(exc.code), body=truncate_body(detail))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc


class WebhookDispatcher:
    """Turns due outbox rows into signed HTTP deliveries.

    Policy lives here rather than in the store: how long to wait between
    attempts, how many attempts is enough, and when an endpoint has failed so
    consistently that it should be switched off. The store only persists what
    this decides.
    """

    def __init__(
        self,
        *,
        transport: WebhookTransport | None = None,
        timeout: float = 10.0,
        lease_seconds: int = 300,
        backoff_base_seconds: float = 30.0,
        backoff_cap_seconds: float = 3600.0,
        disable_after_consecutive_failures: int = 20,
    ) -> None:
        self.transport = transport or UrllibWebhookTransport()
        self.timeout = max(1.0, timeout)
        self.lease_seconds = max(1, lease_seconds)
        self.backoff_base_seconds = max(1.0, backoff_base_seconds)
        self.backoff_cap_seconds = max(self.backoff_base_seconds, backoff_cap_seconds)
        self.disable_after_consecutive_failures = max(0, disable_after_consecutive_failures)

    def dispatch(
        self,
        store: DatabaseStore,
        *,
        worker_id: str,
        limit: int = 10,
    ) -> list[WebhookDelivery]:
        deliveries = store.claim_webhook_deliveries(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        settled: list[WebhookDelivery] = []
        for delivery in deliveries:
            result = self._deliver_one(store, delivery)
            if result is not None:
                settled.append(result)
        return settled

    def _deliver_one(
        self,
        store: DatabaseStore,
        delivery: WebhookDelivery,
    ) -> WebhookDelivery | None:
        subscription = store.get_webhook_subscription(delivery.subscription_id)
        if subscription is None:
            # The claim query joins on the subscription, so this is only reachable
            # if the row vanished mid-flight. Abandon rather than release, or the
            # claim would return it forever.
            logger.warning(
                "webhook delivery %s has no subscription; abandoning",
                delivery.delivery_id,
            )
            settled = store.settle_webhook_delivery(
                delivery.delivery_id,
                attempt=delivery.attempt + 1,
                signature="",
                response_status=None,
                response_body=None,
                error_message="subscription no longer exists",
                retry_delay_seconds=None,
            )
            return settled.delivery

        body = json.dumps(delivery.payload, separators=(",", ":"), default=str).encode()
        timestamp = signature_timestamp()
        signature = sign_webhook(subscription.secret, timestamp=timestamp, body=body)
        attempt = delivery.attempt + 1
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            SIGNATURE_HEADER: signature,
            DELIVERY_ID_HEADER: delivery.delivery_id,
            EVENT_ID_HEADER: delivery.event_id,
            EVENT_TYPE_HEADER: delivery.event_type,
            ATTEMPT_HEADER: str(attempt),
        }

        response: WebhookResponse | None = None
        error_message: str | None = None
        try:
            response = self.transport.send(
                url=subscription.url,
                headers=headers,
                body=body,
                timeout=self.timeout,
            )
        except TransportError as exc:
            error_message = f"transport error: {exc}"
        except Exception as exc:  # noqa: BLE001  # a broken transport must not kill the worker
            error_message = f"unexpected transport failure: {exc}"

        succeeded = response is not None and response.ok
        if succeeded:
            settled = store.settle_webhook_delivery(
                delivery.delivery_id,
                attempt=attempt,
                signature=signature,
                response_status=response.status,
                response_body=response.body,
                error_message=None,
                retry_delay_seconds=None,
            )
            logger.info(
                "webhook delivery %s -> %s %s (%s)",
                delivery.delivery_id,
                response.status,
                delivery.event_type,
                subscription.url,
            )
            return settled.delivery

        if error_message is None and response is not None:
            error_message = f"endpoint returned HTTP {response.status}"
        exhausted = attempt >= delivery.max_attempts
        delay = (
            None
            if exhausted
            else retry_delay_seconds(
                attempt,
                base=self.backoff_base_seconds,
                cap=self.backoff_cap_seconds,
            )
        )
        settled = store.settle_webhook_delivery(
            delivery.delivery_id,
            attempt=attempt,
            signature=signature,
            response_status=response.status if response else None,
            response_body=response.body if response else None,
            error_message=error_message,
            retry_delay_seconds=delay,
            disable_after_consecutive_failures=self.disable_after_consecutive_failures,
        )
        log = logger.warning if settled.abandoned else logger.info
        log(
            "webhook delivery %s attempt %s/%s failed for %s: %s",
            delivery.delivery_id,
            attempt,
            delivery.max_attempts,
            subscription.url,
            error_message,
        )
        if settled.subscription_disabled:
            logger.warning(
                "webhook subscription %s disabled after %s consecutive failures",
                subscription.subscription_id,
                self.disable_after_consecutive_failures,
            )
        return settled.delivery


def next_attempt_moment(
    attempt: int,
    *,
    base_seconds: float = 30.0,
    cap_seconds: float = 3600.0,
    now: datetime | None = None,
) -> datetime:
    """Convenience wrapper so a caller can preview the schedule."""
    moment = now or datetime.now(UTC)
    delay = retry_delay_seconds(attempt, base=base_seconds, cap=cap_seconds)
    return moment + timedelta(seconds=delay)
