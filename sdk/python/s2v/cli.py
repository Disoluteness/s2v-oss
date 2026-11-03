"""The ``s2v`` command line — the SDK's shell for local debugging and ops.

The CLI is a thin wrapper over :class:`~s2v.Script2VideoClient`: authentication,
retries, idempotency keys and error decoding all live in the client, so a fix
there fixes the CLI too. It adds exactly three things the library does not have:

* **Configuration** — ``--base-url``/``--api-key`` flags win over the
  ``S2V_BASE_URL``/``S2V_API_KEY`` environment variables, which win over a
  small JSON file (``s2v config path``). What was said last wins.
* **Human output** — key/value for one object, compact rows for a list, and a
  ``--json`` flag that prints the raw response for scripts that parse it.
* **Exit codes worth scripting against** — ``0`` success, ``1`` failure,
  ``2`` usage, ``3`` a wait that timed out, ``4`` a run that settled on
  ``failed``. A run parked on ``waiting_review`` is a *stop*, not a failure:
  a person is now the blocking resource, and the process exits ``0``.

No third-party dependency is introduced; ``argparse`` keeps the
"pip install s2v-client and go" story intact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from s2v.client import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WAIT_TIMEOUT,
    Script2VideoClient,
)
from s2v.errors import (
    ApiError,
    FinalVideoNotFound,
    RunWaitTimeout,
    TransportError,
)
from s2v.models import Model

Json = dict[str, Any]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_RUN_FAILED = 4

#: Where ``config set`` persists. Override with ``S2V_CONFIG`` for tests and
#: for pointing one shell at a second deployment.
CONFIG_DIR_NAME = "s2v"
CONFIG_FILE_NAME = "config.json"

DECISIONS = (
    "approve",
    "approve_with_changes",
    "request_changes",
    "reject",
    "reopen",
    "cancel",
)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def config_path() -> Path:
    override = os.environ.get("S2V_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_config() -> Json:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(values: Json) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_config()
    merged.update(values)
    text = json.dumps(merged, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path


def resolve_credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """Flags beat the environment; the environment beats the config file."""
    stored = load_config()
    base_url = (
        getattr(args, "base_url", None)
        or os.environ.get("S2V_BASE_URL")
        or stored.get("base_url")
    )
    api_key = (
        getattr(args, "api_key", None)
        or os.environ.get("S2V_API_KEY")
        or stored.get("api_key")
    )
    if isinstance(base_url, str):
        base_url = base_url.strip() or None
    if isinstance(api_key, str):
        api_key = api_key.strip() or None
    return base_url, api_key


def default_client_factory(base_url: str, api_key: str | None) -> Script2VideoClient:
    return Script2VideoClient(base_url, api_key=api_key)


def build_client(
    args: argparse.Namespace,
    client_factory: Callable[[str, str | None], Any] | None = None,
) -> Any:
    base_url, api_key = resolve_credentials(args)
    missing = [
        name for name, value in (("base-url", base_url), ("api-key", api_key)) if not value
    ]
    if missing:
        raise MissingConfiguration(missing)
    assert base_url is not None
    factory = client_factory or default_client_factory
    return factory(base_url, api_key)


class MissingConfiguration(Exception):
    """Neither flags, environment nor config file produced the credentials."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        joined = ", ".join(missing)
        super().__init__(
            f"missing configuration: {joined} — pass --{missing[0]}, "
            f"export S2V_{missing[0].upper().replace('-', '_')}, or run: "
            f"s2v config set {missing[0]} <value>"
        )


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


def emit_json(value: Any) -> None:
    if isinstance(value, Model):
        value = value.to_dict()
    elif isinstance(value, list) and value and isinstance(value[0], Model):
        value = [item.to_dict() for item in value]
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def print_dict(data: Json) -> None:
    for key in sorted(data):
        value = data[key]
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = value
        print(f"{key}: {rendered}")


