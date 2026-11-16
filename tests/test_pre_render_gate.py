"""P3 integration: the pre-render promise gate blocks composition.

The scenario from the capability plan is the spine of this file: a run whose
plan claims motion-heavy output while its shot list is almost entirely still
images must stop *before* ffmpeg runs, and must stay stopped until a human
re-sends the request with ``accept_quality_risk``. The FakeComposeWorker would
happily render anything — that is the point: the gate, not the renderer, is
what keeps a slideshow from being born.
"""

import base64

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app.actor import ActorContext
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.errors import error_catalog_table
from apps.api.app.models import AssetUpload, ComposeVideoRequest, ProjectCreate, RunCreate
from apps.api.app.routes import compose_video as compose_video_route
from apps.api.app.run_gate import RunGateError
from packages.pipeline_kit.loader import PipelineLoader
from tests.test_db_store import FakeComposeWorker, FakeQualityRunner

ACTOR = ActorContext(
    actor_id="user_test_owner",
    tenant_id="tenant_default",
    display_name="Test Owner",
)

MOTION_CLAIM = {
    "motion_expectation": "motion_heavy",
    "target_duration_seconds": 40,
}


def _stills_shot_list_payload():
    """Nine stills and one piece of footage — the 90% acceptance scenario."""

    shots = [
        {"shot_id": f"still-{index}", "duration_seconds": 4, "asset_type": "image"}
        for index in range(9)
    ]
    shots.append({"shot_id": "footage-0", "duration_seconds": 4, "asset_type": "video"})
    return {"version": "1.0", "shots": shots}


def _execution_plan_payload(**extra):
    """A minimal-but-valid execution_plan.v1 carrying the motion claim."""

    return {
        "schema_version": "1.0",
        "generated_at": "2026-11-10T00:00:00Z",
        "source": {"pipeline_key": "avatar_spokesperson"},
        "pipeline": {"pipeline_key": "avatar_spokesperson"},
        "execution_policy": "human_gated",
        "can_execute": True,
        "summary": "plan",
        "stage_plans": [],
        "decision_log": [],
        **extra,
    }


def _store_with_composable_run(tmp_path, name: str = "pre-render.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    store = DatabaseStore(
        Session(engine),
        compose_worker=FakeComposeWorker(),
        quality_runner=FakeQualityRunner(),
    )
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="pre-render gate"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="渲染前承诺校验"),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )

    def _upload(asset_type: str, filename: str, mime: str, content: bytes):
        return store.upload_asset(
            run.run_id,
            AssetUpload(
                asset_type=asset_type,
                filename=filename,
                content_base64=base64.b64encode(content).decode(),
                mime_type=mime,
            ),
        )

    avatar = _upload("avatar_video_clip", "avatar.mp4", "video/mp4", b"avatar bytes")
    subtitle = _upload(
        "subtitle_file",
        "subtitles.srt",
        "application/x-subrip",
        b"1\n00:00:00,000 --> 00:00:01,000\nHi\n",
    )
    music = _upload("background_music", "music.wav", "audio/wav", b"music bytes")
    return store, run, {"avatar": avatar, "subtitle": subtitle, "music": music}


def _seed_stills_plan(store, run_id: str):
    store.artifacts.append_artifact(
        run_id=run_id,
        stage_key="storyboard",
        artifact_type="shot_list",
        payload=_stills_shot_list_payload(),
    )
    store.artifacts.append_artifact(
        run_id=run_id,
        stage_key="plan",
        artifact_type="execution_plan",
        payload=_execution_plan_payload(**MOTION_CLAIM),
    )
    store.session.commit()


def _compose_request(assets, **kwargs) -> ComposeVideoRequest:
    return ComposeVideoRequest(
        avatar_video_asset_id=assets["avatar"].asset_id,
        subtitle_asset_id=assets["subtitle"].asset_id,
        music_asset_id=assets["music"].asset_id,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# the gate blocks
# --------------------------------------------------------------------------- #


def test_a_stills_plan_claiming_motion_blocks_composition(tmp_path) -> None:
    store, run, assets = _store_with_composable_run(tmp_path)
    _seed_stills_plan(store, run.run_id)

    with pytest.raises(RunGateError) as exc_info:
        store.compose_video(run.run_id, _compose_request(assets))

    error = exc_info.value
    assert error.code == "pre_render_check_not_acknowledged"
    assert error.status_code == 409
    assert error.details["critical_count"] >= 1
    assert error.details["resolution"]["body"] == {"accept_quality_risk": True}
    signals = error.details["signals"]
    assert signals, "a blocking verdict must carry the evidence"
    assert all(signal["severity"] == "critical" for signal in signals)
    assert error.details["check_artifact_id"]

    # The blocked attempt leaves its evidence behind...
    checks = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "pre_render_check"]
    assert len(checks) == 1
    assert checks[0].payload["status"] == "blocked"
    assert checks[0].payload["accepted_quality_risk"] is False
    assert checks[0].payload["motion_expectation"] == "motion_heavy"
    # ...and an event says what happened, even though nothing rendered.
    blocked_events = [
        event
        for event in store.list_events(run.run_id)
        if event.event_type == "run.compose_blocked"
    ]
    assert len(blocked_events) == 1
    assert blocked_events[0].payload["check_artifact_id"] == checks[0].artifact_id

    # Nothing rendered: the compose worker was never consulted.
    rendered = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "render_summary"]
    assert rendered == []


