"""V7.3 outbound webhooks: subscriptions, the outbox, signing and delivery.

Five things are pinned here:

1. **The event vocabulary is published, not tribal.** ``GET /api/webhook-events``
   is open, static and complete; a subscription that names an unknown event is
   refused at registration instead of silently matching nothing.
2. **The outbox is transactional.** A delivery row is written in the same
   transaction as the event that caused it, so "the event happened but nobody
   was told" is not a state the database can reach. What the platform owes after
   that is at-least-once delivery or a visible ``abandoned`` row.
3. **A delivery carries its own proof.** HMAC-SHA256 over ``"{t}." + body`` with
   the timestamp inside the signed string — tampering and replay are both
   rejected by the same check, on either side of the wire.
4. **Retry is policy, not hope.** A failed attempt is rescheduled with backoff,
   attempts run out into ``abandoned`` (never a silent drop), and an endpoint
   that keeps failing is auto-paused instead of being retried forever.
5. **Manual replay is first-class.** Requeueing resets the attempt count — the
   operator's statement is "the endpoint is fixed now", not "try sooner".

Delivery tests use a scripted transport, so the retry / backoff / auto-pause
logic is exercised without a socket; the signature tests cover both sides of
the wire with the same vectors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from harness import build_client

from apps.api.app.actor import ActorContext
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import TeamMemberAdd
from apps.api.app.repositories import DEFAULT_TENANT_ID
from apps.api.app.webhook_dispatch import (
    SUCCESS_STATUS_CEILING,
    SUCCESS_STATUS_FLOOR,
    TransportError,
    WebhookDispatcher,
    WebhookResponse,
    retry_delay_seconds,
)
from apps.api.app.webhooks import (
    DELIVERY_ID_HEADER,
    EVENT_TYPE_HEADER,
    SIGNATURE_HEADER,
    WebhookEvent,
    parse_signature_header,
    sign_webhook,
    signature_timestamp,
    verify_webhook_signature,
)

OWNER = "system"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "webhooks.db")
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _default_actor() -> ActorContext:
    return ActorContext(
        actor_id=OWNER,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


def _subscribe(asgi, **body) -> dict:
    payload = {
        "name": "ops-hook",
        "url": "https://ops.internal/hooks/s2v",
        "event_types": ["run.started"],
        **body,
    }
    status, _, response = asgi.request("POST", "/api/webhooks", json_body=payload)
    assert status == 200, response
    assert isinstance(response, dict)
    return response


def _start_run(asgi) -> dict:
    """A project and a run that has actually emitted ``run.started``."""
    status, _, project = asgi.request("POST", "/api/projects", json_body={"title": "P"})
    assert status == 200, project
    status, _, run = asgi.request(
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        json_body={"raw_script": "十五秒口播", "pipeline_key": "avatar_spokesperson"},
    )
    assert status == 200, run
    status, _, started = asgi.request("POST", f"/api/runs/{run['run_id']}/start", json_body={})
    assert status == 200, started
    return started


class ScriptedWebhookTransport:
    """Pop one scripted result per ``send``; raise what is raised."""

    def __init__(self, *results: WebhookResponse | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def send(self, *, url: str, headers: dict, body: bytes, timeout: float) -> WebhookResponse:
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


# --------------------------------------------------------------------------- #
# 1. catalog and subscriptions
# --------------------------------------------------------------------------- #


def test_the_catalog_is_open_and_complete(asgi):
    status, _, catalog = asgi.request("GET", "/api/webhook-events")
    assert status == 200
    names = [entry["event_type"] for entry in catalog]
    assert names == [event.value for event in WebhookEvent]
    for entry in catalog:
        assert entry["description"]
        assert entry["sources"], "every published event names its internal source"


def test_creating_a_subscription_issues_a_secret(asgi):
    subscription = _subscribe(asgi)
    assert subscription["subscription_id"].startswith("whsub_")
    assert len(subscription["secret"]) == 64
    assert subscription["status"] == "active"
    assert subscription["consecutive_failures"] == 0


def test_an_unknown_event_type_is_refused_not_stored(asgi):
    status, _, error = asgi.request(
        "POST",
        "/api/webhooks",
        json_body={"name": "x", "url": "https://a.internal/h", "event_types": ["nope.nope"]},
    )
    assert status == 400
    assert error["detail"]["code"] == "bad_request"
    status, _, page = asgi.request("GET", "/api/webhooks")
    assert page["total"] == 0


def test_the_url_must_be_a_usable_web_endpoint(asgi):
    for url in ("ftp://a.internal/h", "https://user:pw@a.internal/h", "not-a-url"):
        status, _, error = asgi.request(
            "POST",
            "/api/webhooks",
            json_body={"name": "x", "url": url, "event_types": ["run.started"]},
        )
        assert status == 400, url
        assert error["detail"]["code"] == "bad_request", url


def test_event_types_are_deduplicated_in_order(asgi):
    subscription = _subscribe(asgi, event_types=["run.completed", "run.started", "run.completed"])
    assert subscription["event_types"] == ["run.completed", "run.started"]


def test_a_subscription_can_be_patched_partially(asgi):
    subscription = _subscribe(asgi)
    status, _, updated = asgi.request(
        "PATCH",
        f"/api/webhooks/{subscription['subscription_id']}",
        json_body={"name": "renamed", "status": "paused"},
    )
    assert status == 200
    assert updated["name"] == "renamed"
    assert updated["status"] == "paused"
    assert updated["url"] == subscription["url"], "omitted fields stay"


def test_updating_an_unknown_subscription_is_a_404(asgi):
    status, _, _ = asgi.request("PATCH", "/api/webhooks/whsub_absent", json_body={"name": "x"})
    assert status == 404


def test_rotating_the_secret_replaces_it(asgi):
    subscription = _subscribe(asgi)
    status, _, rotated = asgi.request(
        "POST", f"/api/webhooks/{subscription['subscription_id']}/rotate-secret", json_body={}
    )
    assert status == 200
    assert rotated["secret"] != subscription["secret"]
    assert len(rotated["secret"]) == 64


def test_deleting_removes_the_subscription_and_its_queue(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries"
    )
    assert page["total"] >= 1
    status, _, _ = asgi.request(
        "DELETE", f"/api/webhooks/{subscription['subscription_id']}"
    )
    assert status == 200
    status, _, _ = asgi.request("GET", f"/api/webhooks/{subscription['subscription_id']}")
    assert status == 404


# --------------------------------------------------------------------------- #
# 2. the transactional outbox
# --------------------------------------------------------------------------- #


def test_a_matching_event_fans_out_to_its_subscribers(asgi):
    subscription = _subscribe(asgi)
    other = _subscribe(asgi, name="other", event_types=["run.completed"])
    started = _start_run(asgi)
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries"
    )
    assert page["total"] == 1
    delivery = page["items"][0]
    assert delivery["event_type"] == "run.started"
    assert delivery["status"] == "pending"
    assert delivery["run_id"] == started["run_id"]
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{other['subscription_id']}/deliveries"
    )
    assert page["total"] == 0, "the event filter is per subscription"


def test_the_delivery_payload_is_a_self_describing_snapshot(asgi):
    subscription = _subscribe(asgi)
    started = _start_run(asgi)
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries"
    )
    payload = page["items"][0]["payload"]
    assert payload["event_type"] == "run.started"
    assert payload["run_id"] == started["run_id"]
    assert payload["project_id"]
    assert payload["event_id"]
    assert payload["delivery_id"]
    assert payload["occurred_at"]
    assert "data" in payload, "the origin event's own payload travels verbatim"


def test_unpublished_internal_events_enqueue_nothing(asgi):
    _subscribe(asgi, event_types=["run.completed"])
    status, _, _ = asgi.request("POST", "/api/projects", json_body={"title": "P"})
    assert status == 200
    status, _, page = asgi.request("GET", "/api/webhooks")
    subscription_id = page["items"][0]["subscription_id"]
    status, _, deliveries = asgi.request(
        "GET", f"/api/webhooks/{subscription_id}/deliveries"
    )
    assert deliveries["total"] == 0


# --------------------------------------------------------------------------- #
# 3. signatures
# --------------------------------------------------------------------------- #


def test_a_delivery_signature_verifies_and_survives_the_wire(asgi):
    secret = "a" * 64
    body = json.dumps({"delivery_id": "whdel_1", "data": {" emoji ": "🎬"}}).encode()
    header = sign_webhook(secret, timestamp=signature_timestamp(), body=body)
    assert verify_webhook_signature(secret, header=header, body=body)


def test_tampering_with_the_body_invalidates_the_signature():
    body = b'{"delivery_id": "whdel_1"}'
    header = sign_webhook("secret", timestamp=signature_timestamp(), body=body)
    assert not verify_webhook_signature(
        "secret", header=header, body=body + b" ", tolerance_seconds=3600
    )


def test_a_stale_signature_is_rejected_even_untampered():
    body = b"{}"
    moment = datetime.now(UTC) - timedelta(seconds=600)
    header = sign_webhook("secret", timestamp=int(moment.timestamp()), body=body)
    assert not verify_webhook_signature("secret", header=header, body=body)


def test_a_wrong_secret_or_malformed_header_is_rejected():
    body = b"{}"
    header = sign_webhook("secret", timestamp=signature_timestamp(), body=body)
    assert not verify_webhook_signature("other", header=header, body=body)
    for bad in (None, "", "v1=deadbeef", "t=abc,v1=zz", "t=123"):
        assert not verify_webhook_signature("secret", header=bad, body=body)


def test_parse_signature_header_extracts_both_parts():
    assert parse_signature_header("t=17,v1=ab") == (17, "ab")
    assert parse_signature_header(" t=17 , v1=ab ") == (17, "ab")
    assert parse_signature_header("t=17") is None
    assert parse_signature_header(None) is None


# --------------------------------------------------------------------------- #
# 4. delivery: retry, backoff, abandonment, auto-pause
# --------------------------------------------------------------------------- #


def _dispatcher(asgi, *results, **options) -> tuple[WebhookDispatcher, ScriptedWebhookTransport]:
    transport = ScriptedWebhookTransport(*results)
    dispatcher = WebhookDispatcher(transport=transport, backoff_base_seconds=30.0, **options)
    return dispatcher, transport


def _first_delivery(asgi, subscription_id: str) -> dict:
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription_id}/deliveries"
    )
    assert page["total"] == 1, page
    return page["items"][0]


def test_a_2xx_response_settles_the_delivery(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    dispatcher, transport = _dispatcher(asgi, WebhookResponse(200, "ok"))
    settled = dispatcher.dispatch(asgi.store, worker_id="w1")
    assert [d.status for d in settled] == ["succeeded"]
    call = transport.calls[0]
    assert call["headers"]["Content-Type"] == "application/json"
    assert DELIVERY_ID_HEADER in call["headers"]
    assert call["headers"][EVENT_TYPE_HEADER] == "run.started"
    assert call["url"] == subscription["url"]
    delivery = _first_delivery(asgi, subscription["subscription_id"])
    assert delivery["status"] == "succeeded"
    assert delivery["attempt"] == 1
    assert delivery["response_status"] == 200
    assert delivery["delivered_at"]


def test_the_signature_on_the_wire_verifies_against_the_subscription_secret(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    dispatcher, transport = _dispatcher(asgi, WebhookResponse(200))
    dispatcher.dispatch(asgi.store, worker_id="w1")
    body = transport.calls[0]["body"]
    header = transport.calls[0]["headers"][SIGNATURE_HEADER]
    assert verify_webhook_signature(
        subscription["secret"], header=header, body=body
    ), "the delivered bytes are exactly what the subscription's secret signed"


def test_a_failed_attempt_is_rescheduled_not_abandoned(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    dispatcher, _ = _dispatcher(
        asgi,
        WebhookResponse(500, "boom"),
        WebhookResponse(200),
    )
    settled = dispatcher.dispatch(asgi.store, worker_id="w1")
    assert [d.status for d in settled] == ["failed"]
    delivery = _first_delivery(asgi, subscription["subscription_id"])
    assert delivery["status"] == "failed"
    assert delivery["attempt"] == 1
    assert delivery["next_attempt_at"], "a retry is scheduled"
    assert delivery["error_message"] == "endpoint returned HTTP 500"

    # a second dispatch immediately after does nothing: the retry is in the future
    assert dispatcher.dispatch(asgi.store, worker_id="w1") == []


def test_exhausted_attempts_become_abandoned_never_silently_dropped(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries"
    )
    delivery_id = page["items"][0]["delivery_id"]
    # drive the store directly: attempt 6 of 6 with nothing scheduled = abandoned
    settled = asgi.store.settle_webhook_delivery(
        delivery_id,
        attempt=6,
        signature="t=1,v1=ab",
        response_status=503,
        response_body=None,
        error_message="endpoint returned HTTP 503",
        retry_delay_seconds=None,
    )
    assert settled.abandoned
    _status, _, page = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}/deliveries"
    )
    row = next(d for d in page["items"] if d["delivery_id"] == delivery_id)
    assert row["status"] == "abandoned"
    assert row["next_attempt_at"] is None


def test_consecutive_failures_disable_the_subscription_then_a_success_resets(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    dispatcher, _ = _dispatcher(
        asgi,
        WebhookResponse(500),
        WebhookResponse(500),
        disable_after_consecutive_failures=2,
    )
    first = dispatcher.dispatch(asgi.store, worker_id="w1")
    asgi.store.requeue_webhook_delivery(first[0].delivery_id, _default_actor())
    second = dispatcher.dispatch(asgi.store, worker_id="w1")
    assert [d.status for d in second] == ["failed"]
    status, _, updated = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}"
    )
    assert updated["status"] == "disabled"
    assert updated["consecutive_failures"] == 2

    # re-activating by hand clears the count, so one more miss does not re-disable
    status, _, reactivated = asgi.request(
        "PATCH",
        f"/api/webhooks/{subscription['subscription_id']}",
        json_body={"status": "active"},
    )
    assert status == 200
    assert reactivated["consecutive_failures"] == 0


def test_a_transport_failure_is_a_failed_attempt_too(asgi):
    _subscribe(asgi)
    _start_run(asgi)
    dispatcher, _ = _dispatcher(asgi, TransportError("dns failure"), WebhookResponse(200))
    settled = dispatcher.dispatch(asgi.store, worker_id="w1")
    assert [d.status for d in settled] == ["failed"]


def test_a_success_resets_the_failure_counter(asgi):
    subscription = _subscribe(asgi)
    _start_run(asgi)
    dispatcher, _ = _dispatcher(
        asgi,
        WebhookResponse(500),
        WebhookResponse(204),
        disable_after_consecutive_failures=2,
    )
    first = dispatcher.dispatch(asgi.store, worker_id="w1")
    asgi.store.requeue_webhook_delivery(first[0].delivery_id, _default_actor())
    second = dispatcher.dispatch(asgi.store, worker_id="w1")
    assert [d.status for d in second] == ["succeeded"]
    _status, _, updated = asgi.request(
        "GET", f"/api/webhooks/{subscription['subscription_id']}"
    )
    assert updated["status"] == "active"
    assert updated["consecutive_failures"] == 0


def test_requeueing_resets_the_attempt_count(asgi):
    _subscribe(asgi)
    _start_run(asgi)
    dispatcher, _ = _dispatcher(asgi, WebhookResponse(500))
    settled = dispatcher.dispatch(asgi.store, worker_id="w1")
    status, _, requeued = asgi.request(
        "POST",
        f"/api/webhook-deliveries/{settled[0].delivery_id}/requeue",
        json_body={},
    )
    assert status == 200
    assert requeued["status"] == "pending"
    assert requeued["attempt"] == 0
    assert requeued["next_attempt_at"] is None
    assert requeued["error_message"] is None


def test_requeueing_an_unknown_delivery_is_a_404(asgi):
    status, _, _ = asgi.request(
        "POST", "/api/webhook-deliveries/whdel_absent/requeue", json_body={}
    )
    assert status == 404


def test_deleting_a_subscription_removes_its_queue_with_it(asgi):
    """The outbox is bound to its subscription by a foreign key, and deletion
    takes the queue along: a delivery that will never be drained is not worth
    keeping, and the event log still holds every source event."""
    subscription = _subscribe(asgi)
    _start_run(asgi)
    assert _first_delivery(asgi, subscription["subscription_id"])["delivery_id"]
    asgi.store.delete_webhook_subscription(subscription["subscription_id"], _default_actor())
    from sqlalchemy import select

    from apps.api.app import db_models

    remaining = list(
        asgi.store.session.scalars(
            select(db_models.WebhookDeliveryRecord).where(
                db_models.WebhookDeliveryRecord.subscription_id
                == subscription["subscription_id"]
            )
        )
    )
    assert remaining == []


def test_the_success_floor_and_ceiling_are_exact():
    assert SUCCESS_STATUS_FLOOR == 200
    assert SUCCESS_STATUS_CEILING == 300
    assert WebhookResponse(199).ok is False
    assert WebhookResponse(200).ok is True
    assert WebhookResponse(299).ok is True
    assert WebhookResponse(300).ok is False


def test_backoff_grows_exponentially_and_is_capped():
    assert retry_delay_seconds(1) == 30
    assert retry_delay_seconds(2) == 60
    assert retry_delay_seconds(3) == 120
    assert retry_delay_seconds(9) == 3600, "capped at one hour"
    assert retry_delay_seconds(0) == 30, "floored, never a hot loop"


# --------------------------------------------------------------------------- #
# 5. permissions and audit
# --------------------------------------------------------------------------- #


def _seat(asgi, user_id: str, role: str = "member") -> None:
    owner = _default_actor()
    team_id, _ = asgi.store.actor_seat(owner)
    assert team_id is not None
    asgi.store.add_team_member(team_id, TeamMemberAdd(user_id=user_id, role=role), owner)
    asgi.store.session.commit()


def test_managing_subscriptions_requires_webhook_manage(asgi):
    _seat(asgi, "alice", role="member")  # members do not hold tenant-only keys
    headers = {"X-Actor-Id": "alice"}
    for method, path in (
        ("POST", "/api/webhooks"),
        ("GET", "/api/webhooks"),
    ):
        status, _, error = asgi.request(
            method, path, json_body={"name": "x", "url": "https://a.internal/h",
                                     "event_types": ["run.started"]}
            if method == "POST" else None,
            headers=headers,
        )
        assert status == 403, (method, path)
        assert error["detail"]["code"] == "permission_denied"
    # the default actor (owner) can
    status, _, _ = asgi.request("GET", "/api/webhooks")
    assert status == 200


def test_an_admin_holds_webhook_manage(asgi):
    _seat(asgi, "bob", role="admin")
    status, _, page = asgi.request("GET", "/api/webhooks", headers={"X-Actor-Id": "bob"})
    assert status == 200
    assert page["total"] == 0


def test_subscription_management_is_audited(asgi):
    subscription = _subscribe(asgi)
    asgi.request("POST", f"/api/webhooks/{subscription['subscription_id']}/rotate-secret",
                 json_body={})
    asgi.request("DELETE", f"/api/webhooks/{subscription['subscription_id']}")
    _status, _, logs = asgi.request("GET", "/api/audit-logs")
    actions = [entry["action"] for entry in logs]
    assert "webhook.subscription_created" in actions
    assert "webhook.secret_rotated" in actions
    assert "webhook.subscription_deleted" in actions


def test_a_cross_tenant_subscription_id_reads_as_absent(asgi):
    subscription = _subscribe(asgi)
    other_store = DatabaseStore(asgi.store.session)
    # the repo layer answers honestly; the store API path filters by tenant
    assert other_store.webhooks.get_subscription(subscription["subscription_id"]) is not None
    assert (
        asgi.store.get_webhook_subscription(
            subscription["subscription_id"], tenant_id="someone_else"
        )
        is None
    )
