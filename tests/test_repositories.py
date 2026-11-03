from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_models import BackgroundJobRecord, PipelineVersionRecord, ProductionRunRecord
from apps.api.app.models import (
    BackgroundJobCreate,
    BackgroundJobStatus,
    EventSeverity,
    EventSourceType,
    ProjectCreate,
    ProviderCredentialCreate,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    RunCreate,
    RunStatus,
    StageStatus,
    ToolInvocationStatus,
)
from apps.api.app.repositories import (
    ArtifactRepository,
    AssetRepository,
    BackgroundJobRepository,
    EventRepository,
    ProductionRunRepository,
    ProjectRepository,
    ProviderCredentialRepository,
    ToolInvocationRepository,
)
from packages.pipeline_kit.loader import PipelineLoader


def session_factory() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_project_repository_creates_and_reads_project() -> None:
    session = session_factory()
    repo = ProjectRepository(session)

    project = repo.create_project(ProjectCreate(title="Milk tea ad"))
    session.commit()

    found = repo.get_project(project.project_id)

    assert found is not None
    assert found.project_id == project.project_id
    assert found.title == "Milk tea ad"


def test_provider_credential_repository_masks_secrets_and_builds_env() -> None:
    session = session_factory()
    repo = ProviderCredentialRepository(session)

    credential = repo.create_credential(
        ProviderCredentialCreate(
            provider="dashscope",
            label="DashScope production",
            secret_values={"DASHSCOPE_API_KEY": "sk-test-secret"},
            quota={"monthly_limit": 100},
        )
    )
    session.commit()

    found = repo.get_credential(credential.credential_id)
    env = repo.environment_overrides()

    assert found is not None
    assert found.secret_names == ["DASHSCOPE_API_KEY"]
    assert found.secret_preview == {"DASHSCOPE_API_KEY": "sk-...cret"}
    assert found.quota == {"monthly_limit": 100}
    assert env == {"DASHSCOPE_API_KEY": "sk-test-secret"}
    assert repo.disabled_providers() == set()


def test_provider_credential_repository_tracks_disabled_providers() -> None:
    session = session_factory()
    repo = ProviderCredentialRepository(session)
    credential = repo.create_credential(
        ProviderCredentialCreate(
            provider="dashscope",
            status=ProviderCredentialStatus.DISABLED,
        )
    )
    session.commit()

    assert repo.environment_overrides() == {}
    assert repo.disabled_providers() == {"dashscope"}

    repo.update_credential(
        credential.credential_id,
        ProviderCredentialUpdate(
            status=ProviderCredentialStatus.ACTIVE,
            secret_values={"DASHSCOPE_API_KEY": "sk-active"},
        ),
    )
    session.commit()

    assert repo.environment_overrides() == {"DASHSCOPE_API_KEY": "sk-active"}
    assert repo.disabled_providers() == set()


def test_run_repository_creates_run_with_ordered_stages() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )
    session.commit()

    found = run_repo.get_run(run.run_id)

    assert found is not None
    assert found.status == RunStatus.DRAFT
    assert found.current_stage_key == "intake"
    assert found.pipeline_version == pipeline.version
    assert found.pipeline_dsl_version == pipeline.dsl_version
    assert found.pipeline_definition_snapshot is not None
    assert found.pipeline_definition_snapshot["pipeline_key"] == "spokesperson_ad"
    assert [stage.stage_key for stage in found.stages][:3] == ["intake", "plan", "script"]


def test_run_repository_creates_avatar_spokesperson_run_snapshot() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    project = project_repo.create_project(ProjectCreate(title="Knowledge avatar"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            pipeline_key="avatar_spokesperson",
            raw_script="为什么天空是蓝色的？",
            target_platform="douyin",
            target_duration_seconds=30,
        ),
        pipeline=pipeline,
    )
    session.commit()

    found = run_repo.get_run(run.run_id)
    versions = session.query(PipelineVersionRecord).all()

    assert found is not None
    assert found.pipeline_key == "avatar_spokesperson"
    assert found.pipeline_version == "0.4.0"
    assert found.pipeline_definition_snapshot is not None
    assert found.pipeline_definition_snapshot["pipeline_key"] == "avatar_spokesperson"
    assert found.pipeline_definition_snapshot["metadata"]["legacy_pipeline_key"] == (
        "spokesperson_ad"
    )
    assert len(versions) == 1
    assert versions[0].pipeline_key == "avatar_spokesperson"
    assert versions[0].content_hash == pipeline.content_hash


