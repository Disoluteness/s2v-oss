import base64
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    ApprovalRequest,
    AssetUpload,
    AvatarVideoArchiveRequest,
    AvatarVideoFromAssetsRequest,
    AvatarVideoSubmitRequest,
    BackgroundJobCreate,
    BackgroundJobStatus,
    BrandKitCreate,
    ComposeVideoRequest,
    CompositionGraphBuildRequest,
    EditOperation,
    EditToolRequest,
    EditVariantRequest,
    EventCreate,
    EventSourceType,
    HtmlCanvasPreviewRequest,
    ImageGenerateRequest,
    ManifestActionRequest,
    MediaAnalysisRequest,
    MusicGenerateRequest,
    ProjectCreate,
    ProviderCallbackRequest,
    ProviderCredentialCreate,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    QualityGateRequest,
    RunCopyRequest,
    RunCreate,
    RunStateActionRequest,
    RunStatus,
    ScriptScreeningRunCreateRequest,
    SourceMediaIngestRequest,
    StageStatus,
    StylePresetCreate,
    SubtitleGenerateRequest,
    ToolInvocationStatus,
    TtsGenerateRequest,
)
from apps.api.app.run_gate import RunGateError
from packages.compose_kit import FfmpegComposeResult
from packages.edit_kit import FfmpegEditResult
from packages.media_analysis_kit import (
    AudioEnergyAnalysis,
    AudioEnergySegment,
    FfmpegMediaAnalysisResult,
    MediaFrameSample,
    SceneBoundary,
    SceneCut,
)
from packages.pipeline_kit.loader import PipelineLoader
from packages.provider_kit import DashScopeAvatarVideoAdapter
from packages.tool_kit.registry import ToolRegistry


class FakeDashScopeTransport:
    def __init__(self) -> None:
        self.calls = []

    def request_json(self, *, method, url, headers, payload=None, timeout_seconds=60):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        if method == "POST" and url.endswith("/face-detect"):
            return {
                "output": {
                    "face_bbox": [10, 20, 110, 160],
                    "ext_bbox": [0, 0, 180, 220],
                }
            }
        if method == "POST":
            return {"output": {"task_id": "task_avatar_1", "task_status": "PENDING"}}
        return {
            "output": {
                "task_id": "task_avatar_1",
                "task_status": "SUCCEEDED",
                "video_url": "https://example.com/avatar.mp4",
            }
        }


class FailingDashScopeTransport(FakeDashScopeTransport):
    def request_json(self, *, method, url, headers, payload=None, timeout_seconds=60):
        if method == "GET":
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "payload": payload,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return {
                "output": {
                    "task_id": "task_avatar_1",
                    "task_status": "FAILED",
                    "code": "InvalidParameter.Ratio",
                    "message": "The request parameter is invalid",
                }
            }
        return super().request_json(
            method=method,
            url=url,
            headers=headers,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )


class SubmitOnlyDashScopeTransport(FakeDashScopeTransport):
    def request_json(self, *, method, url, headers, payload=None, timeout_seconds=60):
        if method == "GET":
            raise AssertionError("callback completion should not poll provider status")
        return super().request_json(
            method=method,
            url=url,
            headers=headers,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )


class FakeTtsAdapter:
    provider_key = "dashscope"
    tool_key = "dashscope_tts"

    def check_availability(self) -> bool:
        return True

    def synthesize(self, payload):
        return {
            "content": b"voice bytes",
            "mime_type": "audio/wav",
            "audio_url": "https://example.com/voice.wav",
            "model": payload.get("model"),
            "voice": payload.get("voice"),
            "language_type": payload.get("language_type"),
        }


class FakeMusicAdapter:
    provider_key = "elevenlabs"
    tool_key = "elevenlabs_music"

    def check_availability(self) -> bool:
        return True

    def compose(self, payload):
        return {
            "content": b"music bytes",
            "mime_type": "audio/mpeg",
            "output_format": payload.get("output_format"),
        }


class FakeImageAdapter:
    provider_key = "dashscope"
    tool_key = "dashscope_image"

    def check_availability(self) -> bool:
        return True

    def generate(self, payload):
        return {
            "content": b"image bytes",
            "mime_type": "image/png",
            "image_url": "https://example.com/person.png",
            "model": payload.get("model") or "qwen-image",
            "size": payload.get("size") or "1024*1024",
        }


class CostlyImageAdapter(FakeImageAdapter):
    def estimate_cost(self, capability, payload) -> float:
        assert capability == "image_generation"
        return 1.25


class FakeComposeWorker:
    def check_availability(self) -> bool:
        return True

    def compose(self, request):
        assert request.avatar_video_path.is_file()
        assert request.subtitle_path is not None
        assert request.subtitle_path.is_file()
        assert request.music_path is not None
        assert request.music_path.is_file()
        request.output_path.write_bytes(b"rendered mp4 bytes")
        return FfmpegComposeResult(
            output_path=request.output_path,
            command=["ffmpeg", "fake"],
            duration_seconds=2.0,
            width=request.width,
            height=request.height,
            has_audio=True,
            subtitles_embedded=True,
            subtitle_track_added=False,
        )


class FakeQualityRunner:
    runner_key = "quality_gate_runner"
    provider_key = "local"

    def check_availability(self) -> bool:
        return True

    def evaluate(self, request):
        assert request.media_path.is_file()
        return {
            "schema_version": "1.0",
            "generated_at": "2026-09-23T00:00:00+00:00",
            "runner": self.runner_key,
            "status": "warning",
            "quality_score": 92,
            "min_score": request.min_score,
            "media": {"width": 720, "height": 1280, "has_audio": True},
            "checks": [
                {
                    "check_key": "provider_output",
                    "status": "warning",
                    "severity": "warning",
                    "message": "Preview-tier output.",
                    "expected": {},
                    "measured": {},
                }
            ],
            "summary": {"failed": 0, "warnings": 1, "passed": 1, "skipped": 0},
            "human_review": {
                "required": True,
                "recommended_action": "review_warnings",
                "reasons": ["provider_output"],
            },
            "commands": [["ffprobe", "fake"]],
        }


class FakeEditWorker:
    def check_availability(self) -> bool:
        return True

    def run(self, request):
        assert request.source_paths
        assert request.source_paths[0].is_file()
        request.output_path.write_bytes(b"edited media bytes")
        is_cover = request.operation == "cover_generation"
        return FfmpegEditResult(
            operation=request.operation,
            output_path=request.output_path,
            mime_type="image/jpeg" if is_cover else "video/mp4",
            asset_type="cover_image" if is_cover else "edited_video",
            commands=[["ffmpeg", request.operation]],
            duration_seconds=None if is_cover else 1.5,
            width=request.width or 1080,
            height=request.height or 1920,
            has_audio=not is_cover,
            metadata={"fake": True, "fit": request.fit},
        )


class FakeMediaAnalyzer:
    """Stand-in for FfmpegMediaAnalyzer.

    The optional scene/audio knobs let a test turn the V1.1 advanced-analysis
    paths on without a real FFmpeg binary; the defaults keep reproducing the
    pre-V1.1 shape (no cuts, no energy) so older assertions stay meaningful.
    """

    def __init__(
        self,
        *,
        scene_cuts: list[SceneCut] | None = None,
        scene_boundaries: list[SceneBoundary] | None = None,
        scene_threshold: float | None = None,
        audio_energy: AudioEnergyAnalysis | None = None,
    ) -> None:
        self.scene_cuts = scene_cuts or []
        self.scene_boundaries = scene_boundaries or []
        self.scene_threshold = scene_threshold
        self.audio_energy = audio_energy

    def check_availability(self) -> bool:
        return True

    def analyze(
        self,
        path,
        *,
        output_dir,
        max_frames=5,
        detect_scenes=True,
        scene_threshold=0.30,
        analyze_audio=True,
        audio_window_seconds=0.5,
    ):
        assert path.is_file()
        output_dir.mkdir(parents=True, exist_ok=True)
        frame_path = output_dir / "frame_0001.jpg"
        frame_path.write_bytes(b"fake jpg")
        return FfmpegMediaAnalysisResult(
            source_path=path,
            probe={
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "12.500000",
                    "bit_rate": "800000",
                    "size": "1024",
                },
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "30/1",
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": 2,
                    },
                ],
            },
            duration_seconds=12.5,
            width=1280,
            height=720,
            frame_rate=30.0,
            video_codec="h264",
            audio_codecs=["aac"],
            has_video=True,
            has_audio=True,
            frames=[
                MediaFrameSample(
                    path=frame_path,
                    timestamp_seconds=0.0,
                    timestamp_formatted="00:00",
                    width=1280,
                    height=720,
                )
            ],
            scene_cuts=self.scene_cuts,
            scene_boundaries=self.scene_boundaries,
            scene_detection_threshold=self.scene_threshold
            if (self.scene_boundaries or self.scene_cuts)
            else None,
            audio_energy=self.audio_energy,
        )


class FailingPipelineLoader:
    def get_pipeline(self, pipeline_key):
        raise AssertionError(f"pipeline loader should not be used for snapshotted run: {pipeline_key}")


def test_database_store_provider_credentials_refresh_runtime_registry(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-credentials.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session)

    credential = store.create_provider_credential(
        ProviderCredentialCreate(
            provider="dashscope",
            label="DashScope workspace key",
            secret_values={"DASHSCOPE_API_KEY": "sk-db-dashscope"},
        )
    )
    after = store.get_tool("dashscope_avatar_video")
    project = store.create_project(ProjectCreate(title="Credential preflight"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="凭证预检"),
        pipeline=pipeline,
    )
    preflight = store.run_preflight(run.run_id)
    check = store.check_provider_credential(credential.credential_id)
    session.close()

    avatar_capability = next(
        capability
        for capability in preflight.payload["required_capabilities"]
        if capability["capability"] == "avatar_video"
    )
    assert after["status"] == "available"
    assert avatar_capability["status"] == "available"
    assert avatar_capability["recommended_tool_key"] == "dashscope_avatar_video"
    assert check is not None
    assert check.ok is True
    assert check.env_var_names == ["DASHSCOPE_API_KEY"]
    assert check.credential.secret_preview == {"DASHSCOPE_API_KEY": "sk-...cope"}


