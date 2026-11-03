"""V6.6 review attachments: evidence assets mounted on a review request.

An attachment is a *mount*, not a copy: the row points at the original asset so
the evidence is the same object the decision saw. Four things are pinned:

1. **Mounting is idempotent.** Re-adding the same asset returns the existing
   row — the unique pair ``(review_request_id, asset_id)`` is a database
   guarantee, not just an application-level lookup.
2. **A foreign asset is a 404, not a 403.** The asset must live in the same
   project as the review request; "forbidden" would confirm the asset exists
   somewhere else.
3. **The detail carries the snapshot.** ``GET /api/review-requests/{id}``
   includes attachments with the embedded ``asset`` summary, so the workspace
   renders evidence without a second call.
4. **Removal is a hard delete with an event.** The mount is a pointer, not
   history; the removal is auditable through ``review.attachment_removed``.
"""

from __future__ import annotations

import base64

import pytest
from harness import build_client
from sqlalchemy import select

from apps.api.app.actor import ActorContext
from apps.api.app.db_models import EventLogRecord
from apps.api.app.repositories import DEFAULT_TENANT_ID

OWNER = "system"
PIPELINE_KEY = "avatar_spokesperson"
PNG_BYTES = b"person image bytes"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "attachments.db")
    client.store.ensure_default_seat(
        ActorContext(
            actor_id=OWNER,
            tenant_id=DEFAULT_TENANT_ID,
            display_name="Internal System",
            source="default",
        )
    )
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _project(asgi, title: str = "P") -> dict:
    status, _, project = asgi.request("POST", "/api/projects", json_body={"title": title})
    assert status == 200, project
    return project


def _run(asgi, project_id: str, script: str = "十五秒口播") -> dict:
    status, _, run = asgi.request(
        "POST",
        f"/api/projects/{project_id}/runs",
        json_body={"raw_script": script, "pipeline_key": PIPELINE_KEY},
    )
    assert status == 200, run
    return run


def _started_run(asgi, project_id: str) -> dict:
    run = _run(asgi, project_id)
    status, _, started = asgi.request("POST", f"/api/runs/{run['run_id']}/start", json_body={})
    assert status == 200, started
    assert started["status"] == "waiting_review", started["status"]
    return started


def _open_request(asgi, run_id: str) -> dict:
    status, _, requests = asgi.request(
        "GET", f"/api/runs/{run_id}/review-requests?status=open"
    )
    assert status == 200 and len(requests) == 1, requests
    return requests[0]


def _asset(asgi, run_id: str, filename: str = "person.png") -> dict:
    status, _, asset = asgi.request(
        "POST",
        f"/api/runs/{run_id}/assets",
        json_body={
            "asset_type": "person_image",
            "filename": filename,
            "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
            "mime_type": "image/png",
        },
    )
    assert status == 200, asset
    return asset


@pytest.fixture()
def gated(asgi):
    """A project with a run parked at its first gate and one uploaded asset."""
    project = _project(asgi)
    run = _started_run(asgi, project["project_id"])
    request = _open_request(asgi, run["run_id"])
    asset = _asset(asgi, run["run_id"])
    return {
        "project": project,
        "run": run,
        "request": request,
        "asset": asset,
    }


def _mount(asgi, request_id: str, asset_id: str, **extra):
    return asgi.request(
        "POST",
        f"/api/review-requests/{request_id}/attachments",
        json_body={"asset_id": asset_id, **extra},
    )


def _error_code(body) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("code", "")
    return ""


# --------------------------------------------------------------------------- #
# mounting
# --------------------------------------------------------------------------- #


def test_mounting_an_asset_returns_it_with_the_snapshot(asgi, gated):
    status, _, attachment = _mount(asgi, gated["request"]["review_request_id"], gated["asset"]["asset_id"])
    assert status == 200, attachment
    assert attachment["attachment_id"].startswith("rvatt_")
    assert attachment["review_request_id"] == gated["request"]["review_request_id"]
    assert attachment["asset_id"] == gated["asset"]["asset_id"]
    # The snapshot rides along: the workspace renders evidence in one request.
    assert attachment["asset"]["asset_id"] == gated["asset"]["asset_id"]
    assert attachment["asset"]["asset_type"] == "person_image"


