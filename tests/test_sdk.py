"""V7.2 SDK: the client's own behaviour, tested against the real application.

The SDK is tested through its pluggable transport, which is pointed at the
in-process ASGI app (see ``tests/harness.py``). That means the wrapper methods,
the auth headers, the idempotency keys, the retry loop and the error decoding all
run exactly as they would in production, against the real routes and the real
permission policy — with no server to start and no HTTP library to install.

Retry and transport behaviour that cannot be observed through the app is driven
by a scripted fake transport instead, so a 503 followed by a 200 is a two-line
test rather than a race.
"""

from __future__ import annotations

import json

import pytest
from harness import AsgiTransport, build_client
from s2v import (
    ApiError,
    FinalVideoNotFound,
    Run,
    RunWaitTimeout,
    Script2VideoClient,
    TransportError,
)
from s2v.transport import Response

from apps.api.app.repositories import DEFAULT_TENANT_ID


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "sdk.db")
    try:
        yield client
    finally:
        client.close()


def _client(asgi, **kwargs) -> Script2VideoClient:
    kwargs.setdefault("sleep", lambda _seconds: None)
    return Script2VideoClient("http://testserver", transport=AsgiTransport(asgi), **kwargs)


def _project(client: Script2VideoClient, title: str = "SDK 项目"):
    return client.create_project(title)


# --------------------------------------------------------------------------- #
# a scripted transport, for the behaviour the app cannot show us
# --------------------------------------------------------------------------- #