def test_database_store_project_dashboard_and_run_copy(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'project-dashboard.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session)

    project = store.create_project(ProjectCreate(title="Dashboard project"))
    review_run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
        ),
        pipeline=pipeline,
    )
    failed_run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="失败任务聚合测试", brand_name="测试品牌"),
        pipeline=pipeline,
    )
    store.runs.update_run_state(
        review_run.run_id,
        status=RunStatus.WAITING_REVIEW,
        current_stage_key="plan",
    )
    store.runs.update_stage_status(
        review_run.run_id,
        "plan",
        StageStatus.WAITING_REVIEW,
    )
    job = store.background_jobs.create_job(
        run_id=failed_run.run_id,
        payload=BackgroundJobCreate(job_type="avatar_from_assets"),
    )
    store.background_jobs.update_job(
        job.job_id,
        status=BackgroundJobStatus.FAILED,
        progress=100,
        error_code="provider_error",
        error_message="provider failed",
    )

    summaries = store.list_project_summaries()
    dashboard = store.project_dashboard(project.project_id, search="茶小朵")
    waiting_runs = store.list_project_dashboard_runs(
        project.project_id,
        status=RunStatus.WAITING_REVIEW,
    )
    copied = store.copy_run(review_run.run_id, RunCopyRequest(copy_reason="再做一版"))
    session.close()

    assert summaries[0].project.project_id == project.project_id
    assert summaries[0].run_count == 2
    assert dashboard is not None
    assert dashboard.metrics.total_runs == 2
    assert dashboard.metrics.pending_reviews == 1
    assert dashboard.metrics.failed_jobs == 1
    assert len(dashboard.runs) == 1
    assert dashboard.runs[0].run_id == review_run.run_id
    assert dashboard.review_queue[0].stage_key == "plan"
    assert dashboard.failed_jobs[0].job_id == job.job_id
    assert waiting_runs is not None
    assert [run.run_id for run in waiting_runs] == [review_run.run_id]
    assert copied is not None
    assert copied.source_run_id == review_run.run_id
    assert copied.run.project_id == project.project_id
    assert copied.run.status == RunStatus.DRAFT
    assert copied.workspace_url == f"/workspace?run_id={copied.run.run_id}"


def test_database_store_run_board_aggregates_run_state(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'run-board.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session)

    project = store.create_project(ProjectCreate(title="Run board project"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="Run board 测试脚本",
            brand_name="测试品牌",
            product_name="测试产品",
        ),
        pipeline=pipeline,
    )
    execution_plan = store.plan_execution(run.run_id)
    manifest = store.plan_stage_execution(run.run_id, execution_plan_artifact=execution_plan)
    compose_stage_id = next(
        stage.stage_run_id for stage in run.stages if stage.stage_key == "compose"
    )
    render_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            stage_run_id=compose_stage_id,
            asset_type="rendered_video",
            filename="final.mp4",
            content_base64=base64.b64encode(b"video bytes").decode(),
            mime_type="video/mp4",
            width=1080,
            height=1920,
        ),
    )
    job = store.background_jobs.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(
            stage_run_id=compose_stage_id,
            job_type="stage_tool_step",
            request_payload={"manifest_step_key": "test-step"},
        ),
    )

    board = store.run_board(run.run_id)
    session.close()

    assert execution_plan is not None
    assert manifest is not None
    assert render_asset is not None
    assert board is not None
    assert board.run.run_id == run.run_id
    assert board.project.project_id == project.project_id
    assert board.metrics["artifact_count"] >= 2
    assert board.metrics["asset_count"] >= 1
    assert board.decision_log
    artifact_types = {artifact.artifact_type for artifact in board.artifact_timeline}
    assert {"execution_plan", "stage_execution_manifest"}.issubset(artifact_types)
    compose_lane = next(lane for lane in board.stage_lanes if lane.stage.stage_key == "compose")
    assert compose_lane.assets[0].asset_id == render_asset.asset_id
    assert compose_lane.jobs[0].job_id == job.job_id
    assert board.final_outputs[0].asset_id == render_asset.asset_id
    assert board.final_outputs[0].download_url == f"/api/assets/{render_asset.asset_id}/download"
    assert board.version_groups[0].assets[0].asset_id == render_asset.asset_id
    assert board.input_issues
    assert board.workspace_url == f"/workspace?run_id={run.run_id}"
    assert board.dashboard_url == f"/dashboard?project_id={project.project_id}"


def test_database_store_disabled_provider_blocks_registry_even_with_env(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'disabled-provider.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(session)

    credential = store.create_provider_credential(
        ProviderCredentialCreate(
            provider="dashscope",
            status=ProviderCredentialStatus.DISABLED,
            secret_values={"DASHSCOPE_API_KEY": "sk-disabled"},
        )
    )
    tool = store.get_tool("dashscope_avatar_video")
    check = store.check_provider_credential(credential.credential_id)
    enabled = store.update_provider_credential(
        credential.credential_id,
        ProviderCredentialUpdate(status=ProviderCredentialStatus.ACTIVE),
    )
    enabled_tool = store.get_tool("dashscope_avatar_video")
    session.close()

    assert tool["status"] == "unavailable"
    assert tool["missing_dependencies"] == ["provider_disabled:dashscope"]
    assert check is not None
    assert check.ok is False
    assert "disabled" in check.message
    assert enabled is not None
    assert enabled.status == ProviderCredentialStatus.ACTIVE
    assert enabled_tool["status"] == "available"


def test_database_store_advances_run_and_persists_artifacts_across_sessions(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    first_session = Session(engine)
    first_store = DatabaseStore(first_session)
    project = first_store.create_project(ProjectCreate(title="Milk tea ad"))
    run = first_store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
        ),
        pipeline=pipeline,
    )
    started = first_store.start_run(run.run_id)
    first_session.close()

    assert started is not None
    assert started.status == RunStatus.WAITING_REVIEW
    assert started.current_stage_key == "plan"
    assert next(stage for stage in started.stages if stage.stage_key == "intake").status == (
        StageStatus.COMPLETED
    )
    assert next(stage for stage in started.stages if stage.stage_key == "plan").status == (
        StageStatus.WAITING_REVIEW
    )

    second_session = Session(engine)
    second_store = DatabaseStore(second_session)
    reloaded_run = second_store.get_run(run.run_id)
    reloaded_artifacts = second_store.list_artifacts(run.run_id)

    assert reloaded_run is not None
    assert reloaded_run.run_id == run.run_id
    assert reloaded_run.status == RunStatus.WAITING_REVIEW
    assert reloaded_artifacts is not None
    assert {artifact.artifact_type for artifact in reloaded_artifacts} >= {
        "preflight_report",
        "execution_plan",
    }
    assert [artifact.artifact_type for artifact in reloaded_artifacts if artifact.stage_key != "preflight"] == [
        "intake_brief",
        "production_plan",
    ]
    assert all(artifact.is_current for artifact in reloaded_artifacts)
    intake_artifact = next(
        artifact for artifact in reloaded_artifacts if artifact.artifact_type == "intake_brief"
    )
    plan_artifact = next(
        artifact for artifact in reloaded_artifacts if artifact.artifact_type == "production_plan"
    )
    assert intake_artifact.payload["brand_name"] == "茶小朵"
    assert plan_artifact.payload["creative_strategy"]["hook"] == (
        "秋天第一杯奶茶，还没人给你点？"
    )
    assert plan_artifact.payload["planner"]["tool_key"] == "rule_based_planner"

    reviewed = second_store.review_stage(
        run.run_id,
        "plan",
        ApprovalRequest(decision="approved"),
    )
    second_session.close()

    assert reviewed is not None
    assert reviewed.current_stage_key == "script"
    assert reviewed.status == RunStatus.WAITING_REVIEW
    assert next(stage for stage in reviewed.stages if stage.stage_key == "plan").status == (
        StageStatus.APPROVED
    )
    assert next(stage for stage in reviewed.stages if stage.stage_key == "script").status == (
        StageStatus.WAITING_REVIEW
    )

    third_session = Session(engine)
    third_store = DatabaseStore(third_session)
    script_artifacts = third_store.list_artifacts(run.run_id)
    storyboard_reviewed = third_store.review_stage(
        run.run_id,
        "script",
        ApprovalRequest(decision="approved"),
    )
    final_artifacts = third_store.list_artifacts(run.run_id)
    third_session.close()

    assert storyboard_reviewed is not None
    assert storyboard_reviewed.current_stage_key == "storyboard"
    assert storyboard_reviewed.status == RunStatus.WAITING_REVIEW
    assert script_artifacts is not None
    assert script_artifacts[-1].payload["segments"][0]["text"] == (
        "秋天第一杯奶茶，还没人给你点？"
    )
    assert final_artifacts is not None
    assert [artifact.artifact_type for artifact in final_artifacts if artifact.stage_key != "preflight"] == [
        "intake_brief",
        "production_plan",
        "spoken_script",
        "shot_list",
    ]
    assert final_artifacts[-1].payload["shots"][0]["spoken_segment_ids"] == ["line_01"]