def test_run_repository_registers_and_reuses_pipeline_versions() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    first = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="第一次运行"),
        pipeline=pipeline,
    )
    second = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="第二次运行"),
        pipeline=pipeline,
    )
    session.commit()

    versions = session.query(PipelineVersionRecord).all()
    first_record = session.get(ProductionRunRecord, first.run_id)
    second_record = session.get(ProductionRunRecord, second.run_id)

    assert len(versions) == 1
    assert versions[0].pipeline_key == "spokesperson_ad"
    assert versions[0].version == pipeline.version
    assert versions[0].dsl_version == "1.0"
    assert versions[0].content_hash == pipeline.content_hash
    assert versions[0].definition_snapshot["dsl_version"] == "1.0"
    assert first_record is not None
    assert second_record is not None
    assert first_record.pipeline_version_id == versions[0].id
    assert second_record.pipeline_version_id == versions[0].id
    assert first_record.pipeline_definition_snapshot["content_hash"] == pipeline.content_hash


def test_run_repository_updates_run_and_stage_status() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )

    run_repo.update_run_state(
        run.run_id,
        status=RunStatus.WAITING_REVIEW,
        current_stage_key="plan",
    )
    updated = run_repo.update_stage_status(run.run_id, "plan", StageStatus.WAITING_REVIEW)

    assert updated is not None
    assert updated.status == RunStatus.WAITING_REVIEW
    assert updated.current_stage_key == "plan"
    assert next(stage for stage in updated.stages if stage.stage_key == "plan").status == (
        StageStatus.WAITING_REVIEW
    )


def test_artifact_repository_appends_and_versions_artifacts() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    artifact_repo = ArtifactRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )
    first = artifact_repo.append_artifact(
        run_id=run.run_id,
        stage_key="intake",
        artifact_type="intake_brief",
        payload={
            "version": "1.0",
            "language": "zh-CN",
            "raw_script": "v1",
            "target_platform": "douyin",
            "aspect_ratio": "9:16",
            "target_duration_seconds": 15,
        },
    )
    second = artifact_repo.append_artifact(
        run_id=run.run_id,
        stage_key="intake",
        artifact_type="intake_brief",
        payload={
            "version": "1.0",
            "language": "zh-CN",
            "raw_script": "v2",
            "target_platform": "douyin",
            "aspect_ratio": "9:16",
            "target_duration_seconds": 15,
        },
        producer="repository_test",
        lineage={"source": "unit_test"},
        derived_from=[{"artifact_id": first.artifact_id, "artifact_type": first.artifact_type}],
    )
    session.commit()

    artifacts = artifact_repo.list_artifacts(run.run_id)
    current_artifacts = artifact_repo.list_artifacts(run.run_id, current_only=True)
    payloads = artifact_repo.current_payloads(run.run_id)

    assert first.version == 1
    assert second.version == 2
    assert [artifact.version for artifact in artifacts] == [1, 2]
    assert [artifact.is_current for artifact in artifacts] == [False, True]
    assert [artifact.version for artifact in current_artifacts] == [2]
    assert payloads["intake_brief"]["raw_script"] == "v2"
    assert second.schema_key == "intake_brief.v1"
    assert second.producer == "repository_test"
    assert second.lineage == {"source": "unit_test"}
    assert second.derived_from == [
        {"artifact_id": first.artifact_id, "artifact_type": first.artifact_type}
    ]