class ScriptedTransport:
    """Returns queued responses; records every request it was given."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method, url, headers, body, timeout) -> Response:
        self.calls.append((method.upper(), url, dict(headers)))
        if not self._responses:
            raise AssertionError("ScriptedTransport ran out of responses")
        queued = self._responses.pop(0)
        if isinstance(queued, Exception):
            raise queued
        return queued


def _ok(payload: dict, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, headers or {}, json.dumps(payload).encode("utf-8"))


def _err(status: int, code: str, message: str = "no", headers: dict[str, str] | None = None):
    body = json.dumps({"detail": {"code": code, "message": message}}).encode("utf-8")
    return Response(status, headers or {}, body)


# --------------------------------------------------------------------------- #
# 1. auth
# --------------------------------------------------------------------------- #


def test_without_a_key_the_client_uses_the_default_identity(asgi):
    client = _client(asgi)
    me = client.me()
    assert me.actor_id == "system"
    assert me.source == "default"


def test_an_api_key_becomes_the_identity(asgi):
    admin = _client(asgi)
    created = admin.create_api_key("sdk-key")
    assert created.plaintext is not None

    scoped = _client(asgi, api_key=created.plaintext)
    me = scoped.me()
    assert me.source == "api_key"
    assert me.actor_id == "system"
    assert me.can("apikey:manage")


def test_a_scoped_key_is_refused_where_it_lacks_scope(asgi):
    admin = _client(asgi)
    created = admin.create_api_key("reader", scopes=["project:read", "run:read"])
    scoped = _client(asgi, api_key=created.plaintext)

    assert scoped.list_projects() == []
    with pytest.raises(ApiError) as caught:
        scoped.create_project("nope")
    assert caught.value.status == 403
    assert caught.value.code == "permission_denied"
    assert caught.value.retryable is False


def test_a_revoked_key_fails_with_a_typed_error(asgi):
    admin = _client(asgi)
    created = admin.create_api_key("temporary")
    scoped = _client(asgi, api_key=created.plaintext)
    assert scoped.me().source == "api_key"

    admin.revoke_api_key(created.api_key_id)
    with pytest.raises(ApiError) as caught:
        scoped.me()
    assert caught.value.status == 401
    assert caught.value.code == "api_key_invalid"


def test_the_actor_header_is_used_when_no_key_is_held(asgi):
    client = _client(asgi, actor_id="someone")
    # No seat, so the request is refused — which proves the header was sent.
    with pytest.raises(ApiError) as caught:
        client.list_projects()
    assert caught.value.status == 403


# --------------------------------------------------------------------------- #
# 2. the headline wrappers
# --------------------------------------------------------------------------- #


def test_create_run_maps_options_to_the_api_field_names(asgi):
    client = _client(asgi)
    project = _project(client)

    run = client.create_run(
        project.project_id,
        "脚本正文",
        target_platform="xiaohongshu",
        target_duration_seconds=30,
        cost_budget=12.5,
        quality_mode="provider_audio",
        brand_name="品牌",
    )
    assert run.run_id
    assert run.project_id == project.project_id

    stored = client.get_run(run.run_id)
    assert stored.pipeline_key == "avatar_spokesperson"
    payload = stored.raw["input_payload"]
    assert payload["target_platform"] == "xiaohongshu"
    assert payload["target_duration_seconds"] == 30
    assert payload["quality_mode"] == "provider_audio"


def test_upload_and_download_round_trip_the_exact_bytes(asgi):
    client = _client(asgi)
    run = client.create_run(_project(client).project_id, "脚本")
    payload = bytes(range(256)) * 4

    asset = client.upload_asset(
        run.run_id,
        asset_type="person_image",
        filename="avatar.png",
        mime_type="image/png",
        content=payload,
    )
    assert asset.asset_id
    assert asset.mime_type == "image/png"

    assert client.download_asset(asset.asset_id) == payload


def test_upload_rejects_a_call_with_no_source(asgi):
    client = _client(asgi)
    run = client.create_run(_project(client).project_id, "脚本")
    with pytest.raises(ValueError):
        client.upload_asset(run.run_id, asset_type="person_image")


def test_run_script_can_return_before_the_run_finishes(asgi):
    client = _client(asgi)
    project = _project(client)
    started = client.run_script(project.project_id, "脚本", wait=False)
    assert started.run_id
    assert started.status is not None
    assert not started.is_terminal


def test_get_final_video_says_so_when_there_is_none(asgi):
    client = _client(asgi)
    run = client.create_run(_project(client).project_id, "脚本")
    with pytest.raises(FinalVideoNotFound):
        client.get_final_video(run.run_id)


def test_retry_failed_jobs_is_a_no_op_on_a_healthy_run(asgi):
    client = _client(asgi)
    run = client.create_run(_project(client).project_id, "脚本")
    assert client.retry_failed_jobs(run.run_id) == []


def test_pagination_wrapper_exposes_items_and_total(asgi):
    client = _client(asgi)
    client.create_api_key("a")
    client.create_api_key("b")

    page = client.list_api_keys(limit=1)
    assert page.total == 2
    assert page.limit == 1
    assert len(page.items) == 1
    assert page.has_more is True
    assert page.items[0].key_prefix
    assert page.items[0].plaintext is None  # never re-exposed

    second = client.list_api_keys(limit=1, offset=1)
    assert len(second.items) == 1
    assert second.has_more is False


def test_query_parameters_reach_the_server(asgi):
    client = _client(asgi)
    run = client.create_run(_project(client).project_id, "脚本")

    assert client.list_run_artifacts(run.run_id, current_only=True) == []
    events = client.list_run_events(run.run_id, limit=5)
    assert [event["event_type"] for event in events] == ["run.created"]


def test_error_codes_are_discoverable_through_the_sdk(asgi):
    client = _client(asgi)
    codes = {entry["code"] for entry in client.error_codes()}
    assert {"not_found", "idempotency_conflict", "api_key_invalid"} <= codes


# --------------------------------------------------------------------------- #
# 3. wait_run
# --------------------------------------------------------------------------- #


def test_wait_run_returns_a_terminal_run():
    client = Script2VideoClient("http://testserver", sleep=lambda _seconds: None)
    sequence = [Run({"status": "running"}), Run({"status": "completed", "run_id": "run_1"})]
    client.get_run = lambda _run_id: sequence.pop(0)  # type: ignore[method-assign]

    settled = client.wait_run("run_1")
    assert settled.status == "completed"
    assert sequence == []


def test_wait_run_stops_at_a_human_gate():
    client = Script2VideoClient("http://testserver", sleep=lambda _seconds: None)
    seen = [Run({"status": "waiting_review"}), Run({"status": "completed"})]
    client.get_run = lambda _run_id: seen.pop(0)  # type: ignore[method-assign]

    settled = client.wait_run("run_1")
    assert settled.status == "waiting_review"
    assert len(seen) == 1  # it did not keep polling past the gate


def test_wait_run_can_be_asked_to_poll_through_a_review():
    client = Script2VideoClient("http://testserver", sleep=lambda _seconds: None)
    seen = [Run({"status": "waiting_review"}), Run({"status": "completed"})]
    client.get_run = lambda _run_id: seen.pop(0)  # type: ignore[method-assign]

    settled = client.wait_run("run_1", stop_on_review=False)
    assert settled.status == "completed"


def test_wait_run_returns_a_failed_run_instead_of_raising():
    client = Script2VideoClient("http://testserver", sleep=lambda _seconds: None)
    client.get_run = lambda _run_id: Run(  # type: ignore[method-assign]
        {"status": "failed", "run_id": _run_id}
    )
    settled = client.wait_run("run_1")
    assert settled.status == "failed"


def test_wait_run_reports_progress_and_can_raise_on_timeout():
    client = Script2VideoClient("http://testserver", sleep=lambda _seconds: None)
    client.get_run = lambda _run_id: Run({"status": "running"})  # type: ignore[method-assign]

    updates: list[str] = []
    settled = client.wait_run("run_1", timeout=0.0, on_progress=lambda r: updates.append(r.status))
    assert settled.status == "running"
    assert updates == ["running"]

    with pytest.raises(RunWaitTimeout):
        client.wait_run("run_1", timeout=0.0, raise_on_timeout=True)


# --------------------------------------------------------------------------- #
# 4. retries and idempotency
# --------------------------------------------------------------------------- #


def test_a_retryable_status_is_retried_with_the_same_idempotency_key():
    transport = ScriptedTransport(
        _err(503, "service_unavailable"),
        _err(503, "service_unavailable"),
        _ok({"project_id": "project_1"}),
    )
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None
    )
    assert client.create_project("x").project_id == "project_1"
    assert len(transport.calls) == 3

    keys = {call[2].get("Idempotency-Key") for call in transport.calls}
    assert len(keys) == 1
    assert None not in keys  # the retry reused the original key, it did not drop it


def test_a_terminal_error_is_not_retried():
    transport = ScriptedTransport(_err(403, "permission_denied"))
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None
    )
    with pytest.raises(ApiError):
        client.create_project("x")
    assert len(transport.calls) == 1


def test_retry_after_is_honoured():
    slept: list[float] = []
    transport = ScriptedTransport(
        _err(429, "rate_limited", headers={"retry-after": "2"}),
        _ok({"project_id": "project_1"}),
    )
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=slept.append
    )
    client.create_project("x")
    assert slept == [2.0]


def test_a_connection_failure_is_retried_then_raised():
    transport = ScriptedTransport(
        TransportError("refused"),
        TransportError("refused"),
        TransportError("refused"),
    )
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None, max_retries=2
    )
    with pytest.raises(TransportError):
        client.get_project("project_1")
    assert len(transport.calls) == 3  # the original plus two retries


def test_retries_can_be_disabled():
    transport = ScriptedTransport(_err(503, "service_unavailable"))
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None, max_retries=0
    )
    with pytest.raises(ApiError):
        client.create_project("x")
    assert len(transport.calls) == 1


def test_mutating_calls_carry_a_key_and_reads_do_not():
    transport = ScriptedTransport(_ok({"project_id": "p"}), _ok([]))
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None
    )
    client.create_project("x")
    client.list_projects()

    assert "Idempotency-Key" in transport.calls[0][2]
    assert "Idempotency-Key" not in transport.calls[1][2]


def test_auto_idempotency_can_be_turned_off():
    transport = ScriptedTransport(_ok({"project_id": "p"}))
    client = Script2VideoClient(
        "http://testserver",
        transport=transport,
        auto_idempotency=False,
        sleep=lambda _seconds: None,
    )
    client.create_project("x")
    assert "Idempotency-Key" not in transport.calls[0][2]


def test_an_idempotency_key_can_be_pinned():
    transport = ScriptedTransport(_ok({"project_id": "p"}))
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None
    )
    client.create_project("x", idempotency_key="job-42")
    assert transport.calls[0][2]["Idempotency-Key"] == "job-42"


def test_a_replayed_write_returns_the_original_result(asgi):
    client = _client(asgi)
    first = client.create_project("只有一次", idempotency_key="sdk-pinned-key")
    replay = client.create_project("只有一次", idempotency_key="sdk-pinned-key")
    assert replay.project_id == first.project_id
    assert len(client.list_projects()) == 1


# --------------------------------------------------------------------------- #
# 5. error decoding
# --------------------------------------------------------------------------- #


def test_a_missing_resource_becomes_a_typed_error(asgi):
    client = _client(asgi)
    with pytest.raises(ApiError) as caught:
        client.get_project("project_does_not_exist")
    assert caught.value.status == 404
    assert caught.value.code == "not_found"
    assert caught.value.detail["code"] == "not_found"
    assert caught.value.to_dict()["status_code"] == 404


def test_a_validation_failure_becomes_a_typed_error(asgi):
    client = _client(asgi)
    with pytest.raises(ApiError) as caught:
        client.create_project("")
    assert caught.value.status == 422
    assert caught.value.code == "validation_error"
    assert caught.value.detail.get("errors")


def test_an_unparseable_body_still_yields_a_typed_error():
    transport = ScriptedTransport(Response(500, {}, b"<html>boom</html>"))
    client = Script2VideoClient(
        "http://testserver", transport=transport, sleep=lambda _seconds: None, max_retries=0
    )
    with pytest.raises(ApiError) as caught:
        client.health()
    assert caught.value.status == 500
    assert caught.value.code == "internal_error"


def test_the_client_surfaces_the_server_error_catalog():
    from s2v.errors import RETRYABLE_CODES

    assert "rate_limited" in RETRYABLE_CODES
    assert "permission_denied" not in RETRYABLE_CODES


# --------------------------------------------------------------------------- #
# 6. models
# --------------------------------------------------------------------------- #


def test_run_model_reports_state():
    assert Run({"status": "completed"}).is_terminal
    assert Run({"status": "cancelled"}).is_terminal
    assert not Run({"status": "running"}).is_terminal
    assert Run({"status": "waiting_review"}).is_blocked_on_human
    assert not Run({"status": "running"}).is_blocked_on_human
    assert [stage.stage_key for stage in Run({"stages": [{"stage_key": "planning"}]}).stages] == [
        "planning"
    ]


def test_unknown_fields_stay_reachable():
    run = Run({"run_id": "run_1", "brand_new_field": 7})
    assert run["brand_new_field"] == 7
    assert run.get("missing", "fallback") == "fallback"
    assert "brand_new_field" in run
    assert run.to_dict()["brand_new_field"] == 7


def test_env_fallbacks(monkeypatch):
    monkeypatch.setenv("SCRIPT2VIDEO_BASE_URL", "https://from-env.example/")
    monkeypatch.setenv("SCRIPT2VIDEO_API_KEY", "s2v_env_key")
    client = Script2VideoClient(transport=ScriptedTransport())
    assert client.base_url == "https://from-env.example"
    assert client.api_key == "s2v_env_key"


def test_the_tenant_default_is_what_the_app_uses():
    assert DEFAULT_TENANT_ID == "tenant_default"