def test_database_store_animated_explainer_smoke_reaches_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'animated-explainer.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("animated_explainer")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Sky explainer"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="animated_explainer",
            raw_script="为什么天空是蓝色的？",
            target_duration_seconds=30,
            target_platform="douyin",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    plan_reviewed = store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    script_reviewed = store.review_stage(
        run.run_id,
        "script",
        ApprovalRequest(decision="approved"),
    )
    storyboard_reviewed = store.review_stage(
        run.run_id,
        "storyboard",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == "animated_explainer"
    assert started.current_stage_key == "plan"
    assert plan_reviewed is not None
    assert plan_reviewed.current_stage_key == "script"
    assert script_reviewed is not None
    assert script_reviewed.current_stage_key == "storyboard"
    assert storyboard_reviewed is not None
    assert storyboard_reviewed.status == RunStatus.WAITING_REVIEW
    assert storyboard_reviewed.current_stage_key == "final_review"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert current_payloads["render_summary"] == {
        "stage_key": "compose",
        "status": "placeholder",
    }
    assert "publish_package" not in current_payloads


def test_database_store_source_clip_factory_smoke_reaches_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'source-clip-factory.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("source_clip_factory")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Clip factory"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="source_clip_factory",
            raw_script="把这条长视频切成 3 条适合抖音的短视频。",
            target_duration_seconds=30,
            target_platform="douyin",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    source_reviewed = store.review_stage(
        run.run_id,
        "source_ingest",
        ApprovalRequest(decision="approved"),
    )
    candidates_reviewed = store.review_stage(
        run.run_id,
        "clip_selection",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == "source_clip_factory"
    assert started.current_stage_key == "source_ingest"
    assert source_reviewed is not None
    assert source_reviewed.current_stage_key == "clip_selection"
    assert candidates_reviewed is not None
    assert candidates_reviewed.status == RunStatus.WAITING_REVIEW
    assert candidates_reviewed.current_stage_key == "final_review"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert current_payloads["source_media_review"] == {
        "stage_key": "source_ingest",
        "status": "placeholder",
    }
    assert current_payloads["media_analysis_report"] == {
        "stage_key": "analysis",
        "status": "placeholder",
    }
    assert current_payloads["clip_candidate_list"] == {
        "stage_key": "clip_selection",
        "status": "placeholder",
    }
    assert current_payloads["clip_export_manifest"] == {
        "stage_key": "export_clips",
        "status": "placeholder",
    }
    assert "publish_package" not in current_payloads


@pytest.mark.parametrize(
    ("pipeline_key", "raw_script"),
    [
        ("talking_head", "把这段访谈切成 3 条竖版短片。"),
        ("podcast_repurpose", "把这一期播客切成 3 条短视频。"),
        ("cinematic", "用这些镜头剪一支 30 秒品牌预告。"),
    ],
)
def test_database_store_p1_media_lines_reach_final_review(
    tmp_path, pipeline_key: str, raw_script: str
) -> None:
    """The three P1 lines share the clipping kernel; each must reach final review.

    They differ in selection criteria and capability requirements, not in the
    orchestration shape, so one parameterised smoke is enough to prove the
    stages resolve and the review gates fire.
    """
    engine = create_engine(f"sqlite:///{tmp_path / (pipeline_key + '.db')}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline(pipeline_key)
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title=pipeline.display_name))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key=pipeline_key,
            raw_script=raw_script,
            target_duration_seconds=30,
            target_platform="douyin",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    source_reviewed = store.review_stage(
        run.run_id,
        "source_ingest",
        ApprovalRequest(decision="approved"),
    )
    candidates_reviewed = store.review_stage(
        run.run_id,
        "clip_selection",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == pipeline_key
    assert started.current_stage_key == "source_ingest"
    assert source_reviewed is not None
    assert source_reviewed.current_stage_key == "clip_selection"
    assert candidates_reviewed is not None
    assert candidates_reviewed.status == RunStatus.WAITING_REVIEW
    assert candidates_reviewed.current_stage_key == "final_review"
    artifact_types = {artifact.artifact_type for artifact in artifacts}
    assert {
        "source_media_review",
        "media_analysis_report",
        "clip_candidate_list",
        "clip_export_manifest",
    }.issubset(artifact_types)


def test_database_store_ingests_source_media_manifest(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'source-media-ingest.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("source_clip_factory")
    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Source media ingest"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="source_clip_factory",
            raw_script="把上传素材整理成短视频候选。",
            target_duration_seconds=30,
        ),
        pipeline=pipeline,
    )
    source_stage = next(stage for stage in run.stages if stage.stage_key == "source_ingest")

    result = store.ingest_source_media(
        run.run_id,
        SourceMediaIngestRequest(
            stage_run_id=source_stage.stage_run_id,
            files=[
                {
                    "filename": "demo.mp4",
                    "content_base64": base64.b64encode(b"video bytes").decode("ascii"),
                    "mime_type": "video/mp4",
                    "label": "主视频",
                    "role": "primary_footage",
                    "duration_ms": 15_000,
                    "width": 1080,
                    "height": 1920,
                    "notes": ["来自内部脚本放映室测试素材"],
                    "source_metadata": {"shot_count_hint": 3},
                }
            ],
            links=[
                {
                    "url": "https://example.com/reference.pdf",
                    "label": "脚本文档",
                    "role": "brief",
                    "rights": {"license": "internal"},
                }
            ],
            default_rights={"owner": "internal-team"},
            notes=["素材可进入下一步媒体分析。"],
        ),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    assets = store.list_assets(run.run_id)
    events = store.list_events(run.run_id, event_type="source_media.ingested")
    session.close()

    assert result is not None
    assert result.artifact.artifact_type == "source_asset_manifest"
    assert result.artifact.schema_key == "source_asset_manifest.v1"
    assert result.source_media_review.artifact_type == "source_media_review"
    assert len(result.assets) == 1
    assert result.assets[0].asset_type == "source_video"
    assert result.assets[0].asset_metadata["media_type"] == "video"
    assert result.manifest["analysis_entry"] == {
        "status": "pending",
        "recommended_next_artifacts": ["media_analysis_report"],
        "source_asset_count": 1,
        "source_link_count": 1,
    }
    assert result.manifest["source_assets"][0]["label"] == "主视频"
    assert result.manifest["source_assets"][0]["rights"] == {"owner": "internal-team"}
    assert result.manifest["source_links"][0]["media_type"] == "document"
    assert result.manifest["source_links"][0]["rights"] == {
        "owner": "internal-team",
        "license": "internal",
    }
    assert artifacts is not None
    assert {artifact.artifact_type for artifact in artifacts} >= {
        "source_asset_manifest",
        "source_media_review",
    }
    assert assets is not None
    assert len(assets) == 1
    assert events is not None
    assert len(events) == 1
    assert events[0].payload["file_count"] == 1
    assert events[0].payload["link_count"] == 1


def test_database_store_analyzes_source_media_manifest(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'source-media-analysis.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("source_clip_factory")
    session = Session(engine)
    store = DatabaseStore(session, media_analyzer=FakeMediaAnalyzer())
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Source media analysis"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="source_clip_factory",
            raw_script="分析源视频并生成候选片段输入。",
            target_duration_seconds=30,
            target_platform="douyin",
        ),
        pipeline=pipeline,
    )
    source_stage = next(stage for stage in run.stages if stage.stage_key == "source_ingest")
    ingest = store.ingest_source_media(
        run.run_id,
        SourceMediaIngestRequest(
            stage_run_id=source_stage.stage_run_id,
            files=[
                {
                    "filename": "source.mp4",
                    "content_base64": base64.b64encode(b"fake video bytes").decode("ascii"),
                    "mime_type": "video/mp4",
                    "label": "主视频",
                    "role": "primary_footage",
                }
            ],
        ),
    )
    analysis_stage = next(stage for stage in run.stages if stage.stage_key == "analysis")

    result = store.analyze_source_media(
        run.run_id,
        MediaAnalysisRequest(stage_run_id=analysis_stage.stage_run_id),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    assets = store.list_assets(run.run_id)
    invocations = store.list_tool_invocations(run.run_id)
    events = store.list_events(run.run_id, event_type="media_analysis.completed")
    session.close()

    assert ingest is not None
    assert result is not None
    assert result.artifact.artifact_type == "media_analysis_report"
    assert result.artifact.schema_key == "media_analysis_report.v1"
    assert result.video_analysis_brief.artifact_type == "video_analysis_brief"
    assert result.video_analysis_brief.schema_key == "video_analysis_brief.v1"
    assert result.report["media_summary"]["analyzed_source_count"] == 1
    assert result.report["media_summary"]["frame_asset_count"] == 1
    assert result.report["source_assets"][0]["video_codec"] == "h264"
    assert result.report["transcript"]["status"] == "unavailable"
    assert result.brief["clip_planning_inputs"]["transcript_available"] is False
    assert result.brief["clip_planning_inputs"]["candidate_windows"][0]["source_asset_id"]
    assert len(result.frame_assets) == 1
    assert result.frame_assets[0].asset_type == "analysis_frame"
    assert result.frame_assets[0].asset_metadata["source_asset_id"] == ingest.assets[0].asset_id
    assert artifacts is not None
    assert {artifact.artifact_type for artifact in artifacts} >= {
        "source_asset_manifest",
        "source_media_review",
        "media_analysis_report",
        "video_analysis_brief",
    }
    assert assets is not None
    assert {asset.asset_type for asset in assets} >= {"source_video", "analysis_frame"}
    assert invocations is not None
    assert invocations[-1].tool_key == "ffmpeg_media_probe"
    assert events is not None
    assert len(events) == 1
    assert events[0].payload["frame_asset_count"] == 1


def _ingest_and_analyze(store, tmp_path, request_overrides: dict | None = None):
    """Shared recipe: one project, one source_clip_factory run, one asset, one analysis."""
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="V1.1 media analysis"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="source_clip_factory",
            raw_script="分析源视频并生成候选片段输入。",
            target_duration_seconds=30,
            target_platform="douyin",
        ),
        pipeline=PipelineLoader().get_pipeline("source_clip_factory"),
    )
    source_stage = next(stage for stage in run.stages if stage.stage_key == "source_ingest")
    store.ingest_source_media(
        run.run_id,
        SourceMediaIngestRequest(
            stage_run_id=source_stage.stage_run_id,
            files=[
                {
                    "filename": "source.mp4",
                    "content_base64": base64.b64encode(b"fake video bytes").decode("ascii"),
                    "mime_type": "video/mp4",
                    "label": "主视频",
                    "role": "primary_footage",
                }
            ],
        ),
    )
    analysis_stage = next(stage for stage in run.stages if stage.stage_key == "analysis")
    request = MediaAnalysisRequest(stage_run_id=analysis_stage.stage_run_id)
    if request_overrides:
        for key, value in request_overrides.items():
            setattr(request, key, value)
    return store.analyze_source_media(run.run_id, request)


def test_database_store_media_analysis_reports_scene_boundaries_and_audio(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'v11-analysis.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    audio_energy = AudioEnergyAnalysis(
        sample_interval_seconds=0.5,
        duration_seconds=12.5,
        mean_rms_db=-20.0,
        peak_rms_db=-6.0,
        dynamic_range_db=14.0,
        silence_seconds=3.0,
        silence_ratio=0.24,
        segments=[
            AudioEnergySegment(
                kind="loud",
                start_seconds=0.0,
                end_seconds=2.0,
                duration_seconds=2.0,
                peak_rms_db=-8.0,
            ),
            AudioEnergySegment(
                kind="silence",
                start_seconds=2.0,
                end_seconds=5.0,
                duration_seconds=3.0,
                peak_rms_db=-60.0,
            ),
        ],
    )
    store = DatabaseStore(
        session,
        media_analyzer=FakeMediaAnalyzer(
            scene_cuts=[
                SceneCut(
                    index=1,
                    timestamp_seconds=4.0,
                    timestamp_formatted="00:04",
                    score=0.62,
                ),
                SceneCut(
                    index=2,
                    timestamp_seconds=9.5,
                    timestamp_formatted="00:09",
                    score=0.51,
                ),
            ],
            scene_boundaries=[
                SceneBoundary(
                    index=1,
                    start_seconds=0.0,
                    end_seconds=4.0,
                    duration_seconds=4.0,
                ),
                SceneBoundary(
                    index=2,
                    start_seconds=4.0,
                    end_seconds=9.5,
                    duration_seconds=5.5,
                    confidence=0.51,
                ),
                SceneBoundary(
                    index=3,
                    start_seconds=9.5,
                    end_seconds=12.5,
                    duration_seconds=3.0,
                    confidence=0.62,
                ),
            ],
            scene_threshold=0.35,
            audio_energy=audio_energy,
        ),
    )
    result = _ingest_and_analyze(store, tmp_path)
    invocations = store.list_tool_invocations(result.artifact.run_id)
    session.close()

    assert result is not None
    source_report = result.report["source_assets"][0]
    # Scene: detector output tiles the source; the threshold is echoed so the
    # report can be reproduced.
    assert source_report["scene_cut_count"] == 2
    assert source_report["scene_detection_threshold"] == 0.35
    assert source_report["scene_cuts"][0]["timestamp_seconds"] == 4.0
    scenes = result.report["scenes"]
    assert len(scenes) == 3
    assert scenes[0]["detection_method"] == "ffmpeg_scene_detection"
    assert scenes[0]["start_seconds"] == 0.0 and scenes[-1]["end_seconds"] == 12.5
    assert scenes[1]["confidence"] == 0.51
    # The frame sits at t=0, so only the first boundary holds it.
    assert scenes[0]["representative_frame_asset_ids"]
    assert not scenes[1]["representative_frame_asset_ids"]
    # Audio: the report block keeps the statistics and bounded segments.
    assert source_report["audio_energy"]["mean_rms_db"] == -20.0
    assert source_report["audio_energy"]["silence_ratio"] == 0.24
    assert len(source_report["audio_energy"]["segments"]) == 2
    summary = result.report["media_summary"]
    assert summary["scene_cut_count"] == 2
    assert summary["scene_boundary_count"] == 3
    assert summary["audio"]["analyzed_source_count"] == 1
    assert summary["audio"]["peak_rms_db"] == -6.0
    # Transcript: still honest — no key, no override, no fabricated text.
    assert result.report["transcript"]["status"] == "unavailable"
    assert result.report["transcript"]["reason"]
    assert summary["has_transcript"] is False
    # Brief: the pending-boundary limitation disappears once boundaries exist.
    brief = result.brief
    assert brief["clip_planning_inputs"]["scene_boundary_available"] is True
    assert brief["clip_planning_inputs"]["candidate_windows"][0]["basis"] == (
        "ffmpeg_scene_detection"
    )
    assert all(
        limitation["code"] != "scene_boundary_detection_pending"
        for limitation in brief["limitations"]
    )
    assert any(
        invocation.tool_key == "ffmpeg_media_probe" for invocation in invocations
    )


