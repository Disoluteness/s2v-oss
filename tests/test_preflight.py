import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import ProjectCreate, RunCreate
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.preflight import build_preflight_report
from packages.tool_kit.registry import ToolRegistry


def write_catalog(tmp_path, tools: list[dict]) -> list:
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return [path]


def test_preflight_report_marks_blocked_required_capabilities(tmp_path) -> None:
    catalog_paths = write_catalog(
        tmp_path,
        [
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
                "fallback_tool_keys": ["local_asset_upload"],
            },
        ],
    )
    registry = ToolRegistry(
        catalog_paths=catalog_paths,
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    report = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "测试脚本"},
    )
    stage_status = {stage["stage_key"]: stage["status"] for stage in report["stages"]}
    avatar_report = next(
        capability
        for capability in report["required_capabilities"]
        if capability["capability"] == "avatar_video"
    )
    avatar_tool = next(
        tool
        for tool in avatar_report["tools"]
        if tool["tool_key"] == "dashscope_avatar_video"
    )

    assert report["overall_status"] == "blocked"
    assert stage_status["script"] == "ready"
    assert stage_status["asset_generation"] == "blocked"
    assert avatar_report["status"] == "unavailable"
    assert avatar_report["missing_dependencies"] == ["env:DASHSCOPE_API_KEY"]
    assert avatar_report["selector"]["selector_key"] == "avatar_selector"
    assert avatar_report["selector"]["selected_tool_key"] is None
    assert avatar_report["selector"]["candidates"][0]["selection_role"] == "blocked"
    assert avatar_tool["contract_version"] == "2.0"
    assert avatar_tool["runtime"] == "api"
    assert avatar_tool["selection"]["selection_role"] == "blocked"
    assert avatar_tool["selection"]["fallback_reason"] is not None
    assert avatar_tool["dependencies"] == [
        {"kind": "env", "name": "DASHSCOPE_API_KEY", "required": True}
    ]
    assert avatar_tool["operations"] == {
        "validate": True,
        "estimate": True,
        "submit": True,
        "poll": True,
        "archive": True,
        "cancel": False,
    }
    assert avatar_tool["retry_policy"]["timeout_seconds"] == 600
    assert "resource_profile" in avatar_tool
    assert "quality_profile" in avatar_tool
    assert report["blocking_issues"][0]["capability"] == "avatar_video"
    assert report["recommendations"][0]["action"] == "configure_or_replace_provider"


def test_database_store_persists_preflight_report_artifact_and_event(tmp_path) -> None:
    catalog_paths = write_catalog(
        tmp_path,
        [
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
        ],
    )
    registry = ToolRegistry(
        catalog_paths=catalog_paths,
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'preflight.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=registry)
    project = store.create_project(ProjectCreate(title="Preflight run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="预检测试"),
        pipeline=pipeline,
    )

    artifact = store.run_preflight(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id, event_type="run.preflight_completed")
    session.close()

    assert artifact is not None
    assert artifact.artifact_type == "preflight_report"
    assert artifact.payload["overall_status"] == "degraded"
    assert artifact.payload["pipeline"]["pipeline_key"] == "spokesperson_ad"
    assert any(
        capability["selector"]["selector_version"] == "1.0"
        for capability in artifact.payload["required_capabilities"]
    )
    assert artifacts is not None
    assert artifacts[-1].artifact_type == "preflight_report"
    assert events is not None
    assert events[-1].payload["artifact_id"] == artifact.artifact_id