def test_event_repository_records_run_stage_and_tool_events() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    event_repo = EventRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )
    run_event = event_repo.append_event(
        run_id=run.run_id,
        event_type="run.created",
        source_type=EventSourceType.RUN,
        source_id=run.run_id,
        payload={"status": "draft"},
    )
    stage_event = event_repo.append_event(
        run_id=run.run_id,
        event_type="stage.started",
        source_type=EventSourceType.STAGE,
        stage_key="plan",
        source_id="stage_plan",
        payload={"status": "running"},
    )
    tool_event = event_repo.append_event(
        run_id=run.run_id,
        event_type="tool.completed",
        source_type=EventSourceType.TOOL,
        source_id="tool_invocation_1",
        tool_invocation_id="tool_invocation_1",
        severity=EventSeverity.INFO,
        payload={"tool_key": "demo_tool"},
    )
    session.commit()

    all_events = event_repo.list_events(run.run_id)
    tool_events = event_repo.list_events(run.run_id, source_type=EventSourceType.TOOL)
    stage_events = event_repo.list_events(run.run_id, stage_key="plan")

    assert [event.sequence for event in [run_event, stage_event, tool_event]] == [1, 2, 3]
    assert [event.event_type for event in all_events] == [
        "run.created",
        "stage.started",
        "tool.completed",
    ]
    assert [event.event_type for event in tool_events] == ["tool.completed"]
    assert [event.event_type for event in stage_events] == ["stage.started"]


def test_asset_repository_creates_and_lists_assets() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    asset_repo = AssetRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Milk tea ad"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="秋天第一杯奶茶，还没人给你点？"),
        pipeline=pipeline,
    )
    asset = asset_repo.create_asset(
        asset_id="asset_test",
        run_id=run.run_id,
        asset_type="image",
        storage_url="local://script2video/runs/run_1/asset_test/demo.png",
        mime_type="image/png",
        file_size=12,
        checksum="abc123",
        asset_metadata={"filename": "demo.png"},
    )
    session.commit()

    found = asset_repo.get_asset(asset.asset_id)
    assets = asset_repo.list_assets(run.run_id)

    assert found is not None
    assert found.storage_url == "local://script2video/runs/run_1/asset_test/demo.png"
    assert found.asset_metadata == {"filename": "demo.png"}
    assert [stored_asset.asset_id for stored_asset in assets] == ["asset_test"]


def test_tool_invocation_repository_stores_error_taxonomy_fields() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    invocation_repo = ToolInvocationRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Tool invocation run"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="工具调用失败测试"),
        pipeline=pipeline,
    )
    invocation = invocation_repo.create_invocation(
        run_id=run.run_id,
        tool_key="dashscope_avatar_video",
        provider="dashscope",
        request_payload={"image_url": "https://example.com/person.png"},
        status=ToolInvocationStatus.SUBMITTED,
    )
    updated = invocation_repo.update_invocation(
        invocation.invocation_id,
        status=ToolInvocationStatus.FAILED,
        error_code="InvalidParameter.Ratio",
        error_message="request parameter is invalid",
        error_category="input",
        error_taxonomy_code="input.invalid_or_missing",
        error_retryable=False,
        error_user_message="输入参数、素材引用或当前状态不满足要求。",
        error_remediation="修正参数后重试。",
    )
    session.commit()

    assert updated is not None
    assert updated.status == ToolInvocationStatus.FAILED
    assert updated.error_code == "InvalidParameter.Ratio"
    assert updated.error_category == "input"
    assert updated.error_taxonomy_code == "input.invalid_or_missing"
    assert updated.error_retryable is False
    assert updated.error_user_message == "输入参数、素材引用或当前状态不满足要求。"
    assert updated.error_remediation == "修正参数后重试。"