def test_database_store_media_analysis_transcript_uses_reachable_audio_url(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'v11-asr.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)

    class FakeAsrAdapter:
        provider_key = "fake-asr"
        tool_key = "fake_asr"

        def check_availability(self) -> bool:
            return True

        def transcribe(self, payload: dict) -> dict:
            assert payload["audio_url"] == "https://example.com/audio.mp4"
            return {
                "text": "大家好，欢迎观看本期视频。",
                "segments": [],
                "model": "qwen3-asr-flash",
                "raw_response": {},
            }

    store = DatabaseStore(session, media_analyzer=FakeMediaAnalyzer(), asr_adapter=FakeAsrAdapter())
    result = _ingest_and_analyze(
        store,
        tmp_path,
        request_overrides={
            "provider_options": {"asr": {"audio_url": "https://example.com/audio.mp4"}}
        },
    )
    invocations = store.list_tool_invocations(result.artifact.run_id)
    session.close()

    assert result is not None
    transcript = result.report["transcript"]
    assert transcript["status"] == "available"
    assert transcript["engine"] == "fake-asr"
    assert transcript["text"] == "大家好，欢迎观看本期视频。"
    assert transcript["source_asset_id"]
    summary = result.report["media_summary"]
    assert summary["has_transcript"] is True
    assert summary["transcript_engine"] == "fake-asr"
    assert result.brief["clip_planning_inputs"]["transcript_available"] is True
    assert any(invocation.tool_key == "fake_asr" for invocation in invocations)
    # The success note rides with the analysis notes, not silently.
    assert any(
        note.get("code") == "transcript_available" for note in result.report["analysis_notes"]
    )


def test_database_store_media_analysis_transcript_disabled_by_request(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'v11-asr-off.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(session, media_analyzer=FakeMediaAnalyzer())
    result = _ingest_and_analyze(
        store,
        tmp_path,
        request_overrides={"transcribe_audio": False},
    )
    session.close()

    assert result is not None
    transcript = result.report["transcript"]
    assert transcript["status"] == "unavailable"
    assert "disabled by request" in transcript["reason"]


def test_database_store_creates_script_screening_room_run(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'script-screening-room.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Script screening room"))

    result = store.create_script_screening_room_run(
        project_id=project.project_id,
        pipeline=pipeline,
        payload=ScriptScreeningRunCreateRequest(
            raw_script="""
【茶小朵】抖音 · 7s · 口播风格分镜脚本
主题：秋天第一杯奶茶
钩子：秋天第一杯奶茶，还没人给你点？

镜头 01｜3s｜近景｜节奏快
画面：女生坐在宿舍桌前，对着镜头举起奶茶杯。
台词：秋天第一杯奶茶，还没人给你点？
字幕：秋天第一杯奶茶 还没人给你点？
配乐：轻快俏皮卡点音效起

镜头 02｜4s｜特写｜节奏中
画面：撕开茶小朵奶茶包装，热水注入杯中，热气升腾。
台词：别等啦！自己冲就好！
字幕：自己冲就好！
配乐：温馨吉他BGM
""",
            pipeline_key="avatar_spokesperson",
        ),
    )
    assert result is not None
    artifacts = store.list_artifacts(result.run.run_id, current_only=True)
    events = store.list_events(result.run.run_id, event_type="screening_room.run_created")
    session.close()

    assert result.run.pipeline_key == "avatar_spokesperson"
    assert result.scene_plan_artifact.artifact_type == "screening_room_scene_plan"
    assert result.scene_plan_artifact.schema_key == "screening_room_scene_plan.v1"
    assert result.spoken_script_artifact.artifact_type == "spoken_script"
    assert result.shot_list_artifact.artifact_type == "shot_list"
    assert result.parse_result.scene_plan["title"] == "茶小朵"
    assert result.parse_result.scene_plan["target_duration_seconds"] == 7
    assert result.parse_result.scene_plan["shots"][0]["camera"] == "近景"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert {"screening_room_scene_plan", "spoken_script", "shot_list"}.issubset(
        current_payloads
    )
    assert current_payloads["spoken_script"]["segments"][1]["text"] == "别等啦！自己冲就好！"
    assert current_payloads["shot_list"]["shots"][0]["spoken_segment_ids"] == ["seg_01"]
    assert events is not None
    assert len(events) == 1
    assert events[0].payload["shot_count"] == 2


def test_database_store_applies_default_style_preset_snapshot_to_run(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'brand-style.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Brand style project"))

    brand_kit = store.create_brand_kit(
        project.project_id,
        BrandKitCreate(
            name="茶小朵品牌规范",
            color_palette={
                "primary": "#E9472E",
                "secondary": "#F7C948",
            },
            typography={"caption_font": "Noto Sans CJK SC"},
            safe_area={"top": 120, "bottom": 180},
            brand_voice={"tone": ["温暖", "俏皮"]},
            banned_terms=["最健康", "包治"],
        ),
    )
    assert brand_kit is not None
    style_preset = store.create_style_preset(
        project.project_id,
        StylePresetCreate(
            name="抖音口播默认样式",
            brand_kit_id=brand_kit.brand_kit_id,
            style_key="douyin_warm_spokesperson",
            is_default=True,
            aspect_ratio="9:16",
            subtitle_style={"placement": "bottom", "font_size": 56},
            visual_style={"background": "warm dorm room", "lighting": "soft"},
            tone={"keywords": ["香浓", "秋日", "轻松"]},
            guardrails={"banned_terms": ["最健康", "包治"]},
        ),
    )
    assert style_preset is not None

    refreshed_project = store.get_project(project.project_id)
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    explicit_style_run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="显式 style_key 保持优先。",
            style_key="manual_override",
        ),
        pipeline=pipeline,
    )
    session.close()

    assert refreshed_project is not None
    assert started is not None
    assert artifacts is not None
    assert refreshed_project.default_style_preset_id == style_preset.style_preset_id
    snapshot = run.input_payload["style_preset_snapshot"]
    assert snapshot["style_preset"]["style_preset_id"] == style_preset.style_preset_id
    assert snapshot["style_preset"]["subtitle_style"]["font_size"] == 56
    assert snapshot["brand_kit"]["brand_kit_id"] == brand_kit.brand_kit_id
    assert snapshot["brand_kit"]["color_palette"]["primary"] == "#E9472E"
    assert run.input_payload["style_key"] == "douyin_warm_spokesperson"
    production_plan = next(
        artifact.payload for artifact in artifacts if artifact.artifact_type == "production_plan"
    )
    assert production_plan["creative_strategy"]["style_preset"]["style_key"] == (
        "douyin_warm_spokesperson"
    )
    assert production_plan["creative_strategy"]["brand_constraints"]["banned_terms"] == [
        "包治",
        "最健康",
    ]
    assert explicit_style_run.input_payload["style_key"] == "manual_override"
    assert explicit_style_run.input_payload["style_preset_snapshot"]["style_preset"][
        "style_key"
    ] == "douyin_warm_spokesperson"


def test_database_store_builds_proposal_packet_before_generation(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'proposal-packet.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Proposal packet project"))
    brand_kit = store.create_brand_kit(
        project.project_id,
        BrandKitCreate(
            name="茶小朵",
            color_palette={"primary": "#E9472E", "secondary": "#F7C948"},
            typography={"caption_font": "Noto Sans CJK SC"},
            banned_terms=["最健康"],
        ),
    )
    assert brand_kit is not None
    style_preset = store.create_style_preset(
        project.project_id,
        StylePresetCreate(
            name="秋日口播",
            brand_kit_id=brand_kit.brand_kit_id,
            style_key="autumn_spokesperson",
            is_default=True,
            aspect_ratio="9:16",
            subtitle_style={"placement": "bottom", "font_size": 56},
            visual_style={"background": "warm dorm room", "lighting": "soft"},
            tone={"keywords": ["温暖", "俏皮"]},
        ),
    )
    assert style_preset is not None
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=15,
        ),
        pipeline=pipeline,
    )

    artifact = store.build_proposal_packet(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id, event_type="run.proposal_packet_created")
    session.close()

    assert artifact is not None
    assert artifact.artifact_type == "proposal_packet"
    assert artifact.schema_key == "proposal_packet.v1"
    assert artifact.producer == "proposal_packet_builder"
    assert artifact.lineage is not None
    assert artifact.lineage["source"] == "clean_room_proposal_kit"
    assert len(artifact.payload["options"]) == 3
    option_ids = {option["option_id"] for option in artifact.payload["options"]}
    assert artifact.payload["recommended_option_id"] in option_ids
    assert artifact.payload["budget_summary"]["currency"] == "USD"
    assert artifact.payload["timeline_summary"]["max_minutes"] >= 1
    assert artifact.payload["approval_gate"]["required_before"] == "asset_generation"
    assert artifact.payload["style_preview"]["style_key"] == "autumn_spokesperson"
    assert artifact.payload["style_preview"]["colors"]["primary"] == "#E9472E"
    assert artifact.payload["style_preview"]["subtitle_style"]["font_size"] == 56
    assert artifacts is not None
    assert {"preflight_report", "execution_plan", "proposal_packet"}.issubset(
        {current.artifact_type for current in artifacts}
    )
    assert events is not None
    assert len(events) == 1
    assert events[0].payload["option_count"] == 3


def test_database_store_screen_demo_smoke_reaches_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'screen-demo.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("screen_demo")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Screen demo"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="screen_demo",
            raw_script="演示脚本放映室如何把脚本变成视频。",
            target_duration_seconds=45,
            target_platform="internal_training",
            aspect_ratio="16:9",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    demo_plan_reviewed = store.review_stage(
        run.run_id,
        "demo_plan",
        ApprovalRequest(decision="approved"),
    )
    recording_plan_reviewed = store.review_stage(
        run.run_id,
        "recording_plan",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == "screen_demo"
    assert started.current_stage_key == "demo_plan"
    assert demo_plan_reviewed is not None
    assert demo_plan_reviewed.current_stage_key == "recording_plan"
    assert recording_plan_reviewed is not None
    assert recording_plan_reviewed.status == RunStatus.WAITING_REVIEW
    assert recording_plan_reviewed.current_stage_key == "final_review"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert current_payloads["demo_flow_plan"] == {
        "stage_key": "demo_plan",
        "status": "placeholder",
    }
    assert current_payloads["recording_plan"] == {
        "stage_key": "recording_plan",
        "status": "placeholder",
    }
    assert current_payloads["screen_capture_manifest"] == {
        "stage_key": "capture",
        "status": "placeholder",
    }
    assert current_payloads["interaction_annotation_plan"] == {
        "stage_key": "annotation",
        "status": "placeholder",
    }
    assert current_payloads["render_summary"] == {
        "stage_key": "compose",
        "status": "placeholder",
    }
    assert "publish_package" not in current_payloads


def test_database_store_localization_dub_smoke_reaches_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'localization-dub.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("localization_dub")
    # The default catalog only ships provider-backed TTS (env-keyed), so a bare
    # store would honestly block the dub stage. The smoke test exercises the run
    # flow, so it injects a local degraded TTS instead of provider keys.
    dub_catalog_path = tmp_path / "dub-tools.json"
    dub_catalog_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool_key": "rule_based_planner",
                        "display_name": "Rule-Based Planner",
                        "capability": "llm_generation",
                        "provider": "internal",
                        "runtime_type": "local",
                    },
                    {
                        "tool_key": "ffmpeg_media_probe",
                        "display_name": "FFmpeg Media Probe",
                        "capability": "media_analysis",
                        "provider": "ffmpeg",
                        "runtime_type": "local",
                        "required_binaries": ["ffmpeg", "ffprobe"],
                    },
                    {
                        "tool_key": "local_transcript_placeholder",
                        "display_name": "Local Transcript Placeholder",
                        "capability": "speech_to_text",
                        "provider": "internal",
                        "runtime_type": "local",
                        "status": "degraded",
                    },
                    {
                        "tool_key": "local_tts_placeholder",
                        "display_name": "Local TTS Placeholder",
                        "capability": "tts",
                        "provider": "internal",
                        "runtime_type": "local",
                        "status": "degraded",
                    },
                    {
                        "tool_key": "local_srt_subtitles",
                        "display_name": "Local SRT Subtitle Generator",
                        "capability": "subtitle_generation",
                        "provider": "internal",
                        "runtime_type": "local",
                    },
                    {
                        "tool_key": "ffmpeg_compose",
                        "display_name": "FFmpeg Compose",
                        "capability": "video_composition",
                        "provider": "ffmpeg",
                        "runtime_type": "local",
                        "required_binaries": ["ffmpeg"],
                    },
                    {
                        "tool_key": "local_object_storage",
                        "display_name": "Local Object Storage",
                        "capability": "storage",
                        "provider": "local",
                        "runtime_type": "local",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    dub_registry = ToolRegistry(
        catalog_paths=[dub_catalog_path],
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg"
        if binary in {"ffmpeg", "ffprobe"}
        else None,
    )
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=dub_registry)
    project = store.create_project(ProjectCreate(title="Localization dub"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="localization_dub",
            raw_script="把这条视频本地化成英文版。",
            target_duration_seconds=60,
            target_platform="bilibili",
            aspect_ratio="16:9",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    localize_plan_reviewed = store.review_stage(
        run.run_id,
        "localize_plan",
        ApprovalRequest(decision="approved"),
    )
    translate_reviewed = store.review_stage(
        run.run_id,
        "translate",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == "localization_dub"
    assert started.current_stage_key == "localize_plan"
    assert localize_plan_reviewed is not None
    assert localize_plan_reviewed.current_stage_key == "translate"
    assert translate_reviewed is not None
    assert translate_reviewed.status == RunStatus.WAITING_REVIEW
    assert translate_reviewed.current_stage_key == "final_review"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert current_payloads["media_analysis_report"] == {
        "stage_key": "analyze",
        "status": "placeholder",
    }
    assert current_payloads["localization_plan"] == {
        "stage_key": "localize_plan",
        "status": "placeholder",
    }
    assert current_payloads["spoken_script"] == {
        "stage_key": "translate",
        "status": "placeholder",
    }
    assert current_payloads["dubbing_manifest"] == {
        "stage_key": "dub",
        "status": "placeholder",
    }
    assert current_payloads["edit_plan"] == {
        "stage_key": "edit",
        "status": "placeholder",
    }
    assert current_payloads["render_summary"] == {
        "stage_key": "compose",
        "status": "placeholder",
    }
    assert "publish_package" not in current_payloads


def test_database_store_hybrid_story_smoke_reaches_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'hybrid-story.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("hybrid_story")
    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Hybrid story"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="hybrid_story",
            raw_script="用我拍的素材讲一个周末露营的故事。",
            target_duration_seconds=45,
            target_platform="xiaohongshu",
            aspect_ratio="9:16",
        ),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    story_plan_reviewed = store.review_stage(
        run.run_id,
        "story_plan",
        ApprovalRequest(decision="approved"),
    )
    storyboard_reviewed = store.review_stage(
        run.run_id,
        "visual_plan",
        ApprovalRequest(decision="approved"),
    )
    asset_review_reviewed = store.review_stage(
        run.run_id,
        "asset_review",
        ApprovalRequest(decision="approved"),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert started is not None
    assert started.pipeline_key == "hybrid_story"
    assert started.current_stage_key == "story_plan"
    assert story_plan_reviewed is not None
    assert story_plan_reviewed.current_stage_key == "visual_plan"
    assert storyboard_reviewed is not None
    assert storyboard_reviewed.current_stage_key == "asset_review"
    assert asset_review_reviewed is not None
    assert asset_review_reviewed.status == RunStatus.WAITING_REVIEW
    assert asset_review_reviewed.current_stage_key == "final_review"
    assert artifacts is not None
    current_payloads = {artifact.artifact_type: artifact.payload for artifact in artifacts}
    assert current_payloads["story_outline"] == {
        "stage_key": "story_plan",
        "status": "placeholder",
    }
    assert current_payloads["shot_list"] == {
        "stage_key": "visual_plan",
        "status": "placeholder",
    }
    assert current_payloads["source_asset_manifest"] == {
        "stage_key": "ingest",
        "status": "placeholder",
    }
    assert current_payloads["asset_manifest"] == {
        "stage_key": "supplement",
        "status": "placeholder",
    }
    assert current_payloads["edit_plan"] == {
        "stage_key": "edit",
        "status": "placeholder",
    }
    assert current_payloads["render_summary"] == {
        "stage_key": "compose",
        "status": "placeholder",
    }
    assert "publish_package" not in current_payloads


def test_database_store_run_state_actions_pause_resume_and_cancel(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'run-state-actions.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="State action run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="状态机测试"),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    assert started is not None
    job = store.background_jobs.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(
            stage_run_id=started.stages[0].stage_run_id,
            job_type="stage_tool_step",
            request_payload={"manifest_step_key": "test.step"},
        ),
    )
    session.commit()

    paused = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action="pause", reason="manual hold"),
    )
    resumed = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action="resume", reason="continue"),
    )
    cancelled = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action="cancel", reason="stop run"),
    )
    cancelled_job = store.get_background_job(job.job_id)
    events = store.list_events(run.run_id)
    session.close()

    assert paused is not None
    assert paused.run.status == RunStatus.PAUSED
    assert resumed is not None
    assert resumed.run.status == RunStatus.WAITING_REVIEW
    assert resumed.run.current_stage_key == "plan"
    assert cancelled is not None
    assert cancelled.run.status == RunStatus.CANCELLED
    assert cancelled_job is not None
    assert cancelled_job.status == BackgroundJobStatus.CANCELLED
    assert events is not None
    event_types = [event.event_type for event in events]
    assert "run.paused" in event_types
    assert "run.resumed" in event_types
    assert "run.cancelled" in event_types
    assert "job.cancelled" in event_types


