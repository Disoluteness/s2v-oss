import base64
import json
from collections import Counter

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    ApprovalRequest,
    AssetUpload,
    AvatarVideoFromAssetsRequest,
    BackgroundJob,
    BackgroundJobStatus,
    ProjectCreate,
    RunCreate,
)
from apps.worker.background_jobs import BackgroundJobWorker, _should_wait_after_batch
from packages.compose_kit import FfmpegComposeResult
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
                "video_url": "data:video/mp4;base64," + base64.b64encode(b"avatar mp4").decode(),
            }
        }


class FakeComposeWorker:
    def check_availability(self) -> bool:
        return True

    def compose(self, request):
        assert request.avatar_video_path.is_file()
        assert request.narration_path is not None
        assert request.narration_path.is_file()
        request.output_path.write_bytes(b"rendered mp4")
        return FfmpegComposeResult(
            output_path=request.output_path,
            command=["ffmpeg", "fake"],
            duration_seconds=2.0,
            width=request.width,
            height=request.height,
            has_audio=True,
            subtitles_embedded=False,
            subtitle_track_added=False,
        )


def _background_job(status: BackgroundJobStatus) -> BackgroundJob:
    return BackgroundJob(
        job_id="job_test",
        run_id="run_test",
        job_type="avatar_from_assets",
        status=status,
        progress=60,
        request_payload={},
        result_payload={},
    )


def _write_worker_catalog(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(
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
                        "tool_key": "dashscope_avatar_video",
                        "display_name": "DashScope Avatar Video",
                        "capability": "avatar_video",
                        "provider": "dashscope",
                        "runtime_type": "api",
                        "required_env_vars": ["DASHSCOPE_API_KEY"],
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
    return [path]


def test_background_job_worker_waits_when_only_provider_jobs_are_pending() -> None:
    assert _should_wait_after_batch([]) is True
    assert _should_wait_after_batch([_background_job(BackgroundJobStatus.WAITING_PROVIDER)]) is True
    assert _should_wait_after_batch([_background_job(BackgroundJobStatus.COMPLETED)]) is False
    assert _should_wait_after_batch(
        [
            _background_job(BackgroundJobStatus.WAITING_PROVIDER),
            _background_job(BackgroundJobStatus.COMPLETED),
        ]
    ) is False


def test_background_job_worker_continues_when_manifest_dispatch_fails() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rollback_count = 0
            self.closed = False

        def rollback(self) -> None:
            self.rollback_count += 1

        def close(self) -> None:
            self.closed = True

    class FakeStore:
        def dispatch_all_stage_execution_tasks(self):
            raise ValueError("manifest dispatch failed")

        def claim_processable_background_jobs(self, *, worker_id, limit, lease_seconds, queues=None):
            assert worker_id == "worker-a"
            assert limit == 1
            assert lease_seconds == 300
            assert queues is None  # default role "all" claims every queue
            return [_background_job(BackgroundJobStatus.QUEUED)]

        def process_background_job(self, job_id, *, worker_id):
            assert job_id == "job_test"
            assert worker_id == "worker-a"
            return _background_job(BackgroundJobStatus.COMPLETED)

    sessions: list[FakeSession] = []

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    worker = BackgroundJobWorker(
        session_factory=session_factory,
        store_factory=lambda _session: FakeStore(),
        batch_size=1,
        worker_id="worker-a",
    )

    processed = worker.run_once()

    assert [job.status for job in processed] == [BackgroundJobStatus.COMPLETED]
    assert sessions[0].rollback_count == 1
    assert sessions[0].closed is True


def test_background_job_worker_processes_avatar_jobs(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    transport = FakeDashScopeTransport()

    def store_factory(session):
        store = DatabaseStore(
            session,
            avatar_video_adapter=DashScopeAvatarVideoAdapter(
                api_key="test-key",
                transport=transport,
            ),
            compose_worker=FakeComposeWorker(),
        )
        store.storage.root = tmp_path / "storage"
        return store

    seed_session = session_factory()
    seed_store = store_factory(seed_session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    project = seed_store.create_project(ProjectCreate(title="Worker job"))
    run = seed_store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="后台 worker 测试"),
        pipeline=pipeline,
    )
    image_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
        ),
    )
    audio_asset = seed_store.upload_asset(
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
    job = seed_store.enqueue_avatar_from_assets_job(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_asset_id=image_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url="https://studio.example",
        ),
    )
    seed_session.close()
    assert job is not None

    worker = BackgroundJobWorker(
        session_factory=session_factory,
        store_factory=store_factory,
        batch_size=1,
        poll_interval_seconds=0.2,
        worker_id="worker-a",
    )
    competing_worker = BackgroundJobWorker(
        session_factory=session_factory,
        store_factory=store_factory,
        batch_size=1,
        poll_interval_seconds=0.2,
        worker_id="worker-b",
    )
    first_batch = worker.run_once()
    blocked_batch = competing_worker.run_once()
    second_batch = worker.run_once()

    verify_session = session_factory()
    verify_store = store_factory(verify_session)
    completed = verify_store.get_background_job(job.job_id)
    assets = verify_store.list_assets(run.run_id)
    events = verify_store.list_events(run.run_id)
    verify_session.close()

    assert [processed.status for processed in first_batch] == [BackgroundJobStatus.WAITING_PROVIDER]
    assert blocked_batch == []
    assert [processed.status for processed in second_batch] == [BackgroundJobStatus.COMPLETED]
    assert completed is not None
    assert completed.status == BackgroundJobStatus.COMPLETED
    assert completed.result_payload["archived_avatar_asset_id"]
    assert completed.result_payload["render_asset_id"]
    assert completed.result_payload["render_summary_artifact_id"]
    assert completed.result_payload["final_video_url"].startswith("/api/assets/")
    assert completed.result_payload["compose_inputs"]["narration_audio_asset_id"] == audio_asset.asset_id
    assert assets is not None
    assert [asset.asset_type for asset in assets].count("avatar_video_clip") == 1
    assert [asset.asset_type for asset in assets].count("rendered_video") == 1
    assert events is not None
    assert [event.event_type for event in events if event.event_type.startswith("job.")] == [
        "job.queued",
        "job.waiting_provider",
        "job.avatar_archived",
        "job.final_composed",
        "job.completed",
    ]