def test_remounting_the_same_asset_is_idempotent(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    asset_id = gated["asset"]["asset_id"]
    _, _, first = _mount(asgi, request_id, asset_id, note="第一版参考")
    _, _, second = _mount(asgi, request_id, asset_id, note="换了说法的备注")
    # The same row comes back — the note of the first mount is preserved.
    assert second["attachment_id"] == first["attachment_id"]
    assert second["note"] == "第一版参考"
    status, _, listed = asgi.request("GET", f"/api/review-requests/{request_id}/attachments")
    assert status == 200 and len(listed) == 1, listed


def test_the_detail_carries_the_attachments(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _, _, attachment = _mount(asgi, request_id, gated["asset"]["asset_id"], note="客户原始素材")
    status, _, detail = asgi.request("GET", f"/api/review-requests/{request_id}")
    assert status == 200, detail
    assert [item["attachment_id"] for item in detail["attachments"]] == [attachment["attachment_id"]]
    assert detail["attachments"][0]["note"] == "客户原始素材"
    assert detail["attachments"][0]["asset"]["storage_url"] == gated["asset"]["storage_url"]


def test_the_list_endpoint_matches_the_detail(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _, _, attachment = _mount(asgi, request_id, gated["asset"]["asset_id"])
    status, _, listed = asgi.request("GET", f"/api/review-requests/{request_id}/attachments")
    assert status == 200
    assert listed[0]["attachment_id"] == attachment["attachment_id"]


def test_an_asset_from_another_project_is_a_404(asgi, gated):
    other = _project(asgi, title="另一个项目")
    other_run = _run(asgi, other["project_id"])
    foreign = _asset(asgi, other_run["run_id"], filename="foreign.png")
    status, _, body = _mount(
        asgi, gated["request"]["review_request_id"], foreign["asset_id"]
    )
    assert status == 404, body
    assert _error_code(body) == "not_found"


def test_an_unknown_asset_is_a_404(asgi, gated):
    status, _, body = _mount(asgi, gated["request"]["review_request_id"], "asset_missing")
    assert status == 404
    assert _error_code(body) == "not_found"


def test_an_unknown_request_is_a_404(asgi, gated):
    status, _, body = _mount(asgi, "reviewreq_missing", gated["asset"]["asset_id"])
    assert status == 404
    assert _error_code(body) == "not_found"


# --------------------------------------------------------------------------- #
# removal
# --------------------------------------------------------------------------- #


def test_removal_takes_the_mount_down(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _, _, attachment = _mount(asgi, request_id, gated["asset"]["asset_id"])
    status, _, removed = asgi.request(
        "DELETE", f"/api/review-attachments/{attachment['attachment_id']}"
    )
    assert status == 200 and removed["attachment_id"] == attachment["attachment_id"]
    _, _, listed = asgi.request("GET", f"/api/review-requests/{request_id}/attachments")
    assert listed == []
    _, _, detail = asgi.request("GET", f"/api/review-requests/{request_id}")
    assert detail["attachments"] == []


def test_removing_twice_is_a_404(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _, _, attachment = _mount(asgi, request_id, gated["asset"]["asset_id"])
    asgi.request("DELETE", f"/api/review-attachments/{attachment['attachment_id']}")
    status, _, body = asgi.request(
        "DELETE", f"/api/review-attachments/{attachment['attachment_id']}"
    )
    assert status == 404
    assert _error_code(body) == "not_found"


def test_remounting_after_removal_creates_a_fresh_row(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    asset_id = gated["asset"]["asset_id"]
    _, _, first = _mount(asgi, request_id, asset_id)
    asgi.request("DELETE", f"/api/review-attachments/{first['attachment_id']}")
    status, _, second = _mount(asgi, request_id, asset_id)
    assert status == 200
    assert second["attachment_id"] != first["attachment_id"]


# --------------------------------------------------------------------------- #
# events and permissions
# --------------------------------------------------------------------------- #


def test_mount_and_removal_leave_a_review_event(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _, _, attachment = _mount(asgi, request_id, gated["asset"]["asset_id"])
    asgi.request("DELETE", f"/api/review-attachments/{attachment['attachment_id']}")
    events = (
        asgi.store.session.scalars(
            select(EventLogRecord)
            .where(EventLogRecord.event_type.in_(["review.attachment_added", "review.attachment_removed"]))
            .order_by(EventLogRecord.sequence)
        ).all()
    )
    assert [event.event_type for event in events] == [
        "review.attachment_added",
        "review.attachment_removed",
    ]
    assert events[0].payload["asset_id"] == gated["asset"]["asset_id"]


def test_an_unseated_identity_cannot_mount_or_read(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    headers = {"X-Actor-Id": "verify_ghost_attachments"}
    status, _, body = asgi.request(
        "POST",
        f"/api/review-requests/{request_id}/attachments",
        json_body={"asset_id": gated["asset"]["asset_id"]},
        headers=headers,
    )
    assert status == 403 and _error_code(body) == "permission_denied", body
    status, _, body = asgi.request(
        "GET", f"/api/review-requests/{request_id}/attachments", headers=headers
    )
    assert status == 403 and _error_code(body) == "permission_denied"
    # The refusal is audited like every other permission denial.
    status, _, logs = asgi.request(
        "GET", "/api/audit-logs?action=permission.denied&limit=50"
    )
    assert any(
        entry.get("actor_id") == "verify_ghost_attachments" for entry in logs
    )


def test_mounting_does_not_disturb_the_open_request(asgi, gated):
    request_id = gated["request"]["review_request_id"]
    _mount(asgi, request_id, gated["asset"]["asset_id"])
    status, _, detail = asgi.request("GET", f"/api/review-requests/{request_id}")
    assert status == 200
    assert detail["status"] == "open"
    assert detail["current_decision"] is None
    assert detail["decisions"] == []