def print_rows(rows: Sequence[Json], columns: Sequence[str]) -> None:
    """A compact fixed-width table; missing columns render as ``-``."""
    if not rows:
        print("(no rows)")
        return
    cells = [[_cell(row.get(column)) for column in columns] for row in rows]
    widths = [
        max([len(column)] + [len(row[index]) for row in cells])
        for index, column in enumerate(columns)
    ]
    print("  ".join(column.ljust(widths[i]) for i, column in enumerate(columns)))
    print("  ".join("-" * width for width in widths))
    for row in cells:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def emit(args: argparse.Namespace, value: Any, *, columns: Sequence[str]) -> None:
    if getattr(args, "json_output", False):
        emit_json(value)
        return
    if isinstance(value, list):
        items = [item.to_dict() if isinstance(item, Model) else item for item in value]
        print_rows(items, columns)  # "(no rows)" when the list is empty
        return
    if isinstance(value, Model):
        print_dict(value.to_dict())
        return
    if isinstance(value, dict):
        print_dict(value)
        return
    print(value)


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def error_exit(message: str, *, hint: str | None = None) -> int:
    eprint(f"error: {message}")
    if hint:
        eprint(f"hint: {hint}")
    return EXIT_ERROR


def _read_script(args: argparse.Namespace) -> str:
    if getattr(args, "script_file", None):
        return Path(args.script_file).read_text(encoding="utf-8")
    return args.script


def _progress_printer():
    """Report status changes on stderr without spamming every poll."""
    state = {"last": None}

    def on_progress(run: Model) -> None:
        status = run.get("status")
        if status != state["last"]:
            eprint(f"run status: {status}")
            state["last"] = status

    return on_progress


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_whoami(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.me(), columns=("actor_id", "source", "team_role"))
    return EXIT_OK


def cmd_health(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.health(), columns=("status", "version"))
    return EXIT_OK


def cmd_config_path(_args: argparse.Namespace, _client: Any) -> int:
    print(config_path())
    return EXIT_OK