def test_database_store_run_state_actions_rerun_stage_and_artifact(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rerun-state-actions.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Rerun state action run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="重跑状态机测试"),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    assert started is not None

    rerun_stage = store.run_state_action(
        run.run_id,
        RunStateActionRequest(action="rerun_stage", stage_key="intake", reason="refresh brief"),
    )
    rerun_artifact = store.run_state_action(
        run.run_id,
        RunStateActionRequest(
            action="rerun_artifact",
            artifact_type="production_plan",
            reason="refresh plan",
        ),
    )
    artifacts = store.list_artifacts(run.run_id)
    events = store.list_events(run.run_id)
    session.close()

    assert rerun_stage is not None
    assert rerun_stage.run.status == RunStatus.WAITING_REVIEW
    assert rerun_stage.run.current_stage_key == "plan"
    assert rerun_artifact is not None
    assert rerun_artifact.run.status == RunStatus.WAITING_REVIEW
    assert rerun_artifact.run.current_stage_key == "plan"
    assert artifacts is not None
    intake_versions = [
        artifact.version for artifact in artifacts if artifact.artifact_type == "intake_brief"
    ]
    plan_versions = [
        artifact.version for artifact in artifacts if artifact.artifact_type == "production_plan"
    ]
    assert max(intake_versions) == 2
    assert max(plan_versions) == 3
    assert events is not None
    assert [event.event_type for event in events].count("run.rerun_requested") == 2


def test_database_store_run_state_action_skips_optional_final_review(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'skip-state-actions.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Skip state action run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="跳过终审测试"),
        pipeline=pipeline,
    )
    started = store.start_run(run.run_id)
    assert started is not None
    with pytest.raises(ValueError, match="stage is not skippable"):
        store.run_state_action(
            run.run_id,
            RunStateActionRequest(action="skip_optional_stage", stage_key="plan", reason="no"),
        )

    for stage_key in ["plan", "script", "storyboard", "asset_review"]:
        reviewed = store.review_stage(
            run.run_id,
            stage_key,
            ApprovalRequest(decision="approved"),
        )
        assert reviewed is not None
    before_skip = store.get_run(run.run_id)
    assert before_skip is not None
    assert before_skip.status == RunStatus.WAITING_REVIEW
    assert before_skip.current_stage_key == "final_review"

    skipped = store.run_state_action(
        run.run_id,
        RunStateActionRequest(
            action="skip_optional_stage",
            stage_key="final_review",
            reason="internal QA already approved rendered video",
        ),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id)
    session.close()

    assert skipped is not None
    assert skipped.run.status == RunStatus.COMPLETED
    assert skipped.run.current_stage_key is None
    assert artifacts is not None
    current_artifacts = {artifact.artifact_type: artifact for artifact in artifacts}
    assert current_artifacts["review_notes"].stage_key == "final_review"
    assert current_artifacts["review_notes"].payload["status"] == "skipped"
    assert current_artifacts["publish_package"].payload["status"] == "placeholder"
    assert events is not None
    event_types = [event.event_type for event in events]
    assert "stage.skipped" in event_types
    assert "run.stage_skipped" in event_types


def test_database_store_uses_run_pipeline_snapshot_after_creation(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline-snapshot.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Snapshot test"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试 pipeline 快照"),
        pipeline=pipeline,
    )

    store.pipeline_loader = FailingPipelineLoader()
    started = store.start_run(run.run_id)
    session.close()

    assert started is not None
    assert started.pipeline_version == pipeline.version
    assert started.pipeline_dsl_version == "1.0"
    assert started.status == RunStatus.WAITING_REVIEW
    assert started.current_stage_key == "plan"


def test_database_store_can_filter_current_artifacts(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'current-artifacts.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Versioned artifacts"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试 artifact 版本"),
        pipeline=pipeline,
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="plan",
        artifact_type="custom_revision",
        payload={"revision": 1},
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="plan",
        artifact_type="custom_revision",
        payload={"revision": 2},
    )
    session.commit()

    all_artifacts = store.list_artifacts(run.run_id)
    current_artifacts = store.list_artifacts(run.run_id, current_only=True)
    session.close()

    assert all_artifacts is not None
    assert current_artifacts is not None
    assert [artifact.version for artifact in all_artifacts] == [1, 2]
    assert [artifact.is_current for artifact in all_artifacts] == [False, True]
    assert [artifact.version for artifact in current_artifacts] == [2]
    assert current_artifacts[0].payload == {"revision": 2}


