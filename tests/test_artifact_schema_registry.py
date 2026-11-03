import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.models import ProjectCreate, RunCreate
from apps.api.app.repositories import ArtifactRepository, ProductionRunRepository, ProjectRepository
from packages.artifact_kit import ArtifactSchemaRegistry, ArtifactValidationError
from packages.pipeline_kit.loader import PipelineLoader


def test_artifact_schema_registry_lists_core_artifacts() -> None:
    registry = ArtifactSchemaRegistry()
    schema_keys = {schema.schema_key for schema in registry.list_schemas()}

    assert "intake_brief.v1" in schema_keys
    assert "production_plan.v1" in schema_keys
    assert "spoken_script.v1" in schema_keys
    assert "proposal_packet.v1" in schema_keys
    assert "composition_graph.v1" in schema_keys
    assert "canvas_render_report.v1" in schema_keys
    assert "quality_report.v1" in schema_keys
    assert "edit_operation_report.v1" in schema_keys
    assert "stage_execution_manifest.v1" in schema_keys
    assert "source_asset_manifest.v1" in schema_keys
    assert "source_media_review.v1" in schema_keys
    assert "media_analysis_report.v1" in schema_keys
    assert "video_analysis_brief.v1" in schema_keys
    assert "screening_room_scene_plan.v1" in schema_keys
    assert "clip_candidate_list.v1" in schema_keys
    assert "clip_export_manifest.v1" in schema_keys
    assert "demo_flow_plan.v1" in schema_keys
    assert "recording_plan.v1" in schema_keys
    assert "screen_capture_manifest.v1" in schema_keys
    assert "interaction_annotation_plan.v1" in schema_keys


def test_artifact_repository_rejects_invalid_registered_artifact() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    artifact_repo = ArtifactRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Schema validation"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="校验测试"),
        pipeline=pipeline,
    )

    with pytest.raises(ArtifactValidationError) as error:
        artifact_repo.append_artifact(
            run_id=run.run_id,
            stage_key="plan",
            artifact_type="production_plan",
            payload={"status": "missing required production plan fields"},
        )

    assert error.value.schema_key == "production_plan.v1"
    assert "planner" in str(error.value)
    assert artifact_repo.list_artifacts(run.run_id) == []


def test_failed_validation_does_not_replace_current_artifact() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    project_repo = ProjectRepository(session)
    run_repo = ProductionRunRepository(session)
    artifact_repo = ArtifactRepository(session)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    project = project_repo.create_project(ProjectCreate(title="Current safety"))
    run = run_repo.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="校验测试"),
        pipeline=pipeline,
    )
    current = artifact_repo.append_artifact(
        run_id=run.run_id,
        stage_key="intake",
        artifact_type="intake_brief",
        payload={
            "version": "1.0",
            "language": "zh-CN",
            "raw_script": "第一版",
            "target_platform": "douyin",
            "aspect_ratio": "9:16",
            "target_duration_seconds": 15,
        },
    )

    with pytest.raises(ArtifactValidationError):
        artifact_repo.append_artifact(
            run_id=run.run_id,
            stage_key="intake",
            artifact_type="intake_brief",
            payload={"raw_script": "缺少必填字段"},
        )

    current_artifacts = artifact_repo.list_artifacts(run.run_id, current_only=True)

    assert len(current_artifacts) == 1
    assert current_artifacts[0].artifact_id == current.artifact_id
    assert current_artifacts[0].payload["raw_script"] == "第一版"