def cmd_config_show(_args: argparse.Namespace, _client: Any) -> int:
    stored = load_config()
    if "api_key" in stored and isinstance(stored["api_key"], str):
        key = stored["api_key"]
        stored["api_key"] = key[:10] + "…" if len(key) > 10 else key
    print(json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_config_set(args: argparse.Namespace, _client: Any) -> int:
    key = args.key.replace("-", "_")
    if key not in ("base_url", "api_key"):
        return error_exit(f"unknown config key: {args.key}", hint="use base-url or api-key")
    path = save_config({key: args.value})
    print(f"saved {key} to {path}")
    return EXIT_OK


def cmd_projects_create(args: argparse.Namespace, client: Any) -> int:
    project = client.create_project(
        args.title,
        description=args.description,
        default_language=args.default_language,
        default_aspect_ratio=args.default_aspect_ratio,
    )
    emit(args, project, columns=("project_id", "title", "created_at"))
    return EXIT_OK


def cmd_projects_list(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.list_projects(), columns=("project_id", "title", "created_at"))
    return EXIT_OK


def cmd_runs_create(args: argparse.Namespace, client: Any) -> int:
    run = client.create_run(
        args.project,
        _read_script(args),
        pipeline_key=args.pipeline_key,
        brand_name=args.brand_name,
        product_name=args.product_name,
        target_platform=args.platform,
        target_duration_seconds=args.duration,
        aspect_ratio=args.aspect_ratio,
        style_key=args.style_key,
        cost_budget=args.budget,
        quality_mode=args.quality_mode,
        idempotency_key=args.idempotency_key,
    )
    emit(args, run, columns=("run_id", "status", "pipeline_key", "created_at"))
    return EXIT_OK


def cmd_runs_get(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.get_run(args.run), columns=("run_id", "status", "created_at"))
    return EXIT_OK


def cmd_runs_list(args: argparse.Namespace, client: Any) -> int:
    emit(
        args,
        client.list_project_runs(args.project),
        columns=("run_id", "status", "created_at"),
    )
    return EXIT_OK


def cmd_runs_board(args: argparse.Namespace, client: Any) -> int:
    board = client.get_run_board(args.run)
    if getattr(args, "json_output", False):
        emit_json(board)
        return EXIT_OK
    data = board.to_dict() if isinstance(board, Model) else dict(board)
    run = data.get("run")
    if isinstance(run, dict):
        print_dict(run)
    paid = data.get("paid_providers_pending") or []
    print(f"paid_providers_pending: {len(paid)}")
    outputs = data.get("final_outputs") or []
    print(f"final_outputs: {len(outputs)}")
    if outputs:
        print_rows(outputs, ("asset_id", "asset_type", "mime_type"))
    return EXIT_OK


def cmd_runs_start(args: argparse.Namespace, client: Any) -> int:
    run = client.start_run(
        args.run,
        accept_intent_downgrade=args.accept_intent_downgrade,
        accept_paid_providers=args.accept_paid_providers,
    )
    emit(args, run, columns=("run_id", "status"))
    return EXIT_OK


def cmd_runs_wait(args: argparse.Namespace, client: Any) -> int:
    try:
        run = client.wait_run(
            args.run,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            stop_on_review=not args.no_stop_on_review,
            raise_on_timeout=True,
            on_progress=_progress_printer(),
        )
    except RunWaitTimeout as exc:
        eprint(f"error: {exc}")
        return EXIT_WAIT_TIMEOUT
    status = run.get("status")
    emit(args, run, columns=("run_id", "status"))
    if status == "failed":
        eprint(f"run {args.run} settled on 'failed'")
        return EXIT_RUN_FAILED
    if status in ("waiting_review", "paused"):
        eprint(f"run {args.run} is parked on '{status}' — a person is the blocking resource")
    return EXIT_OK


def cmd_assets_upload(args: argparse.Namespace, client: Any) -> int:
    asset = client.upload_asset(
        args.run,
        asset_type=args.type,
        path=args.path,
        filename=args.filename,
        mime_type=args.mime,
    )
    emit(args, asset, columns=("asset_id", "asset_type", "mime_type", "filename"))
    return EXIT_OK


def cmd_assets_list(args: argparse.Namespace, client: Any) -> int:
    emit(
        args,
        client.list_run_assets(args.run),
        columns=("asset_id", "asset_type", "mime_type", "filename"),
    )
    return EXIT_OK


def cmd_assets_download(args: argparse.Namespace, client: Any) -> int:
    data = client.download_asset(args.asset)
    Path(args.output).write_bytes(data)
    print(f"wrote {len(data)} bytes to {args.output}")
    return EXIT_OK


def cmd_jobs_list(args: argparse.Namespace, client: Any) -> int:
    emit(
        args,
        client.list_run_jobs(args.run),
        columns=("job_id", "job_type", "status", "updated_at"),
    )
    return EXIT_OK


def cmd_jobs_retry(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.retry_job(args.job), columns=("job_id", "status"))
    return EXIT_OK


def cmd_jobs_retry_failed(args: argparse.Namespace, client: Any) -> int:
    emit(args, client.retry_failed_jobs(args.run), columns=("job_id", "status"))
    return EXIT_OK


def _final_video_or_none(client: Any, run_id: str) -> tuple[Any | None, int]:
    try:
        return client.get_final_video(run_id), EXIT_OK
    except FinalVideoNotFound:
        return None, error_exit(
            f"run {run_id} has no final video yet",
            hint=f"check progress with: s2v runs get {run_id} "
            "(a failed run exits 4 from s2v runs wait)",
        )


def cmd_video_get(args: argparse.Namespace, client: Any) -> int:
    video, code = _final_video_or_none(client, args.run)
    if video is None:
        return code
    emit(args, video, columns=("asset_id", "asset_type", "mime_type", "download_url"))
    return EXIT_OK


def cmd_video_download(args: argparse.Namespace, client: Any) -> int:
    video, code = _final_video_or_none(client, args.run)
    if video is None:
        return code
    asset_id = video.get("asset_id")
    if not asset_id:
        return error_exit(f"the final video of run {args.run} has no asset_id to download")
    data = client.download_asset(asset_id)
    Path(args.output).write_bytes(data)
    print(f"wrote {len(data)} bytes to {args.output} (asset {asset_id})")
    return EXIT_OK


def _open_review_request(client: Any, run_id: str) -> tuple[Json | None, int]:
    requests = client.request("GET", f"/api/runs/{run_id}/review-requests") or []
    open_requests = [r for r in requests if r.get("status") == "open"]
    if not open_requests:
        return None, error_exit(f"run {run_id} has no open review request")
    if len(open_requests) > 1:
        ids = ", ".join(r.get("review_request_id") or "?" for r in open_requests)
        return None, error_exit(
            f"run {run_id} has {len(open_requests)} open review requests: {ids}",
            hint="pass --request <id> to pick one",
        )
    return open_requests[0], EXIT_OK


def cmd_reviews_list(args: argparse.Namespace, client: Any) -> int:
    requests = client.request("GET", f"/api/runs/{args.run}/review-requests") or []
    if args.status:
        requests = [r for r in requests if r.get("status") == args.status]
    emit(
        args,
        requests,
        columns=("review_request_id", "stage_key", "status", "iteration"),
    )
    return EXIT_OK


def cmd_review_submit(args: argparse.Namespace, client: Any) -> int:
    if args.request:
        request_id = args.request
    else:
        found, code = _open_review_request(client, args.run)
        if found is None:
            return code
        request_id = found.get("review_request_id")
        assert request_id
    result = client.submit_review_decision(request_id, args.decision, comment=args.note)
    emit(args, result, columns=("review_request_id", "status", "current_decision"))
    return EXIT_OK


def cmd_events(args: argparse.Namespace, client: Any) -> int:
    events = client.list_run_events(args.run, limit=args.limit)
    rows = [e.to_dict() if isinstance(e, Model) else e for e in events or []]
    if getattr(args, "json_output", False):
        emit_json(rows)
        return EXIT_OK
    preferred = ("created_at", "event_type", "stage_key", "status", "event_id")
    if rows:
        keys = list(rows[0])
        columns = [key for key in preferred if key in keys] or keys[:4]
    else:
        columns = ("event_type", "created_at")
    print_rows(rows, columns)
    return EXIT_OK


def cmd_quick(args: argparse.Namespace, client: Any) -> int:
    script = _read_script(args)
    project_id = args.project
    if project_id is None:
        project = client.create_project(args.project_title or "s2v quick run")
        project_id = project.get("project_id")
        assert project_id
        eprint(f"created project {project_id}")
    try:
        run = client.run_script(
            project_id,
            script,
            wait=True,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            accept_intent_downgrade=args.accept_intent_downgrade,
            accept_paid_providers=not args.no_accept_paid_providers,
            on_progress=_progress_printer(),
        )
    except RunWaitTimeout as exc:
        eprint(f"error: {exc}")
        return EXIT_WAIT_TIMEOUT
    status = run.get("status")
    run_id = run.get("run_id")
    if status == "failed":
        eprint(f"run {run_id} settled on 'failed'")
        return EXIT_RUN_FAILED
    video, code = _final_video_or_none(client, run_id)
    if video is None:
        return code
    emit(args, video, columns=("asset_id", "asset_type", "mime_type"))
    if args.download:
        asset_id = video.get("asset_id")
        if not asset_id:
            return error_exit(f"the final video of run {run_id} has no asset_id to download")
        data = client.download_asset(asset_id)
        Path(args.download).write_bytes(data)
        print(f"wrote {len(data)} bytes to {args.download}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def _add_json(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--json", dest="json_output", action="store_true", help="print raw JSON")


def _add_wait_options(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    sub.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s2v",
        description="Script2Video Studio command line — runs, assets, jobs, video.",
    )
    parser.add_argument("--base-url", help="API base URL (wins over env and config file)")
    parser.add_argument("--api-key", help="API key, s2v_... (wins over env and config file)")
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    whoami = subs.add_parser("whoami", help="show the identity this credential resolves to")
    _add_json(whoami)
    whoami.set_defaults(func=cmd_whoami)

    health = subs.add_parser("health", help="check that the server is reachable")
    _add_json(health)
    health.set_defaults(func=cmd_health)

    config = subs.add_parser("config", help="inspect or write the local config file")
    config_subs = config.add_subparsers(dest="config_command", required=True, metavar="ACTION")
    config_path_cmd = config_subs.add_parser("path", help="print the config file path")
    config_path_cmd.set_defaults(func=cmd_config_path)
    config_show = config_subs.add_parser("show", help="print the config (api-key masked)")
    config_show.set_defaults(func=cmd_config_show)
    config_set = config_subs.add_parser("set", help="persist base-url or api-key")
    config_set.add_argument("key", choices=("base-url", "api-key"))
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config_set)

    projects = subs.add_parser("projects", help="create or list projects")
    project_subs = projects.add_subparsers(dest="projects_command", required=True, metavar="ACTION")
    projects_create = project_subs.add_parser("create")
    projects_create.add_argument("title")
    projects_create.add_argument("--description")
    projects_create.add_argument("--language", dest="default_language")
    projects_create.add_argument("--aspect-ratio", dest="default_aspect_ratio")
    _add_json(projects_create)
    projects_create.set_defaults(func=cmd_projects_create)
    projects_list = project_subs.add_parser("list")
    _add_json(projects_list)
    projects_list.set_defaults(func=cmd_projects_list)

    runs = subs.add_parser("runs", help="create, inspect, start and wait for runs")
    run_subs = runs.add_subparsers(dest="runs_command", required=True, metavar="ACTION")

    runs_create = run_subs.add_parser("create", help="create a run from a script")
    runs_create.add_argument("project", metavar="PROJECT_ID")
    runs_create.add_argument("script", nargs="?")
    runs_create.add_argument("--script-file")
    runs_create.add_argument("--pipeline-key")
    runs_create.add_argument("--brand-name")
    runs_create.add_argument("--product-name")
    runs_create.add_argument("--platform", help="target platform, e.g. douyin")
    runs_create.add_argument("--duration", type=int, help="target duration in seconds")
    runs_create.add_argument("--aspect-ratio")
    runs_create.add_argument("--style-key")
    runs_create.add_argument("--budget", type=float, help="cost budget for the cost gate")
    runs_create.add_argument("--quality-mode")
    runs_create.add_argument("--idempotency-key")
    _add_json(runs_create)
    runs_create.set_defaults(func=cmd_runs_create)

    runs_get = run_subs.add_parser("get")
    runs_get.add_argument("run", metavar="RUN_ID")
    _add_json(runs_get)
    runs_get.set_defaults(func=cmd_runs_get)

    runs_list = run_subs.add_parser("list")
    runs_list.add_argument("--project", required=True, metavar="PROJECT_ID")
    _add_json(runs_list)
    runs_list.set_defaults(func=cmd_runs_list)

    runs_board = run_subs.add_parser("board", help="the run board: stages, gates, outputs")
    runs_board.add_argument("run", metavar="RUN_ID")
    _add_json(runs_board)
    runs_board.set_defaults(func=cmd_runs_board)

    runs_start = run_subs.add_parser("start")
    runs_start.add_argument("run", metavar="RUN_ID")
    runs_start.add_argument("--accept-intent-downgrade", action="store_true")
    runs_start.add_argument("--accept-paid-providers", action="store_true")
    _add_json(runs_start)
    runs_start.set_defaults(func=cmd_runs_start)

    runs_wait = run_subs.add_parser("wait", help="poll until the run settles or hits a human gate")
    runs_wait.add_argument("run", metavar="RUN_ID")
    _add_wait_options(runs_wait)
    runs_wait.add_argument(
        "--no-stop-on-review",
        action="store_true",
        help="keep polling past waiting_review/paused",
    )
    _add_json(runs_wait)
    runs_wait.set_defaults(func=cmd_runs_wait)

    assets = subs.add_parser("assets", help="upload, list and download run assets")
    asset_subs = assets.add_subparsers(dest="assets_command", required=True, metavar="ACTION")
    assets_upload = asset_subs.add_parser("upload")
    assets_upload.add_argument("run", metavar="RUN_ID")
    assets_upload.add_argument("path")
    assets_upload.add_argument("--type", required=True, help="asset type, e.g. person_image")
    assets_upload.add_argument("--filename")
    assets_upload.add_argument("--mime")
    _add_json(assets_upload)
    assets_upload.set_defaults(func=cmd_assets_upload)
    assets_list = asset_subs.add_parser("list")
    assets_list.add_argument("--run", required=True, metavar="RUN_ID")
    _add_json(assets_list)
    assets_list.set_defaults(func=cmd_assets_list)
    assets_download = asset_subs.add_parser("download")
    assets_download.add_argument("asset", metavar="ASSET_ID")
    assets_download.add_argument("-o", "--output", required=True)
    assets_download.set_defaults(func=cmd_assets_download)

    jobs = subs.add_parser("jobs", help="inspect and retry background jobs")
    job_subs = jobs.add_subparsers(dest="jobs_command", required=True, metavar="ACTION")
    jobs_list = job_subs.add_parser("list")
    jobs_list.add_argument("--run", required=True, metavar="RUN_ID")
    _add_json(jobs_list)
    jobs_list.set_defaults(func=cmd_jobs_list)
    jobs_retry = job_subs.add_parser("retry")
    jobs_retry.add_argument("job", metavar="JOB_ID")
    _add_json(jobs_retry)
    jobs_retry.set_defaults(func=cmd_jobs_retry)
    jobs_retry_failed = job_subs.add_parser("retry-failed")
    jobs_retry_failed.add_argument("--run", required=True, metavar="RUN_ID")
    _add_json(jobs_retry_failed)
    jobs_retry_failed.set_defaults(func=cmd_jobs_retry_failed)

    video = subs.add_parser("video", help="fetch or download the final video of a run")
    video_subs = video.add_subparsers(dest="video_command", required=True, metavar="ACTION")
    video_get = video_subs.add_parser("get")
    video_get.add_argument("run", metavar="RUN_ID")
    _add_json(video_get)
    video_get.set_defaults(func=cmd_video_get)
    video_download = video_subs.add_parser("download", help="download the render to a file")
    video_download.add_argument("run", metavar="RUN_ID")
    video_download.add_argument("-o", "--output", required=True)
    video_download.set_defaults(func=cmd_video_download)

    reviews = subs.add_parser("reviews", help="list open review requests of a run")
    reviews_list = reviews.add_subparsers(dest="reviews_command", required=True, metavar="ACTION")
    reviews_list_cmd = reviews_list.add_parser("list")
    reviews_list_cmd.add_argument("--run", required=True, metavar="RUN_ID")
    reviews_list_cmd.add_argument("--status")
    _add_json(reviews_list_cmd)
    reviews_list_cmd.set_defaults(func=cmd_reviews_list)

    review = subs.add_parser("review", help="submit a review decision")
    review_subs = review.add_subparsers(dest="review_command", required=True, metavar="ACTION")
    review_submit = review_subs.add_parser("submit")
    review_submit.add_argument("run", metavar="RUN_ID", help="run whose open request to decide")
    review_submit.add_argument("decision", choices=DECISIONS)
    review_submit.add_argument("--note", help="comment stored with the decision")
    review_submit.add_argument("--request", help="explicit review request id (skips resolution)")
    _add_json(review_submit)
    review_submit.set_defaults(func=cmd_review_submit)

    events = subs.add_parser("events", help="the event log of a run (for debugging)")
    events.add_argument("--run", required=True, metavar="RUN_ID")
    events.add_argument("--limit", type=int)
    _add_json(events)
    events.set_defaults(func=cmd_events)

    quick = subs.add_parser("quick", help="script in, video out — create, start, wait, render")
    quick.add_argument("script", nargs="?")
    quick.add_argument("--script-file")
    quick.add_argument("--project", metavar="PROJECT_ID", help="omit to auto-create a project")
    quick.add_argument("--project-title", help="title used when auto-creating the project")
    quick.add_argument("--download", help="write the final video to this file")
    _add_wait_options(quick)
    quick.add_argument("--accept-intent-downgrade", action="store_true")
    quick.add_argument(
        "--no-accept-paid-providers",
        action="store_true",
        help="stop at the paid-provider confirmation instead of accepting it",
    )
    _add_json(quick)
    quick.set_defaults(func=cmd_quick)

    return parser


def main(
    argv: Sequence[str] | None = None,
    client_factory: Callable[[str, str | None], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_only = (cmd_config_path, cmd_config_show, cmd_config_set)
    try:
        if args.func in config_only:
            return args.func(args, None)
        client = build_client(args, client_factory)
        return args.func(args, client)
    except MissingConfiguration as exc:
        eprint(f"error: {exc}")
        return EXIT_ERROR
    except TransportError as exc:
        return error_exit(
            str(exc),
            hint="check the base URL (--base-url) and that the server is reachable",
        )
    except ApiError as exc:
        return error_exit(f"HTTP {exc.status_code} {exc.code}: {exc.message}")
    except FinalVideoNotFound as exc:
        return error_exit(str(exc))
    except FileNotFoundError as exc:
        return error_exit(f"file not found: {exc.filename}")
    except OSError as exc:
        return error_exit(str(exc))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