def test_database_store_records_automatic_and_tool_events(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Evented run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试事件日志"),
        pipeline=pipeline,
    )
    store.start_run(run.run_id)
    tool_event = store.record_event(
        run.run_id,
        EventCreate(
            event_type="tool.completed",
            source_type=EventSourceType.TOOL,
            source_id="tool_demo",
            tool_invocation_id="tool_demo",
            payload={"tool_key": "demo_tool"},
        ),
    )
    events = store.list_events(run.run_id)
    tool_events = store.list_events(run.run_id, source_type=EventSourceType.TOOL)
    session.close()

    assert tool_event is not None
    assert events is not None
    assert tool_events is not None
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert "run.created" in {event.event_type for event in events}
    assert "run.started" in {event.event_type for event in events}
    assert "stage.started" in {event.event_type for event in events}
    assert "stage.waiting_review" in {event.event_type for event in events}
    assert [event.event_type for event in tool_events] == ["tool.completed"]


def test_database_store_uploads_downloads_and_signs_asset_links(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'assets.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Asset run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试资产上传"),
        pipeline=pipeline,
    )
    content = b"asset bytes"
    asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="image",
            filename="demo.png",
            content_base64=base64.b64encode(content).decode(),
            mime_type="image/png",
            asset_metadata={"scene": "cover"},
        ),
    )

    assert asset is not None
    assert asset.file_size == len(content)
    assert asset.mime_type == "image/png"
    assert asset.asset_metadata["filename"] == "demo.png"
    assert asset.asset_metadata["scene"] == "cover"

    listed_assets = store.list_assets(run.run_id)
    downloaded = store.download_asset(asset.asset_id)
    link = store.create_asset_access_link(asset.asset_id, expires_in_seconds=60)
    assert listed_assets is not None
    assert downloaded is not None
    assert link is not None
    assert [stored_asset.asset_id for stored_asset in listed_assets] == [asset.asset_id]
    assert downloaded[1] == content

    expires = int(link.url.split("expires=")[1].split("&")[0])
    signature = link.url.split("signature=")[1]
    resolved = store.resolve_asset_access_link(asset.asset_id, expires=expires, signature=signature)
    session.close()

    assert resolved is not None
    assert resolved[0].asset_id == asset.asset_id
    assert resolved[1] == content


def test_database_store_submits_polls_and_archives_avatar_task(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    transport = FakeDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)

    session = Session(engine)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Avatar run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="数字人口播测试"),
        pipeline=pipeline,
    )

    invocation = store.submit_avatar_task(
        run.run_id,
        AvatarVideoSubmitRequest(
            shot_id="shot_01",
            image_url="https://example.com/person.png",
            audio_url="https://example.com/voice.wav",
            face_bbox=[10, 20, 110, 160],
            ext_bbox=[0, 0, 180, 220],
        ),
    )
    assert invocation is not None
    assert invocation.status.value == "submitted"
    assert invocation.external_task_id == "task_avatar_1"

    polled = store.poll_avatar_task(invocation.invocation_id)
    assert polled is not None
    assert polled.status.value == "succeeded"
    assert polled.response_payload["video_url"] == "https://example.com/avatar.mp4"

    archived = store.archive_avatar_task(
        invocation.invocation_id,
        AvatarVideoArchiveRequest(
            filename="shot-01.mp4",
            video_content_base64=base64.b64encode(b"video bytes").decode(),
        ),
    )
    assets = store.list_assets(run.run_id)
    events = store.list_events(run.run_id, source_type=EventSourceType.TOOL)
    session.close()

    assert archived is not None
    archived_invocation, asset = archived
    assert archived_invocation.status.value == "archived"
    assert asset.asset_type == "avatar_video_clip"
    assert asset.provider == "dashscope"
    assert asset.tool_key == "dashscope_avatar_video"
    assert store.storage.read_bytes(asset.storage_url) == b"video bytes"
    assert assets is not None
    assert [stored_asset.asset_id for stored_asset in assets] == [asset.asset_id]
    assert events is not None
    assert [event.event_type for event in events] == [
        "tool.submitted",
        "tool.polled",
        "tool.archived",
    ]


def test_database_store_poll_avatar_failure_has_error_taxonomy(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-failed.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    transport = FailingDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)

    session = Session(engine)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    project = store.create_project(ProjectCreate(title="Failed avatar run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="数字人失败测试"),
        pipeline=pipeline,
    )

    invocation = store.submit_avatar_task(
        run.run_id,
        AvatarVideoSubmitRequest(
            image_url="https://example.com/person.png",
            audio_url="https://example.com/voice.wav",
            face_bbox=[10, 20, 110, 160],
            ext_bbox=[0, 0, 180, 220],
        ),
    )
    assert invocation is not None

    polled = store.poll_avatar_task(invocation.invocation_id)
    session.close()

    assert polled is not None
    assert polled.status == ToolInvocationStatus.FAILED
    assert polled.error_code == "InvalidParameter.Ratio"
    assert polled.error_message == "The request parameter is invalid"
    assert polled.error_category == "input"
    assert polled.error_taxonomy_code == "input.invalid_or_missing"
    assert polled.error_retryable is False
    assert polled.error_user_message == "输入参数、素材引用或当前状态不满足要求。"
    assert "修正参数" in (polled.error_remediation or "")


def test_database_store_generates_tts_subtitles_and_music_assets(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'audio.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(
        session,
        tts_adapter=FakeTtsAdapter(),
        music_adapter=FakeMusicAdapter(),
    )
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Audio run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="音频和字幕测试"),
        pipeline=pipeline,
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="script",
        artifact_type="spoken_script",
        payload={
            "segments": [
                {
                    "segment_id": "line_01",
                    "start_seconds": 0,
                    "end_seconds": 3,
                    "text": "秋天第一杯奶茶，还没人给你点？",
                    "caption": "秋天第一杯奶茶 还没人给你点？",
                },
                {
                    "segment_id": "line_02",
                    "start_seconds": 3,
                    "end_seconds": 7,
                    "text": "别等啦！自己冲就好！",
                    "caption": "自己冲就好！",
                },
            ]
        },
    )
    session.commit()

    tts_result = store.generate_tts_audio(
        run.run_id,
        TtsGenerateRequest(
            segment_id="line_01",
            text="秋天第一杯奶茶，还没人给你点？",
            filename="line-01.wav",
        ),
    )
    subtitle_result = store.generate_subtitles(
        run.run_id,
        SubtitleGenerateRequest(filename="subtitles.srt"),
    )
    music_result = store.generate_music(
        run.run_id,
        MusicGenerateRequest(
            prompt="Warm guitar background music, no vocals",
            music_length_ms=15000,
        ),
    )
    assets = store.list_assets(run.run_id)
    events = store.list_events(run.run_id, source_type=EventSourceType.TOOL)
    usage_summary = store.get_usage_summary(run.run_id)

    assert tts_result is not None
    assert subtitle_result is not None
    assert music_result is not None
    assert store.storage.read_bytes(tts_result[1].storage_url) == b"voice bytes"
    assert store.storage.read_bytes(music_result[1].storage_url) == b"music bytes"
    assert "00:00:00,000 --> 00:00:03,000" in store.storage.read_bytes(
        subtitle_result[1].storage_url
    ).decode()
    assert assets is not None
    assert sorted(asset.asset_type for asset in assets) == sorted(
        [
        "tts_audio",
        "subtitle_file",
        "background_music",
        ]
    )
    assert events is not None
    assert [event.event_type for event in events[-3:]] == [
        "tool.completed",
        "tool.completed",
        "tool.completed",
    ]
    assert usage_summary is not None
    assert usage_summary.ledger_entry_count == 3
    assert usage_summary.asset_bytes >= len(b"voice bytes") + len(b"music bytes")
    assert usage_summary.by_tool_key["dashscope_tts"]["ledger_entry_count"] == 1
    assert usage_summary.by_source_type["tool_invocation"]["ledger_entry_count"] == 3
    session.close()


def test_database_store_runs_manifest_actions_for_repair_inputs(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'manifest-actions.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(
        session,
        image_adapter=FakeImageAdapter(),
        tts_adapter=FakeTtsAdapter(),
    )
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Manifest action run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
        ),
        pipeline=pipeline,
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="script",
        artifact_type="spoken_script",
        payload={
            "segments": [
                {
                    "segment_id": "line_01",
                    "start_seconds": 0,
                    "end_seconds": 3,
                    "text": "秋天第一杯奶茶，还没人给你点？",
                    "caption": "秋天第一杯奶茶 还没人给你点？",
                }
            ]
        },
    )
    session.commit()

    image_result = store.run_manifest_action(
        run.run_id,
        ManifestActionRequest(
            action="generate_person_image",
            stage_key="asset_generation",
            manifest_step_key="avatar_step",
            payload={"filename": "person.png"},
        ),
    )
    tts_result = store.run_manifest_action(
        run.run_id,
        ManifestActionRequest(
            action="generate_tts",
            stage_key="asset_generation",
            manifest_step_key="avatar_step",
            payload={"voice": "Cherry"},
        ),
    )
    upload_result = store.run_manifest_action(
        run.run_id,
        ManifestActionRequest(
            action="upload_person_image",
            stage_key="asset_generation",
            manifest_step_key="avatar_step",
            payload={
                "filename": "uploaded-person.png",
                "content_base64": base64.b64encode(b"person image").decode("ascii"),
                "mime_type": "image/png",
            },
        ),
    )
    assets = store.list_assets(run.run_id)
    events = store.list_events(run.run_id, source_type=EventSourceType.SYSTEM)
    session.close()

    assert image_result is not None
    assert image_result.result_payload["asset"]["asset_metadata"]["role"] == "person_image"
    assert image_result.result_payload["asset"]["asset_metadata"]["manifest_step_key"] == (
        "avatar_step"
    )
    assert tts_result is not None
    assert tts_result.result_payload["asset"]["asset_type"] == "tts_audio"
    assert tts_result.result_payload["asset"]["asset_metadata"]["voice"] == "Cherry"
    assert upload_result is not None
    assert upload_result.result_payload["asset"]["asset_metadata"]["role"] == "person_image"
    assert assets is not None
    assert [asset.asset_type for asset in assets].count("reference_image") == 2
    assert [asset.asset_type for asset in assets].count("tts_audio") == 1
    assert events is not None
    assert [event.event_type for event in events].count("manifest_action.executed") == 3


def test_database_store_blocks_provider_call_when_cost_budget_would_be_exceeded(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'usage-budget.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session, image_adapter=CostlyImageAdapter())
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Budget run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="预算门禁测试",
            cost_budget=0.5,
        ),
        pipeline=pipeline,
    )

    with pytest.raises(RunGateError) as exc_info:
        store.generate_image(
            run.run_id,
            ImageGenerateRequest(
                prompt="front-facing product spokesperson",
                filename="person.png",
            ),
        )
    assert exc_info.value.code == "cost_budget_exceeded"
    assert exc_info.value.details["budget_limit"] == 0.5
    assert exc_info.value.details["additional_estimated_cost"] == 1.25
    assert exc_info.value.details["projected_cost"] == 1.25
    usage_summary = store.get_usage_summary(run.run_id)
    invocations = store.list_tool_invocations(run.run_id)
    session.close()

    assert usage_summary is not None
    assert usage_summary.budget_limit == 0.5
    assert usage_summary.budget_status == "ok"
    assert usage_summary.ledger_entry_count == 0
    assert invocations == []