def test_the_run_status_does_not_change_when_blocked(tmp_path) -> None:
    """The gate refuses a render, not the run itself — the run keeps whatever
    status it had, so the operator can decide on the same board."""

    store, run, assets = _store_with_composable_run(tmp_path)
    _seed_stills_plan(store, run.run_id)
    status_before = store.get_run(run.run_id).status

    with pytest.raises(RunGateError):
        store.compose_video(run.run_id, _compose_request(assets))

    assert store.get_run(run.run_id).status == status_before


def test_accepting_the_risk_renders_and_records_the_decision(tmp_path) -> None:
    store, run, assets = _store_with_composable_run(tmp_path)
    _seed_stills_plan(store, run.run_id)

    result = store.compose_video(
        run.run_id,
        _compose_request(assets, accept_quality_risk=True),
        actor_id=ACTOR.actor_id,
        actor_display_name=ACTOR.display_name,
    )
    assert result is not None
    _invocation, asset, _render_summary = result
    assert asset.asset_type == "rendered_video"

    checks = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "pre_render_check"]
    assert len(checks) == 1
    assert checks[0].payload["status"] == "confirmed_with_risk"
    assert checks[0].payload["accepted_quality_risk"] is True

    log = store.list_run_decisions(run.run_id)
    entries = [entry for entry in log.entries if entry.category == "pre_render_risk"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.decision == "accept_quality_risk"
    assert entry.actor_type == "human"
    assert entry.actor_id == ACTOR.actor_id
    # A risk acceptance is a verdict, not a ranking over alternatives.
    assert entry.confidence is None
    assert entry.candidates, "the accepted critical signals are the candidates"


def test_confirming_via_the_http_layer_succeeds(tmp_path) -> None:
    store, run, assets = _store_with_composable_run(tmp_path)
    _seed_stills_plan(store, run.run_id)

    # The blocked attempt is a 409 whose detail carries the code and evidence.
    with pytest.raises(HTTPException) as exc_info:
        compose_video_route(run.run_id, _compose_request(assets), store, ACTOR)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "pre_render_check_not_acknowledged"
    assert exc_info.value.detail["signals"]
    assert exc_info.value.detail["resolution"]["body"] == {"accept_quality_risk": True}

    # With the acknowledgement the same request goes through.
    response = compose_video_route(
        run.run_id,
        _compose_request(assets, accept_quality_risk=True),
        store,
        ACTOR,
    )
    assert response["asset"]["asset_type"] == "rendered_video"


# --------------------------------------------------------------------------- #
# the gate stays quiet when it should
# --------------------------------------------------------------------------- #


def test_a_warning_only_plan_renders_without_confirmation(tmp_path) -> None:
    """Warnings never block: a plan with no critical contradiction renders, and
    the pass is recorded so the audit shows the gate was consulted."""

    store, run, assets = _store_with_composable_run(tmp_path)
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="storyboard",
        artifact_type="shot_list",
        payload={
            "version": "1.0",
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "b", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "c", "duration_seconds": 2, "asset_type": "video"},
            ],
        },
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="plan",
        artifact_type="execution_plan",
        payload=_execution_plan_payload(**MOTION_CLAIM),
    )
    store.session.commit()

    result = store.compose_video(run.run_id, _compose_request(assets))
    assert result is not None
    checks = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "pre_render_check"]
    assert len(checks) == 1
    assert checks[0].payload["status"] == "pass"
    assert checks[0].payload["critical_count"] == 0
    log = store.list_run_decisions(run.run_id)
    entries = [entry for entry in log.entries if entry.category == "pre_render_risk"]
    assert len(entries) == 1
    assert entries[0].decision == "pass"


def test_a_run_without_planning_artifacts_renders_silently(tmp_path) -> None:
    """No signals to compute — no artifact, no decision row, no friction. The
    gate must not manufacture a conflict from absent evidence."""

    store, run, assets = _store_with_composable_run(tmp_path)
    result = store.compose_video(run.run_id, _compose_request(assets))
    assert result is not None
    checks = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "pre_render_check"]
    assert checks == []
    log = store.list_run_decisions(run.run_id)
    assert all(entry.category != "pre_render_risk" for entry in log.entries)


def test_stills_without_a_claim_render_without_confirmation(tmp_path) -> None:
    """The informational-only branch end to end: stills are reported as a
    warning, but without a declared motion expectation nothing blocks."""

    store, run, assets = _store_with_composable_run(tmp_path)
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="storyboard",
        artifact_type="shot_list",
        payload=_stills_shot_list_payload(),
    )
    store.session.commit()

    result = store.compose_video(run.run_id, _compose_request(assets))
    assert result is not None
    checks = [a for a in store.list_artifacts(run.run_id, current_only=True) if a.artifact_type == "pre_render_check"]
    assert len(checks) == 1
    assert checks[0].payload["status"] == "pass"
    assert checks[0].payload["critical_count"] == 0
    assert checks[0].payload["motion_expectation"] == "unspecified"


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #


def test_the_new_error_code_is_published_with_gate_semantics() -> None:
    catalog = {entry["code"]: entry for entry in error_catalog_table()}
    entry = catalog["pre_render_check_not_acknowledged"]
    assert entry["status_code"] == 409
    assert entry["category"] == "gate"
    assert entry["retryable"] is False
    assert entry["description"]


def test_the_board_names_the_new_write_point() -> None:
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "apps" / "web" / "run_board.html").read_text(
        encoding="utf-8"
    )
    assert "pre_render_risk" in html
    assert "渲染前承诺" in html
    assert "accept_quality_risk" in html
