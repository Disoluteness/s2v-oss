#!/usr/bin/env python3
"""Step-by-step run: project, run, persona image, start, watch, retry.

Shows the level to plug in at when your own system owns scheduling, progress
reporting or a UI. Every call here has a counterpart button in the Web Studio.

    SCRIPT2VIDEO_BASE_URL=http://127.0.0.1:8080 python examples/upload_and_retry.py avatar.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from s2v import ApiError, FinalVideoNotFound, Script2VideoClient

SCRIPT = "把这张人物图做成 15 秒口播视频，讲解我们的新品。"


def retry_then_report(client: Script2VideoClient, run_id: str) -> None:
    """Retry whatever failed, then say what is left."""
    failed = [job for job in client.list_run_jobs(run_id) if job.status == "failed"]
    if not failed:
        print("nothing to retry")
        return
    for job in failed:
        print(f"  retrying {job.job_id} ({job.job_type or 'job'})")
        result = client.retry_action(run_id, "retry_job", job_id=job.job_id)
        print(f"    -> {result.get('status')}")


def main(argv: list[str]) -> int:
    client = Script2VideoClient()

    project = client.create_project("示例：口播重试")
    print(f"project {project.project_id}")

    run = client.create_run(
        project.project_id,
        SCRIPT,
        pipeline_key="avatar_spokesperson",
        target_platform="douyin",
        target_duration_seconds=15,
        cost_budget=8.0,
    )
    print(f"run {run.run_id} status={run.status}")

    if len(argv) > 1:
        image = Path(argv[1])
        asset = client.upload_person_image(run.run_id, path=image)
        print(f"uploaded persona image -> {asset.asset_id} ({asset.mime_type})")

    # The first paid call on a run needs an explicit acknowledgement. Passing
    # accept_paid_providers=True clears every provider the run is waiting on.
    client.start_run(run.run_id, accept_paid_providers=True)

    seen: set[str] = set()

    def on_progress(current) -> None:
        if current.status not in seen:
            seen.add(current.status)
            print(f"  stage={current.current_stage_key} status={current.status}")

    try:
        finished = client.wait_run(run.run_id, timeout=1800, on_progress=on_progress)
    except ApiError as exc:
        print(f"API error while waiting: HTTP {exc.status} {exc.code} — {exc.message}")
        return 1

    print(f"settled at {finished.status}")
    if finished.status == "failed":
        retry_then_report(client, run.run_id)
        return 1
    if finished.is_blocked_on_human:
        print("a human needs to review before this run continues:")
        for request in client.request("GET", f"/api/runs/{run.run_id}/review-requests") or []:
            print(f"  {request['review_request_id']} status={request['status']}")
        return 0

    try:
        video = client.get_final_video(run.run_id)
    except FinalVideoNotFound as exc:
        print(exc)
        return 1
    print(f"final video: {video.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