def test_database_store_submits_avatar_task_from_assets(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-from-assets.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    transport = FakeDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Avatar from assets"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试数字人生成"),
        pipeline=pipeline,
    )
    image_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
        ),
    )
    audio_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
        ),
    )

    assert image_asset is not None
    assert audio_asset is not None
    invocation = store.submit_avatar_task_from_assets(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_asset_id=image_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url="https://studio.example",
            auto_archive=False,
            auto_compose=False,
            # The request body carries `model`, and that is what picks the
            # dialect — the adapter's own default is only a fallback. Pin EMO
            # here so the detect-then-inject path stays covered.
            model="emo-v1",
        ),
    )
    session.close()

    assert invocation is not None
    assert invocation.external_task_id == "task_avatar_1"
    request_input = transport.calls[-1]["payload"]["input"]
    assert request_input["image_url"].startswith(
        f"https://studio.example/api/assets/{image_asset.asset_id}/content?"
    )
    assert request_input["audio_url"].startswith(
        f"https://studio.example/api/assets/{audio_asset.asset_id}/content?"
    )
    assert invocation.response_payload["resolved_inputs"]["person_image_asset_id"] == (
        image_asset.asset_id
    )
    assert invocation.response_payload["resolved_inputs"]["audio_asset_id"] == audio_asset.asset_id
    assert invocation.response_payload["detected_bbox"]["face_bbox"] == [10, 20, 110, 160]


def test_database_store_submits_wan_s2v_without_the_detection_hop(tmp_path) -> None:
    """On the default model no face-detect call is made and no bbox is sent.

    The same wiring as above, but the extra provider round trip would be pure
    waste here: s2v reads the raw image and refuses a bbox it did not ask for.
    """

    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-s2v.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    transport = FakeDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Wanxiang avatar"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="万相数字人生成"),
        pipeline=pipeline,
    )
    image_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
        ),
    )
    audio_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
        ),
    )

    assert image_asset is not None
    assert audio_asset is not None
    invocation = store.submit_avatar_task_from_assets(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_asset_id=image_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url="https://studio.example",
            auto_archive=False,
            auto_compose=False,
        ),
    )
    session.close()

    assert invocation is not None
    assert invocation.response_payload.get("detected_bbox") is None
    assert all(not call["url"].endswith("/face-detect") for call in transport.calls)
    body = transport.calls[-1]["payload"]
    assert body["model"] == "wan2.2-s2v"
    assert set(body["input"]) == {"image_url", "audio_url"}
    assert body["parameters"] == {"resolution": "480P"}


def test_database_store_processes_avatar_background_job(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-job.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    transport = FakeDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Avatar background job"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="后台数字人任务"),
        pipeline=pipeline,
    )
    image_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
        ),
    )
    audio_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
        ),
    )

    assert image_asset is not None
    assert audio_asset is not None
    job = store.enqueue_avatar_from_assets_job(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_asset_id=image_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url="https://studio.example",
            auto_archive=False,
            auto_compose=False,
        ),
    )
    submitted = store.process_background_job(job.job_id if job else "")
    completed = store.process_background_job(job.job_id if job else "")
    events = store.list_events(run.run_id)
    session.close()

    assert job is not None
    assert job.status == BackgroundJobStatus.QUEUED
    assert submitted is not None
    assert submitted.status == BackgroundJobStatus.WAITING_PROVIDER
    assert submitted.progress == 40
    assert submitted.result_payload["external_task_id"] == "task_avatar_1"
    assert completed is not None
    assert completed.status == BackgroundJobStatus.COMPLETED
    assert completed.progress == 100
    assert completed.result_payload["video_url"] == "https://example.com/avatar.mp4"
    assert events is not None
    assert [event.event_type for event in events if event.event_type.startswith("job.")] == [
        "job.queued",
        "job.waiting_provider",
        "job.completed",
    ]


def test_database_store_provider_callback_completes_avatar_background_job_without_polling(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-callback.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    transport = SubmitOnlyDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    project = store.create_project(ProjectCreate(title="Avatar callback job"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="数字人回调测试"),
        pipeline=pipeline,
    )
    job = store.enqueue_avatar_from_assets_job(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_url="https://example.com/person.png",
            audio_url="https://example.com/voice.wav",
            face_bbox=[10, 20, 110, 160],
            ext_bbox=[0, 0, 180, 220],
            auto_archive=False,
            auto_compose=False,
        ),
    )
    submitted = store.process_background_job(job.job_id if job else "")
    callback = store.handle_provider_callback(
        "dashscope",
        ProviderCallbackRequest(
            external_task_id="task_avatar_1",
            status="SUCCEEDED",
            video_url="https://example.com/callback-avatar.mp4",
            raw_payload={
                "output": {
                    "task_status": "SUCCEEDED",
                    "video_url": "https://example.com/callback-avatar.mp4",
                }
            },
        ),
    )
    completed = store.process_background_job(job.job_id if job else "")
    events = store.list_events(run.run_id)
    session.close()

    assert job is not None
    assert submitted is not None
    assert submitted.status == BackgroundJobStatus.WAITING_PROVIDER
    assert callback.matched is True
    assert callback.invocation is not None
    assert callback.invocation.status == ToolInvocationStatus.SUCCEEDED
    assert callback.job is not None
    assert callback.job.status == BackgroundJobStatus.WAITING_PROVIDER
    assert callback.job.progress == 80
    assert callback.job.result_payload["provider_callback_received"] is True
    assert completed is not None
    assert completed.status == BackgroundJobStatus.COMPLETED
    assert completed.result_payload["video_url"] == "https://example.com/callback-avatar.mp4"
    assert [call["method"] for call in transport.calls] == ["POST"]
    assert events is not None
    event_types = [event.event_type for event in events]
    assert "provider.callback_received" in event_types
    assert "job.provider_callback_received" in event_types
    assert "job.completed" in event_types


def test_database_store_provider_callback_failure_marks_job_failed(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'avatar-callback-failed.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    transport = SubmitOnlyDashScopeTransport()
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)
    store = DatabaseStore(session, avatar_video_adapter=adapter)
    project = store.create_project(ProjectCreate(title="Avatar failed callback job"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="数字人失败回调测试"),
        pipeline=pipeline,
    )
    job = store.enqueue_avatar_from_assets_job(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_url="https://example.com/person.png",
            audio_url="https://example.com/voice.wav",
            face_bbox=[10, 20, 110, 160],
            ext_bbox=[0, 0, 180, 220],
            auto_archive=False,
            auto_compose=False,
        ),
    )
    submitted = store.process_background_job(job.job_id if job else "")
    callback = store.handle_provider_callback(
        "dashscope",
        ProviderCallbackRequest(
            external_task_id="task_avatar_1",
            status="FAILED",
            error_code="InvalidParameter.Ratio",
            error_message="The request parameter is invalid",
            raw_payload={
                "output": {
                    "task_status": "FAILED",
                    "code": "InvalidParameter.Ratio",
                    "message": "The request parameter is invalid",
                }
            },
        ),
    )
    events = store.list_events(run.run_id)
    session.close()

    assert job is not None
    assert submitted is not None
    assert submitted.status == BackgroundJobStatus.WAITING_PROVIDER
    assert callback.matched is True
    assert callback.invocation is not None
    assert callback.invocation.status == ToolInvocationStatus.FAILED
    assert callback.invocation.error_code == "InvalidParameter.Ratio"
    assert callback.invocation.error_category == "input"
    assert callback.job is not None
    assert callback.job.status == BackgroundJobStatus.FAILED
    assert callback.job.error_taxonomy_code == "input.invalid_or_missing"
    assert callback.job.result_payload["provider_callback_received"] is True
    assert [call["method"] for call in transport.calls] == ["POST"]
    assert events is not None
    event_types = [event.event_type for event in events]
    assert "provider.callback_received" in event_types
    assert "job.failed" in event_types


def test_database_store_failed_background_job_has_error_taxonomy(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-job.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    project = store.create_project(ProjectCreate(title="Failed background job"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="失败任务测试"),
        pipeline=pipeline,
    )
    job = store.background_jobs.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(job_type="unsupported_job"),
    )
    failed = store.process_background_job(job.job_id)
    events = store.list_events(run.run_id)
    session.close()

    assert failed is not None
    assert failed.status == BackgroundJobStatus.FAILED
    assert failed.error_code == "unsupported_job_type"
    assert failed.error_category == "input"
    assert failed.error_taxonomy_code == "input.invalid_or_missing"
    assert failed.error_retryable is False
    assert failed.error_user_message == "输入参数、素材引用或当前状态不满足要求。"
    assert "修正参数" in (failed.error_remediation or "")
    assert failed.result_payload["error"]["error_category"] == "input"
    assert events is not None
    job_failed = next(event for event in events if event.event_type == "job.failed")
    assert job_failed.payload["error_category"] == "input"
    assert job_failed.payload["error_taxonomy_code"] == "input.invalid_or_missing"


def test_database_store_avatar_manifest_payload_ignores_blank_public_base_url(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'blank-public-base-url.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Blank public base url"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="空 PUBLIC_BASE_URL 测试"),
        pipeline=pipeline,
    )
    image_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
            asset_metadata={"role": "person_image"},
        ),
    )
    audio_asset = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
        ),
    )
    assert image_asset is not None
    assert audio_asset is not None
    monkeypatch.setattr("apps.api.app.db_store.settings.public_base_url", "")

    payload = store._avatar_job_payload_for_manifest_step(
        run=run,
        task={"stage_key": "asset_generation"},
        step={
            "step_id": "avatar_provider",
            "capability": "avatar_video",
            "tool_key": "dashscope_avatar_video",
            "provider": "dashscope",
            "dispatch_target": "background_worker",
        },
        manifest_step_key="manifest:asset_generation:avatar_provider",
    )
    session.close()

    assert payload is not None
    assert payload.request_payload["public_base_url"] is None


