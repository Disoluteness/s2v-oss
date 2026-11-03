"""V7.5 CLI: the ``s2v`` command, tested against the real application.

The CLI is the SDK's shell, so it is tested the same way the SDK is: the
injected client factory hands back a :class:`Script2VideoClient` whose transport
is the in-process ASGI app (``tests/harness.py``), and ``main()`` runs exactly
what a terminal would run — argparse parsing, config resolution, output
formatting and exit codes — with no network and no real config file.

Behaviour that does not need the app (wait exit codes for a failed or parked
run) is driven by a tiny fake client, because the in-process app has no worker
to move a run anywhere.
"""

from __future__ import annotations

import json
import stat
from typing import Any

import pytest
from harness import AsgiTransport, build_client
from s2v import ApiError, FinalVideoNotFound, Script2VideoClient, TransportError
from s2v.models import Model

from apps.api.app.actor import ActorContext
from apps.api.app.repositories import DEFAULT_TENANT_ID

OWNER = "system"
PIPELINE_KEY = "avatar_spokesperson"


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "cli.db")
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


@pytest.fixture()
def sdk(asgi) -> Script2VideoClient:
    return Script2VideoClient(
        "http://testserver",
        transport=AsgiTransport(asgi),
        sleep=lambda _seconds: None,
    )


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Point the CLI at the test deployment through the environment tier."""
    monkeypatch.setenv("S2V_BASE_URL", "http://testserver")
    monkeypatch.setenv("S2V_API_KEY", "s2v_test")
    monkeypatch.setenv("S2V_CONFIG", str(tmp_path / "cfg.json"))
    return monkeypatch


def make_factory(client: Any):
    return lambda base_url, api_key: client


def run_cli(env, argv, client, capsys):
    from s2v.cli import main

    code = main(argv, client_factory=make_factory(client))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def parse_json(out: str) -> Any:
    return json.loads(out)


def _project(sdk: Script2VideoClient, title: str = "CLI 项目"):
    return sdk.create_project(title)


def _run(sdk: Script2VideoClient, project_id: str):
    return sdk.create_run(project_id, "十五秒口播脚本", pipeline_key=PIPELINE_KEY)


# --------------------------------------------------------------------------- #
# 1. configuration
# --------------------------------------------------------------------------- #


def test_flags_beat_the_environment_beat_the_file(env, capsys, asgi):
    env.setenv("S2V_CONFIG", "/nonexistent/cli-config.json")
    seen: dict[str, Any] = {}

    def factory(base_url, api_key):
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return sdk_client_for(asgi)

    from s2v.cli import main

    main(
        ["--base-url", "http://flag", "--api-key", "s2v_flag", "whoami"],
        client_factory=factory,
    )
    assert seen == {"base_url": "http://flag", "api_key": "s2v_flag"}

    seen.clear()
    main(["whoami"], client_factory=factory)
    assert seen == {"base_url": "http://testserver", "api_key": "s2v_test"}


def sdk_client_for(asgi) -> Script2VideoClient:
    return Script2VideoClient(
        "http://testserver",
        transport=AsgiTransport(asgi),
        sleep=lambda _seconds: None,
    )


def test_missing_configuration_is_a_clean_error_not_a_traceback(env, capsys, asgi):
    env.delenv("S2V_BASE_URL")
    env.delenv("S2V_API_KEY")
    from s2v.cli import EXIT_ERROR, main

    code = main(["whoami"], client_factory=make_factory(sdk_client_for(asgi)))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "missing configuration: base-url, api-key" in captured.err
    assert "pass --base-url" in captured.err
    assert captured.out == ""


def test_config_set_writes_a_0600_file_and_show_masks_the_key(env, capsys):
    from s2v.cli import EXIT_OK, config_path, main

    assert main(["config", "set", "base-url", "https://studio.internal"]) == EXIT_OK
    assert main(["config", "set", "api-key", "s2v_secret_value_123456"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "saved" in captured.out

    path = config_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {
        "base_url": "https://studio.internal",
        "api_key": "s2v_secret_value_123456",
    }

    assert main(["config", "show"]) == EXIT_OK
    captured = capsys.readouterr()
    shown = json.loads(captured.out)
    assert shown["api_key"] == "s2v_secret…" or "…" in shown["api_key"]
    assert "s2v_secret_value_123456" not in captured.out


def test_config_path_honours_the_override(env, capsys, tmp_path):
    from s2v.cli import EXIT_OK, main

    target = tmp_path / "elsewhere.json"
    env.setenv("S2V_CONFIG", str(target))
    assert main(["config", "path"]) == EXIT_OK
    assert capsys.readouterr().out.strip() == str(target)


def test_the_config_file_is_the_last_resort(env, capsys, asgi, tmp_path):
    env.delenv("S2V_BASE_URL")
    env.delenv("S2V_API_KEY")
    config = tmp_path / "file-tier.json"
    config.write_text(
        json.dumps({"base_url": "http://file", "api_key": "s2v_file"}),
        encoding="utf-8",
    )
    env.setenv("S2V_CONFIG", str(config))
    seen: dict[str, Any] = {}

    def factory(base_url, api_key):
        seen.update(base_url=base_url, api_key=api_key)
        return sdk_client_for(asgi)

    from s2v.cli import main

    main(["whoami"], client_factory=factory)
    assert seen == {"base_url": "http://file", "api_key": "s2v_file"}


# --------------------------------------------------------------------------- #
# 2. identity and meta
# --------------------------------------------------------------------------- #


def test_whoami_reports_the_resolved_identity(env, capsys, asgi):
    code, out, _err = run_cli(env, ["whoami", "--json"], sdk_client_for(asgi), capsys)
    assert code == 0
    payload = parse_json(out)
    assert payload["actor_id"] == OWNER
    assert payload["source"] == "default"


def test_health_reaches_the_app(env, capsys, asgi):
    code, out, _err = run_cli(env, ["health", "--json"], sdk_client_for(asgi), capsys)
    assert code == 0
    assert parse_json(out)["ok"] is True


def test_an_api_error_prints_code_and_message_and_exits_one(env, capsys):
    from s2v.cli import EXIT_ERROR, main

    class Failing:
        def me(self):
            raise ApiError(403, "permission_denied", "no seat for this identity")

    code = main(["whoami"], client_factory=make_factory(Failing()))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "permission_denied" in captured.err
    assert "no seat for this identity" in captured.err


def test_a_transport_error_suggests_checking_the_base_url(env, capsys):
    from s2v.cli import EXIT_ERROR, main

    class Unreachable:
        def me(self):
            raise TransportError("connection refused")

    code = main(["whoami"], client_factory=make_factory(Unreachable()))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "connection refused" in captured.err
    assert "--base-url" in captured.err


# --------------------------------------------------------------------------- #
# 3. projects and runs
# --------------------------------------------------------------------------- #


def test_projects_create_and_runs_crud_round_trip(env, capsys, asgi):
    client = sdk_client_for(asgi)
    code, out, _ = run_cli(
        env, ["projects", "create", "CLI 项目", "--json"], client, capsys
    )
    assert code == 0
    project_id = parse_json(out)["project_id"]

    code, out, _ = run_cli(
        env,
        ["runs", "create", project_id, "十五秒口播脚本", "--pipeline-key", PIPELINE_KEY, "--json"],
        client,
        capsys,
    )
    assert code == 0
    run_id = parse_json(out)["run_id"]

    code, out, _ = run_cli(env, ["runs", "get", run_id, "--json"], client, capsys)
    assert code == 0
    assert parse_json(out)["run_id"] == run_id

    code, out, _ = run_cli(
        env, ["runs", "list", "--project", project_id, "--json"], client, capsys
    )
    assert code == 0
    assert [row["run_id"] for row in parse_json(out)] == [run_id]


def test_human_output_is_a_table_not_json(env, capsys, asgi):
    client = sdk_client_for(asgi)
    code, out, _ = run_cli(env, ["projects", "create", "表格项目"], client, capsys)
    assert code == 0
    assert "project_id" in out and "title" in out
    assert not out.strip().startswith("{")


def test_a_script_can_come_from_a_file(env, capsys, asgi, tmp_path):
    client = sdk_client_for(asgi)
    _project = client.create_project("脚本文件项目")
    script = tmp_path / "script.txt"
    script.write_text("来自文件的脚本", encoding="utf-8")
    code, out, _ = run_cli(
        env,
        ["runs", "create", _project.project_id, "--script-file", str(script), "--json"],
        client,
        capsys,
    )
    assert code == 0
    raw = client.get_run(parse_json(out)["run_id"])
    assert raw.get("input_payload", {}).get("raw_script") == "来自文件的脚本"


# --------------------------------------------------------------------------- #
# 4. assets
# --------------------------------------------------------------------------- #


def test_assets_upload_and_download_survive_a_byte_round_trip(
    env, capsys, asgi, tmp_path
):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    source = tmp_path / "persona.png"
    payload = b"\x89PNG-bytes-for-the-cli-test"
    source.write_bytes(payload)

    code, out, _ = run_cli(
        env,
        ["assets", "upload", run.run_id, str(source), "--type", "person_image", "--json"],
        client,
        capsys,
    )
    assert code == 0
    asset_id = parse_json(out)["asset_id"]

    code, out, _ = run_cli(env, ["assets", "list", "--run", run.run_id, "--json"], client, capsys)
    assert code == 0
    assert [row["asset_id"] for row in parse_json(out)] == [asset_id]

    target = tmp_path / "downloaded.png"
    code, out, _ = run_cli(
        env, ["assets", "download", asset_id, "-o", str(target)], client, capsys
    )
    assert code == 0
    assert target.read_bytes() == payload
    assert f"wrote {len(payload)} bytes" in out


# --------------------------------------------------------------------------- #
# 5. jobs and video
# --------------------------------------------------------------------------- #


def test_jobs_list_on_a_fresh_run_is_an_empty_table(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    code, out, _ = run_cli(env, ["jobs", "list", "--run", run.run_id], client, capsys)
    assert code == 0
    assert "(no rows)" in out


def test_video_get_without_a_render_exits_one_with_a_hint(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    from s2v.cli import EXIT_ERROR

    code, _out, err = run_cli(env, ["video", "get", run.run_id], client, capsys)
    assert code == EXIT_ERROR
    assert "no final video" in err
    assert "hint:" in err


def test_video_download_of_a_rendered_asset_writes_the_file(
    env, capsys, tmp_path
):
    from s2v.cli import EXIT_OK

    class WithRender:
        def get_final_video(self, run_id):
            return Model(
                {"asset_id": "asset_1", "asset_type": "rendered_video", "mime_type": "video/mp4"}
            )

        def download_asset(self, asset_id):
            assert asset_id == "asset_1"
            return b"mp4-bytes"

    target = tmp_path / "final.mp4"
    code, out, _ = run_cli(
        env,
        ["video", "download", "r1", "-o", str(target)],
        WithRender(),
        capsys,
    )
    assert code == EXIT_OK
    assert target.read_bytes() == b"mp4-bytes"
    assert "asset_1" in out


def test_a_missing_final_video_raises_a_readable_error(env, capsys):
    from s2v.cli import EXIT_ERROR, main

    class Empty:
        def get_final_video(self, run_id):
            raise FinalVideoNotFound("run r1 has no final video yet")

    code = main(["video", "get", "r1"], client_factory=make_factory(Empty()))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "no final video" in captured.err


# --------------------------------------------------------------------------- #
# 6. wait
# --------------------------------------------------------------------------- #


def test_wait_times_out_with_exit_code_three(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    from s2v.cli import EXIT_WAIT_TIMEOUT

    code, _out, err = run_cli(
        env,
        ["runs", "wait", run.run_id, "--timeout", "0", "--poll-interval", "0"],
        client,
        capsys,
    )
    assert code == EXIT_WAIT_TIMEOUT
    assert "did not settle" in err


class FakeWaitClient:
    """A run that has already settled — the app has no worker to get there."""

    def __init__(self, status: str) -> None:
        self.status = status

    def wait_run(self, run_id, **_kwargs):
        return Model({"run_id": run_id, "status": self.status})


def test_wait_on_a_failed_run_exits_four(env, capsys):
    from s2v.cli import EXIT_RUN_FAILED, main

    code = main(
        ["runs", "wait", "r1", "--json"], client_factory=make_factory(FakeWaitClient("failed"))
    )
    captured = capsys.readouterr()
    assert code == EXIT_RUN_FAILED
    assert "failed" in captured.err


def test_wait_on_a_human_gate_is_a_stop_not_a_failure(env, capsys):
    from s2v.cli import EXIT_OK, main

    code = main(
        ["runs", "wait", "r1", "--json"],
        client_factory=make_factory(FakeWaitClient("waiting_review")),
    )
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "waiting_review" in captured.err
    assert "person" in captured.err


# --------------------------------------------------------------------------- #
# 7. review
# --------------------------------------------------------------------------- #


def test_review_submit_without_an_open_request_is_a_clean_error(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    from s2v.cli import EXIT_ERROR

    code, _out, err = run_cli(
        env, ["review", "submit", run.run_id, "approve"], client, capsys
    )
    assert code == EXIT_ERROR
    assert "no open review request" in err


def test_review_submit_resolves_the_single_open_request(env, capsys):
    from s2v.cli import EXIT_OK

    submitted: list[tuple[str, str, str | None]] = []

    class Reviewable:
        def request(self, method, path, **_kwargs):
            assert (method, path) == ("GET", "/api/runs/r1/review-requests")
            return [
                {"review_request_id": "rr_open", "status": "open"},
                {"review_request_id": "rr_done", "status": "approved"},
            ]

        def submit_review_decision(self, request_id, decision, *, comment=None, **_kwargs):
            submitted.append((request_id, decision, comment))
            return {"review_request_id": request_id, "status": "approved"}

    code, _out, _err = run_cli(
        env,
        ["review", "submit", "r1", "approve", "--note", "looks good"],
        Reviewable(),
        capsys,
    )
    assert code == EXIT_OK
    assert submitted == [("rr_open", "approve", "looks good")]


def test_review_submit_with_two_open_requests_asks_for_explicit_pick(env, capsys):
    from s2v.cli import EXIT_ERROR, main

    class Ambiguous:
        def request(self, method, path, **_kwargs):
            return [
                {"review_request_id": "rr_a", "status": "open"},
                {"review_request_id": "rr_b", "status": "open"},
            ]

    code = main(["review", "submit", "r1", "approve"], client_factory=make_factory(Ambiguous()))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "--request" in captured.err


# --------------------------------------------------------------------------- #
# 8. quick
# --------------------------------------------------------------------------- #


class QuickClient:
    """One-stop path with the polling already settled."""

    def __init__(self, status: str = "completed") -> None:
        self.status = status
        self.downloads: list[str] = []

    def create_project(self, title):
        return Model({"project_id": "proj_1", "title": title})

    def run_script(self, project_id, script, **_kwargs):
        return Model({"run_id": "run_1", "status": self.status, "project_id": project_id})

    def get_final_video(self, run_id):
        if self.status == "failed":
            raise FinalVideoNotFound("no video")
        return Model({"asset_id": "asset_9", "asset_type": "rendered_video"})

    def download_asset(self, asset_id):
        self.downloads.append(asset_id)
        return b"final-video-bytes"


def test_quick_creates_a_project_waits_and_downloads(env, capsys, tmp_path):
    from s2v.cli import EXIT_OK

    target = tmp_path / "quick.mp4"
    client = QuickClient()
    code, _out, err = run_cli(
        env,
        ["quick", "脚本", "--download", str(target), "--json"],
        client,
        capsys,
    )
    assert code == EXIT_OK
    assert "created project proj_1" in err
    assert target.read_bytes() == b"final-video-bytes"


def test_quick_on_a_failed_run_exits_four(env, capsys):
    from s2v.cli import EXIT_RUN_FAILED, main

    code = main(
        ["quick", "脚本", "--json"], client_factory=make_factory(QuickClient("failed"))
    )
    captured = capsys.readouterr()
    assert code == EXIT_RUN_FAILED
    assert "failed" in captured.err


def test_quick_reports_a_wait_timeout_as_three(env, capsys):
    from s2v.cli import EXIT_WAIT_TIMEOUT, RunWaitTimeout, main

    class Slow:
        def create_project(self, title):
            return Model({"project_id": "proj_1"})

        def run_script(self, project_id, script, **kwargs):
            raise RunWaitTimeout(Model({"run_id": "run_1"}), timeout=kwargs["timeout"])

    code = main(
        ["quick", "脚本", "--timeout", "0.5", "--json"],
        client_factory=make_factory(Slow()),
    )
    captured = capsys.readouterr()
    assert code == EXIT_WAIT_TIMEOUT
    assert "did not settle" in captured.err


# --------------------------------------------------------------------------- #
# 9. events
# --------------------------------------------------------------------------- #


def test_events_lists_the_run_log(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    code, out, _ = run_cli(env, ["events", "--run", run.run_id, "--json"], client, capsys)
    assert code == 0
    assert isinstance(parse_json(out), list)


def test_events_human_output_prefers_the_known_columns(env, capsys, asgi):
    client = sdk_client_for(asgi)
    run = _run(client, _project(client).project_id)
    code, out, _ = run_cli(env, ["events", "--run", run.run_id], client, capsys)
    assert code == 0
    # Either real rows or the "(no rows)" placeholder — never a traceback.
    assert "(no rows)" in out or "created_at" in out or "event" in out


# --------------------------------------------------------------------------- #
# 10. usage
# --------------------------------------------------------------------------- #


def test_a_file_that_does_not_exist_is_a_clean_error(env, capsys, asgi, tmp_path):
    client = sdk_client_for(asgi)
    from s2v.cli import EXIT_ERROR

    code, _out, err = run_cli(
        env,
        ["runs", "create", "proj_1", "--script-file", str(tmp_path / "missing.txt")],
        client,
        capsys,
    )
    assert code == EXIT_ERROR
    assert "file not found" in err


def test_os_errors_surface_as_messages(env, capsys):
    from s2v.cli import EXIT_ERROR, main

    class Explodes:
        def me(self):
            raise OSError("disk on fire")

    code = main(["whoami"], client_factory=make_factory(Explodes()))
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    assert "disk on fire" in captured.err