def test_background_job_worker_skips_missing_optional_compose_asset(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-missing-asset.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    transport = FakeDashScopeTransport()

    def store_factory(session):
        store = DatabaseStore(
            session,
            avatar_video_adapter=DashScopeAvatarVideoAdapter(
                api_key="test-key",
                transport=transport,
            ),
            compose_worker=FakeComposeWorker(),
        )
        store.storage.root = tmp_path / "storage"
        return store

    seed_session = session_factory()
    seed_store = store_factory(seed_session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    project = seed_store.create_project(ProjectCreate(title="Missing asset job"))
    run = seed_store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="缺失资源测试"),
        pipeline=pipeline,
    )
    image_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
        ),
    )
    audio_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
        ),
    )
    subtitle_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="subtitle_file",
            filename="subtitles.srt",
            content_base64=base64.b64encode(b"1\n00:00:00,000 --> 00:00:01,000\nHi\n").decode(),
            mime_type="application/x-subrip",
        ),
    )
    assert image_asset is not None
    assert audio_asset is not None
    assert subtitle_asset is not None
    seed_store.storage._path_for_url(subtitle_asset.storage_url).unlink()
    job = seed_store.enqueue_avatar_from_assets_job(
        run.run_id,
        AvatarVideoFromAssetsRequest(
            person_image_asset_id=image_asset.asset_id,
            audio_asset_id=audio_asset.asset_id,
            public_base_url="https://studio.example",
        ),
    )
    seed_session.close()
    assert job is not None

    worker = BackgroundJobWorker(
        session_factory=session_factory,
        store_factory=store_factory,
        batch_size=1,
        poll_interval_seconds=0.2,
        worker_id="worker-a",
    )
    first_batch = worker.run_once()
    second_batch = worker.run_once()

    verify_session = session_factory()
    verify_store = store_factory(verify_session)
    completed = verify_store.get_background_job(job.job_id)
    events = verify_store.list_events(run.run_id)
    verify_session.close()

    assert [processed.status for processed in first_batch] == [BackgroundJobStatus.WAITING_PROVIDER]
    assert [processed.status for processed in second_batch] == [BackgroundJobStatus.COMPLETED]
    assert completed is not None
    assert completed.status == BackgroundJobStatus.COMPLETED
    assert completed.result_payload["render_asset_id"]
    assert completed.result_payload["compose_inputs"]["subtitle_asset_id"] is None
    assert completed.result_payload["compose_warnings"][0]["role"] == "subtitle_file"
    assert events is not None
    assert "job.optional_asset_missing" in [event.event_type for event in events]