def test_database_store_composes_video_and_records_render_summary(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'compose.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    session = Session(engine)
    store = DatabaseStore(
        session,
        compose_worker=FakeComposeWorker(),
        quality_runner=FakeQualityRunner(),
    )
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Compose run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="合成测试"),
        pipeline=pipeline,
    )
    avatar = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="avatar_video_clip",
            filename="avatar.mp4",
            content_base64=base64.b64encode(b"avatar bytes").decode(),
            mime_type="video/mp4",
        ),
    )
    subtitle = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="subtitle_file",
            filename="subtitles.srt",
            content_base64=base64.b64encode(b"1\n00:00:00,000 --> 00:00:01,000\nHi\n").decode(),
            mime_type="application/x-subrip",
        ),
    )
    music = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="background_music",
            filename="music.wav",
            content_base64=base64.b64encode(b"music bytes").decode(),
            mime_type="audio/wav",
        ),
    )

    assert avatar is not None
    assert subtitle is not None
    assert music is not None
    result = store.compose_video(
        run.run_id,
        ComposeVideoRequest(
            avatar_video_asset_id=avatar.asset_id,
            subtitle_asset_id=subtitle.asset_id,
            music_asset_id=music.asset_id,
            filename="final.mp4",
            width=720,
            height=1280,
        ),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id, source_type=EventSourceType.TOOL)

    assert result is not None
    invocation, asset, artifact = result
    assert invocation.tool_key == "ffmpeg_compose"
    assert asset.asset_type == "rendered_video"
    assert asset.duration_ms == 2000
    assert store.storage.read_bytes(asset.storage_url) == b"rendered mp4 bytes"
    assert artifact.artifact_type == "render_summary"
    assert artifact.payload["render_asset_id"] == asset.asset_id
    assert artifact.payload["output"]["width"] == 720
    assert artifact.payload["inputs"]["composition_graph_artifact_id"] is not None
    assert artifact.payload["settings"]["render_profile_key"] == "douyin_vertical"
    assert artifact.payload["settings"]["width"] == 720
    assert artifact.payload["settings"]["video_bitrate"] == "6000k"
    assert artifacts is not None
    graph_artifact = next(
        current for current in artifacts if current.artifact_type == "composition_graph"
    )
    assert graph_artifact.payload["source"]["asset_ids"]["primary_video"] == avatar.asset_id
    assert graph_artifact.payload["render_profile"]["profile_key"] == "douyin_vertical"
    assert graph_artifact.payload["render_targets"][0]["renderer"] == "ffmpeg_compose"
    assert any(current.artifact_type == "render_summary" for current in artifacts)
    assert events is not None
    assert events[-1].event_type == "tool.completed"

    quality_result = store.run_quality_gates(
        run.run_id,
        QualityGateRequest(render_asset_id=asset.asset_id),
    )
    quality_events = store.list_events(run.run_id, event_type="run.quality_report_created")

    assert quality_result is not None
    quality_invocation, quality_report = quality_result
    assert quality_invocation.tool_key == "quality_gate_runner"
    assert quality_report.artifact_type == "quality_report"
    assert quality_report.schema_key == "quality_report.v1"
    assert quality_report.payload["status"] == "warning"
    assert quality_report.payload["quality_score"] == 92
    assert quality_report.payload["inputs"]["render_asset_id"] == asset.asset_id
    assert quality_report.payload["human_review"]["required"] is True
    assert quality_events is not None
    assert quality_events[-1].payload["human_review_required"] is True
    session.close()


def test_database_store_runs_edit_tool_and_records_report(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'edit-tools.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    session = Session(engine)
    store = DatabaseStore(session, edit_worker=FakeEditWorker())
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Edit tools run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="编辑测试", target_platform="douyin"),
        pipeline=pipeline,
    )
    rendered = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="rendered_video",
            filename="rendered.mp4",
            content_base64=base64.b64encode(b"rendered video bytes").decode(),
            mime_type="video/mp4",
            width=720,
            height=1280,
        ),
    )

    assert rendered is not None
    result = store.run_edit_tool(
        run.run_id,
        EditToolRequest(
            operation=EditOperation.EXPORT_VERSIONS,
            source_asset_id=rendered.asset_id,
            variants=[
                EditVariantRequest(
                    label="抖音",
                    filename="douyin.mp4",
                    render_profile_key="douyin",
                ),
                EditVariantRequest(
                    label="B站",
                    filename="bilibili.mp4",
                    render_profile_key="bilibili",
                    fit="contain",
                ),
            ],
        ),
    )
    events = store.list_events(run.run_id, event_type="run.edit_operation_completed")
    session.close()

    assert result is not None
    invocation, assets, report = result
    assert invocation.tool_key == "ffmpeg_edit_tools"
    assert len(assets) == 2
    assert {asset.asset_type for asset in assets} == {"edited_video"}
    assert assets[0].width == 1080
    assert assets[0].height == 1920
    assert assets[1].width == 1920
    assert assets[1].height == 1080
    assert report.artifact_type == "edit_operation_report"
    assert report.schema_key == "edit_operation_report.v1"
    assert report.payload["operation"] == "export_versions"
    assert report.payload["output_asset_ids"] == [asset.asset_id for asset in assets]
    assert report.payload["outputs"][0]["metadata"]["variant_label"] == "抖音"
    assert events is not None
    assert events[-1].payload["operation"] == "export_versions"


def test_database_store_builds_composition_graph_from_assets(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'composition-graph.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Composition graph run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=15,
        ),
        pipeline=pipeline,
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="script",
        artifact_type="spoken_script",
        payload={
            "version": "1.0",
            "language": "zh-CN",
            "total_duration_seconds": 2,
            "segments": [
                {
                    "segment_id": "seg_01",
                    "start_seconds": 0,
                    "end_seconds": 2,
                    "text": "秋天第一杯奶茶，还没人给你点？",
                    "caption": "秋天第一杯奶茶 还没人给你点？",
                }
            ],
            "caption_style": {"placement": "bottom", "font_size": 56},
        },
    )
    avatar = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="avatar_video_clip",
            filename="avatar.mp4",
            content_base64=base64.b64encode(b"avatar bytes").decode(),
            mime_type="video/mp4",
            duration_ms=2000,
            width=720,
            height=1280,
        ),
    )
    subtitle = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="subtitle_file",
            filename="subtitles.srt",
            content_base64=base64.b64encode(b"1\n00:00:00,000 --> 00:00:02,000\nHi\n").decode(),
            mime_type="application/x-subrip",
        ),
    )
    music = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="background_music",
            filename="music.wav",
            content_base64=base64.b64encode(b"music bytes").decode(),
            mime_type="audio/wav",
        ),
    )

    assert avatar is not None
    assert subtitle is not None
    assert music is not None
    artifact = store.build_composition_graph(
        run.run_id,
        CompositionGraphBuildRequest(
            avatar_video_asset_id=avatar.asset_id,
            subtitle_asset_id=subtitle.asset_id,
            music_asset_id=music.asset_id,
            width=720,
            height=1280,
            fps=30,
            bgm_volume=0.12,
        ),
    )
    events = store.list_events(run.run_id, event_type="run.composition_graph_created")
    session.close()

    assert artifact is not None
    assert artifact.artifact_type == "composition_graph"
    assert artifact.schema_key == "composition_graph.v1"
    assert artifact.producer == "composition_graph_builder"
    assert artifact.payload["canvas"]["width"] == 720
    assert artifact.payload["canvas"]["height"] == 1280
    assert artifact.payload["render_profile"]["profile_key"] == "douyin_vertical"
    assert artifact.payload["render_targets"][0]["settings"]["video_bitrate"] == "6000k"
    assert artifact.payload["render_targets"][0]["settings"]["safe_area"]["bottom"] == 260
    assert artifact.payload["duration_seconds"] == 2
    assert artifact.payload["validation"]["ready_for_render"] is True
    assert artifact.payload["source"]["asset_ids"]["primary_video"] == avatar.asset_id
    assert artifact.payload["captions"]["segments"][0]["text"] == (
        "秋天第一杯奶茶 还没人给你点？"
    )
    assert artifact.payload["audio_mix"]["tracks"][-1]["volume"] == 0.12
    assert {track["track_id"] for track in artifact.payload["tracks"]} >= {
        "track_video_primary",
        "track_caption_main",
        "track_audio_bgm",
    }
    assert events is not None
    assert events[0].payload["ready_for_render"] is True
    assert events[0].payload["render_profile_key"] == "douyin_vertical"


def test_database_store_builds_composition_graph_from_target_platform_profile(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'composition-graph-profile.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="Xiaohongshu graph run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="小红书口播", target_platform="xiaohongshu"),
        pipeline=pipeline,
    )
    avatar = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="avatar_video_clip",
            filename="avatar.mp4",
            content_base64=base64.b64encode(b"avatar bytes").decode(),
            mime_type="video/mp4",
            duration_ms=2000,
        ),
    )

    assert avatar is not None
    artifact = store.build_composition_graph(
        run.run_id,
        CompositionGraphBuildRequest(avatar_video_asset_id=avatar.asset_id),
    )
    session.close()

    assert artifact is not None
    assert artifact.payload["render_profile"]["profile_key"] == "xiaohongshu_portrait"
    assert artifact.payload["canvas"]["width"] == 1080
    assert artifact.payload["canvas"]["height"] == 1440
    assert artifact.payload["canvas"]["aspect_ratio"] == "3:4"
    assert artifact.payload["canvas"]["cover"]["height"] == 1440
    assert artifact.payload["render_targets"][0]["settings"]["render_profile_key"] == (
        "xiaohongshu_portrait"
    )


def test_database_store_renders_html_canvas_preview(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'html-canvas-preview.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    session = Session(engine)
    store = DatabaseStore(session)
    store.storage.root = tmp_path / "storage"
    project = store.create_project(ProjectCreate(title="HTML Canvas preview run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            target_duration_seconds=15,
        ),
        pipeline=pipeline,
    )
    store.artifacts.append_artifact(
        run_id=run.run_id,
        stage_key="script",
        artifact_type="spoken_script",
        payload={
            "version": "1.0",
            "language": "zh-CN",
            "total_duration_seconds": 3,
            "segments": [
                {
                    "segment_id": "seg_01",
                    "start_seconds": 0,
                    "end_seconds": 3,
                    "text": "秋天第一杯奶茶，还没人给你点？",
                    "caption": "秋天第一杯奶茶 还没人给你点？",
                }
            ],
        },
    )
    avatar = store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="avatar_video_clip",
            filename="avatar.mp4",
            content_base64=base64.b64encode(b"avatar bytes").decode(),
            mime_type="video/mp4",
            duration_ms=3000,
        ),
    )
    assert avatar is not None
    graph = store.build_composition_graph(
        run.run_id,
        CompositionGraphBuildRequest(
            avatar_video_asset_id=avatar.asset_id,
            width=360,
            height=640,
            fps=24,
        ),
    )
    assert graph is not None

    result = store.render_html_canvas_preview(
        run.run_id,
        HtmlCanvasPreviewRequest(
            composition_graph_artifact_id=graph.artifact_id,
            filename="preview.html",
            title="秋日奶茶预览",
            preview_time_seconds=1,
        ),
    )
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id)
    session.close()

    assert result is not None
    invocation, asset, report = result
    html = store.storage.read_bytes(asset.storage_url).decode("utf-8")
    assert invocation.tool_key == "html_canvas_renderer"
    assert asset.asset_type == "html_canvas_preview"
    assert asset.mime_type == "text/html; charset=utf-8"
    assert "s2v-canvas" in html
    assert "requestAnimationFrame" in html
    assert "秋天第一杯奶茶" in html
    assert report.artifact_type == "canvas_render_report"
    assert report.schema_key == "canvas_render_report.v1"
    assert report.payload["composition_graph_artifact_id"] == graph.artifact_id
    assert report.payload["html_asset_id"] == asset.asset_id
    assert report.payload["settings"]["render_profile"]["profile_key"] == "douyin_vertical"
    assert report.payload["validation"]["ready_for_preview"] is True
    assert report.payload["first_frame"]["command_count"] >= 3
    assert artifacts is not None
    assert {"composition_graph", "canvas_render_report"}.issubset(
        {artifact.artifact_type for artifact in artifacts}
    )
    assert events is not None
    event_types = [event.event_type for event in events]
    assert "tool.completed" in event_types
    assert "run.canvas_render_report_created" in event_types