def test_background_job_repository_creates_updates_and_retries_jobs() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    job_repo = BackgroundJobRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Background job run"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="后台任务测试"),
        pipeline=pipeline,
    )
    job = job_repo.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(
            job_type="avatar_from_assets",
            request_payload={"person_image_asset_id": "asset_person"},
            max_attempts=2,
        ),
    )
    updated = job_repo.update_job(
        job.job_id,
        status=BackgroundJobStatus.FAILED,
        progress=100,
        error_code="provider_failed",
        error_message="provider failed",
        error_category="provider",
        error_taxonomy_code="provider.remote_failure",
        error_retryable=True,
        error_user_message="外部生成服务返回失败或结果不可用。",
        error_remediation="查看 provider 控制台后重试。",
    )
    retried = job_repo.retry_job(job.job_id)
    jobs = job_repo.list_jobs(run.run_id)
    session.commit()

    assert updated is not None
    assert updated.status == BackgroundJobStatus.FAILED
    assert updated.error_category == "provider"
    assert updated.error_taxonomy_code == "provider.remote_failure"
    assert updated.error_retryable is True
    assert updated.error_user_message == "外部生成服务返回失败或结果不可用。"
    assert updated.error_remediation == "查看 provider 控制台后重试。"
    assert retried is not None
    assert retried.status == BackgroundJobStatus.QUEUED
    assert retried.attempt == 2
    assert retried.error_code is None
    assert retried.error_category is None
    assert retried.error_taxonomy_code is None
    assert retried.error_retryable is None
    assert retried.error_user_message is None
    assert retried.error_remediation is None
    assert [stored_job.job_id for stored_job in jobs] == [job.job_id]


def test_background_job_repository_lists_processable_jobs() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    job_repo = BackgroundJobRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Processable jobs"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="后台任务筛选"),
        pipeline=pipeline,
    )
    queued = job_repo.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(job_type="avatar_from_assets"),
    )
    waiting = job_repo.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(job_type="avatar_from_assets"),
    )
    failed = job_repo.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(job_type="avatar_from_assets"),
    )
    job_repo.update_job(waiting.job_id, status=BackgroundJobStatus.WAITING_PROVIDER)
    job_repo.update_job(failed.job_id, status=BackgroundJobStatus.FAILED)
    session.commit()

    jobs = job_repo.list_processable_jobs(limit=10)
    session.close()

    assert {job.job_id for job in jobs} == {queued.job_id, waiting.job_id}


def test_background_job_repository_claims_jobs_with_lease() -> None:
    session = session_factory()
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    job_repo = BackgroundJobRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Leased jobs"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="任务租约"),
        pipeline=pipeline,
    )
    job = job_repo.create_job(
        run_id=run.run_id,
        payload=BackgroundJobCreate(job_type="avatar_from_assets"),
    )
    first_claim = job_repo.claim_processable_jobs(worker_id="worker-a", limit=1, lease_seconds=300)
    session.commit()

    second_claim = job_repo.claim_processable_jobs(worker_id="worker-b", limit=1, lease_seconds=300)
    record = session.get(BackgroundJobRecord, job.job_id)
    assert record is not None
    # V8.1: the lease is an explicit timestamp, not the updated_at approximation.
    record.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    session.commit()
    stale_claim = job_repo.claim_processable_jobs(worker_id="worker-b", limit=1, lease_seconds=300)
    session.commit()
    locked = job_repo.get_job(job.job_id)
    session.close()

    assert [claimed.job_id for claimed in first_claim] == [job.job_id]
    assert first_claim[0].locked_by == "worker-a"
    assert second_claim == []
    assert [claimed.job_id for claimed in stale_claim] == [job.job_id]
    assert locked is not None
    assert locked.locked_by == "worker-b"


def test_repositories_work_against_existing_schema(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'repo.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    artifact_repo = ArtifactRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Persistent flow"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="测试脚本"),
        pipeline=pipeline,
    )
    artifact = artifact_repo.append_artifact(
        run_id=run.run_id,
        stage_key="plan",
        artifact_type="custom_status",
        payload={"status": "ok"},
    )
    session.commit()
    session.close()

    second_session = Session(engine)
    second_run_repo = ProductionRunRepository(second_session)
    second_artifact_repo = ArtifactRepository(second_session)

    reloaded_run = second_run_repo.get_run(run.run_id)
    reloaded_artifacts = second_artifact_repo.list_artifacts(run.run_id)

    assert reloaded_run is not None
    assert reloaded_run.run_id == run.run_id
    assert reloaded_artifacts[0].artifact_id == artifact.artifact_id
    assert reloaded_artifacts[0].payload == {"status": "ok"}