def test_stage_execution_dispatch_preview_reports_readiness(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch-preview.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    registry = ToolRegistry(
        catalog_paths=_write_worker_catalog(tmp_path),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    session = session_factory()
    store = DatabaseStore(session, tool_registry=registry)
    store.storage.root = tmp_path / "storage"
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    project = store.create_project(ProjectCreate(title="Dispatch preview"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="派发预览测试"),
        pipeline=pipeline,
    )
    manifest = store.plan_stage_execution(run.run_id)

    draft_preview = store.preview_stage_execution_dispatch(run.run_id)
    store.start_run(run.run_id)
    store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    store.review_stage(run.run_id, "storyboard", ApprovalRequest(decision="approved"))
    waiting_input_preview = store.preview_stage_execution_dispatch(run.run_id)
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
    ready_preview = store.preview_stage_execution_dispatch(run.run_id)
    session.close()

    assert manifest is not None
    assert draft_preview is not None
    assert draft_preview["summary"]["waiting_stage_steps"] == 4
    assert waiting_input_preview is not None
    avatar_step = next(
        step for step in waiting_input_preview["steps"] if step["capability"] == "avatar_video"
    )
    subtitle_step = next(
        step for step in waiting_input_preview["steps"] if step["capability"] == "subtitle_generation"
    )
    assert avatar_step["status"] == "waiting_input"
    assert avatar_step["missing_inputs"] == ["person_image", "tts_audio"]
    assert subtitle_step["status"] == "ready"
    assert subtitle_step["dispatchable"] is True
    assert image_asset is not None
    assert audio_asset is not None
    ready_avatar_step = next(
        step for step in ready_preview["steps"] if step["capability"] == "avatar_video"
    )
    assert ready_avatar_step["status"] == "ready"
    assert ready_avatar_step["dispatchable"] is True
    assert ready_preview["summary"]["dispatchable_steps"] == 2


def test_background_job_worker_dispatches_stage_execution_manifest_steps(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'worker-dispatch.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    transport = FakeDashScopeTransport()
    registry = ToolRegistry(
        catalog_paths=_write_worker_catalog(tmp_path),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )

    def store_factory(session):
        store = DatabaseStore(
            session,
            tool_registry=registry,
            avatar_video_adapter=DashScopeAvatarVideoAdapter(
                api_key="test-key",
                transport=transport,
            ),
            compose_worker=FakeComposeWorker(),
        )
        store.storage.root = tmp_path / "storage"
        return store

    seed_session = session_factory()
    seed_store = store_factory(seed_session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    project = seed_store.create_project(ProjectCreate(title="Manifest dispatch"))
    run = seed_store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="自动调度测试"),
        pipeline=pipeline,
    )
    image_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="reference_image",
            filename="person.png",
            content_base64=base64.b64encode(b"person image").decode("ascii"),
            mime_type="image/png",
            asset_metadata={
                "role": "person_image",
                "source_image_url": "https://example.com/person.png",
            },
        ),
    )
    audio_asset = seed_store.upload_asset(
        run.run_id,
        AssetUpload(
            asset_type="tts_audio",
            filename="voice.wav",
            content_base64=base64.b64encode(b"voice audio").decode("ascii"),
            mime_type="audio/wav",
            asset_metadata={"source_audio_url": "https://example.com/voice.wav"},
        ),
    )
    manifest = seed_store.plan_stage_execution(run.run_id)
    seed_store.start_run(run.run_id)
    seed_store.review_stage(run.run_id, "plan", ApprovalRequest(decision="approved"))
    seed_store.review_stage(run.run_id, "script", ApprovalRequest(decision="approved"))
    seed_store.review_stage(run.run_id, "storyboard", ApprovalRequest(decision="approved"))
    seed_session.close()

    assert image_asset is not None
    assert audio_asset is not None
    assert manifest is not None

    worker = BackgroundJobWorker(
        session_factory=session_factory,
        store_factory=store_factory,
        batch_size=10,
        poll_interval_seconds=0.2,
        worker_id="worker-a",
    )
    first_batch = worker.run_once()
    second_batch = worker.run_once()

    review_session = session_factory()
    review_store = store_factory(review_session)
    review_store.review_stage(run.run_id, "asset_review", ApprovalRequest(decision="approved"))
    review_session.close()

    third_batch = worker.run_once()

    final_review_session = session_factory()
    final_review_store = store_factory(final_review_session)
    final_review_store.review_stage(run.run_id, "final_review", ApprovalRequest(decision="approved"))
    final_review_session.close()

    fourth_batch = worker.run_once()
    fifth_batch = worker.run_once()

    verify_session = session_factory()
    verify_store = store_factory(verify_session)
    jobs = verify_store.list_background_jobs(run.run_id)
    assets = verify_store.list_assets(run.run_id)
    artifacts = verify_store.list_artifacts(run.run_id, current_only=True)
    events = verify_store.list_events(run.run_id)
    verify_session.close()

    assert [job.status for job in first_batch] == [
        BackgroundJobStatus.WAITING_PROVIDER,
        BackgroundJobStatus.COMPLETED,
    ]
    assert [job.status for job in second_batch] == [BackgroundJobStatus.COMPLETED]
    assert [job.status for job in third_batch] == [BackgroundJobStatus.COMPLETED]
    assert [job.status for job in fourth_batch] == [BackgroundJobStatus.COMPLETED]
    assert fifth_batch == []
    assert jobs is not None
    assert Counter(job.job_type for job in jobs) == {
        "avatar_from_assets": 1,
        "stage_tool_step": 3,
    }
    assert len({job.request_payload["manifest_step_key"] for job in jobs}) == 4
    assert assets is not None
    assert [asset.asset_type for asset in assets].count("avatar_video_clip") == 1
    assert [asset.asset_type for asset in assets].count("subtitle_file") == 1
    assert [asset.asset_type for asset in assets].count("rendered_video") == 1
    assert artifacts is not None
    publish_package = next(
        artifact for artifact in artifacts if artifact.artifact_type == "publish_package"
    )
    assert publish_package.payload["mode"] == "stage_execution_manifest"
    assert publish_package.payload["render_asset_id"]
    assert events is not None
    assert "run.stage_execution_dispatched" in [event.event_type for event in events]
    assert "job.publish_package_created" in [event.event_type for event in events]
