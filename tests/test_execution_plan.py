import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import ProjectCreate, RunCreate, RunStatus
from packages.orchestration_kit import build_stage_execution_manifest
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.execution_plan import build_execution_plan
from packages.tool_kit.preflight import build_preflight_report
from packages.tool_kit.registry import ToolRegistry


def write_catalog(tmp_path, tools: list[dict]) -> list:
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return [path]


def base_catalog() -> list[dict]:
    return [
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
        {
            "tool_key": "local_object_storage",
            "display_name": "Local Object Storage",
            "capability": "storage",
            "provider": "local",
            "runtime_type": "local",
        },
    ]


def source_clip_catalog() -> list[dict]:
    return base_catalog() + [
        {
            "tool_key": "local_asset_upload",
            "display_name": "Local Asset Upload",
            "capability": "asset_ingestion",
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
            "tool_key": "local_clip_selector",
            "display_name": "Local Clip Selector",
            "capability": "clip_selection",
            "provider": "internal",
            "runtime_type": "local",
        },
        {
            "tool_key": "ffmpeg_clip_export",
            "display_name": "FFmpeg Clip Export",
            "capability": "clip_export",
            "provider": "ffmpeg",
            "runtime_type": "local",
            "required_binaries": ["ffmpeg"],
        },
        {
            "tool_key": "local_transcript_placeholder",
            "display_name": "Local Transcript Placeholder",
            "capability": "speech_to_text",
            "provider": "internal",
            "runtime_type": "local",
            "status": "degraded",
        },
    ]


def screen_demo_catalog() -> list[dict]:
    return base_catalog() + [
        {
            "tool_key": "local_demo_recording_placeholder",
            "display_name": "Local Demo Recording Placeholder",
            "capability": "browser_recording",
            "provider": "internal",
            "runtime_type": "local",
            "status": "degraded",
            "quality_profile": {"tier": "preview", "deterministic": True},
        },
        {
            "tool_key": "local_screen_capture_placeholder",
            "display_name": "Local Screen Capture Placeholder",
            "capability": "screen_capture",
            "provider": "internal",
            "runtime_type": "local",
            "status": "degraded",
            "quality_profile": {"tier": "preview", "deterministic": True},
        },
        {
            "tool_key": "local_interaction_highlight_planner",
            "display_name": "Local Interaction Highlight Planner",
            "capability": "interaction_highlight",
            "provider": "internal",
            "runtime_type": "local",
            "quality_profile": {"tier": "draft", "deterministic": True},
        },
    ]


def localization_dub_catalog() -> list[dict]:
    return base_catalog() + [
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
    ]


def hybrid_story_catalog() -> list[dict]:
    return base_catalog() + [
        {
            "tool_key": "local_asset_upload",
            "display_name": "Local Asset Upload",
            "capability": "asset_ingestion",
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
    ]


def test_execution_plan_blocks_unavailable_required_provider(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "测试脚本"},
    )

    plan = build_execution_plan(preflight_report=preflight)
    blocked_decisions = [
        decision for decision in plan["decision_log"] if decision["decision"] == "blocked"
    ]

    assert plan["execution_policy"] == "blocked"
    assert plan["can_execute"] is False
    assert blocked_decisions[0]["capability"] == "avatar_video"
    assert blocked_decisions[0]["missing_dependencies"] == ["env:DASHSCOPE_API_KEY"]
    assert blocked_decisions[0]["selected_tool"] is None
    assert blocked_decisions[0]["selector"]["selector_key"] == "avatar_selector"
    assert blocked_decisions[0]["selection_reason"] is None
    assert blocked_decisions[0]["options_considered"][0]["contract_version"] == "2.0"
    assert blocked_decisions[0]["options_considered"][0]["operations"]["submit"] is True
    assert blocked_decisions[0]["options_considered"][0]["runtime"] == "api"
    assert blocked_decisions[0]["options_considered"][0]["selector_score"] <= 0
    assert blocked_decisions[0]["options_considered"][0]["selection_role"] == "blocked"
    assert blocked_decisions[0]["options_considered"][0]["fallback_reason"] is not None
    assert "resource_profile" in blocked_decisions[0]["options_considered"][0]
    assert "cost_model" in blocked_decisions[0]["options_considered"][0]
    assert plan["stage_plans"][4]["stage_key"] == "asset_generation"
    assert plan["stage_plans"][4]["execution_mode"] == "blocked"
    assert any(checkpoint["stage_key"] == "plan" for checkpoint in plan["human_checkpoints"])


def test_avatar_spokesperson_preflight_and_plan_use_formal_pipeline_key(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "知识口播测试"},
    )

    plan = build_execution_plan(preflight_report=preflight)

    assert preflight["pipeline"]["pipeline_key"] == "avatar_spokesperson"
    assert preflight["pipeline"]["display_name"] == "Avatar Spokesperson"
    assert preflight["pipeline"]["version"] == "0.4.0"
    assert plan["pipeline"]["pipeline_key"] == "avatar_spokesperson"
    assert plan["can_execute"] is True
    assert any(stage["stage_key"] == "asset_generation" for stage in plan["stage_plans"])


def test_animated_explainer_plan_keeps_compose_planned_not_auto_dispatched(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("animated_explainer")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "为什么天空是蓝色的？", "target_duration_seconds": 30},
    )
    execution_plan = build_execution_plan(preflight_report=preflight)

    manifest = build_stage_execution_manifest(
        run_id="run_explainer",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )
    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    compose_step = next(
        step
        for step in tasks["compose"]["tool_steps"]
        if step["capability"] == "video_composition"
    )

    assert preflight["pipeline"]["pipeline_key"] == "animated_explainer"
    assert preflight["overall_status"] == "degraded"
    assert execution_plan["can_execute"] is True
    assert manifest["pipeline"]["pipeline_key"] == "animated_explainer"
    assert tasks["compose"]["executor"] == "agent_runner"
    assert compose_step["status"] == "planned"
    assert compose_step["auto_dispatch"] is False
    assert compose_step["dispatch_target"] == "background_worker"


def test_source_clip_factory_plan_keeps_export_planned_not_auto_dispatched(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, source_clip_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg"
        if binary in {"ffmpeg", "ffprobe"}
        else None,
    )
    pipeline = PipelineLoader().get_pipeline("source_clip_factory")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "把这条长视频切成 3 条短视频", "target_duration_seconds": 30},
    )
    execution_plan = build_execution_plan(preflight_report=preflight)

    manifest = build_stage_execution_manifest(
        run_id="run_clip_factory",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )
    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    export_step = next(
        step
        for step in tasks["export_clips"]["tool_steps"]
        if step["capability"] == "clip_export"
    )
    publish_step = next(
        step
        for step in tasks["publish"]["tool_steps"]
        if step["capability"] == "storage"
    )

    assert preflight["pipeline"]["pipeline_key"] == "source_clip_factory"
    assert preflight["overall_status"] == "degraded"
    assert execution_plan["can_execute"] is True
    assert manifest["pipeline"]["pipeline_key"] == "source_clip_factory"
    assert tasks["export_clips"]["executor"] == "agent_runner"
    assert export_step["status"] == "planned"
    assert export_step["auto_dispatch"] is False
    assert export_step["dispatch_target"] == "background_worker"
    assert publish_step["status"] == "planned"
    assert publish_step["auto_dispatch"] is False


def test_screen_demo_plan_keeps_capture_and_compose_planned_not_auto_dispatched(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, screen_demo_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("screen_demo")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={
            "raw_script": "演示脚本放映室如何把脚本变成视频。",
            "target_duration_seconds": 45,
            "target_platform": "internal_training",
            "aspect_ratio": "16:9",
        },
    )
    execution_plan = build_execution_plan(preflight_report=preflight)

    manifest = build_stage_execution_manifest(
        run_id="run_screen_demo",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )
    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    capture_step = next(
        step
        for step in tasks["capture"]["tool_steps"]
        if step["capability"] == "browser_recording"
    )
    compose_step = next(
        step
        for step in tasks["compose"]["tool_steps"]
        if step["capability"] == "video_composition"
    )
    export_step = next(
        step
        for step in tasks["export"]["tool_steps"]
        if step["capability"] == "storage"
    )

    assert preflight["pipeline"]["pipeline_key"] == "screen_demo"
    assert preflight["overall_status"] == "degraded"
    assert execution_plan["can_execute"] is True
    assert manifest["pipeline"]["pipeline_key"] == "screen_demo"
    assert tasks["capture"]["executor"] == "agent_runner"
    assert capture_step["status"] == "planned"
    assert capture_step["decision"] == "use_degraded_tool"
    assert capture_step["auto_dispatch"] is False
    assert capture_step["dispatch_target"] == "background_worker"
    assert compose_step["status"] == "planned"
    assert compose_step["auto_dispatch"] is False
    assert export_step["status"] == "planned"
    assert export_step["auto_dispatch"] is False


def test_localization_dub_plan_builds_and_keeps_dub_and_compose_planned(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, localization_dub_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg"
        if binary in {"ffmpeg", "ffprobe"}
        else None,
    )
    pipeline = PipelineLoader().get_pipeline("localization_dub")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={
            "raw_script": "把这条视频本地化成英文版。",
            "target_duration_seconds": 60,
            "target_platform": "bilibili",
            "aspect_ratio": "16:9",
        },
    )
    execution_plan = build_execution_plan(preflight_report=preflight)
    manifest = build_stage_execution_manifest(
        run_id="run_localization_dub",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )
    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    dub_step = next(
        step for step in tasks["dub"]["tool_steps"] if step["capability"] == "tts"
    )
    compose_step = next(
        step
        for step in tasks["compose"]["tool_steps"]
        if step["capability"] == "video_composition"
    )
    export_step = next(
        step for step in tasks["export"]["tool_steps"] if step["capability"] == "storage"
    )

    assert preflight["pipeline"]["pipeline_key"] == "localization_dub"
    assert preflight["overall_status"] == "degraded"
    assert execution_plan["can_execute"] is True
    assert manifest["pipeline"]["pipeline_key"] == "localization_dub"
    assert dub_step["status"] == "planned"
    assert dub_step["decision"] == "use_degraded_tool"
    assert dub_step["auto_dispatch"] is False
    assert compose_step["status"] == "planned"
    assert compose_step["auto_dispatch"] is False
    assert export_step["status"] == "planned"
    assert export_step["auto_dispatch"] is False


def test_hybrid_story_plan_builds_and_keeps_ingest_and_supplement_planned(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, hybrid_story_catalog()),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg"
        if binary in {"ffmpeg", "ffprobe"}
        else None,
    )
    pipeline = PipelineLoader().get_pipeline("hybrid_story")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={
            "raw_script": "用我拍的素材讲一个周末露营的故事。",
            "target_duration_seconds": 45,
            "target_platform": "xiaohongshu",
            "aspect_ratio": "9:16",
        },
    )
    execution_plan = build_execution_plan(preflight_report=preflight)
    manifest = build_stage_execution_manifest(
        run_id="run_hybrid_story",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )
    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    ingest_steps = {step["capability"]: step for step in tasks["ingest"]["tool_steps"]}
    supplement_steps = {
        step["capability"]: step for step in tasks["supplement"]["tool_steps"]
    }

    assert preflight["pipeline"]["pipeline_key"] == "hybrid_story"
    # Optional generation capabilities have no tools in this catalog, so the
    # preflight honestly reports degradation rather than readiness.
    assert preflight["overall_status"] == "degraded"
    assert execution_plan["can_execute"] is True
    assert manifest["pipeline"]["pipeline_key"] == "hybrid_story"
    assert ingest_steps["asset_ingestion"]["status"] == "planned"
    assert ingest_steps["asset_ingestion"]["auto_dispatch"] is False
    assert ingest_steps["media_analysis"]["status"] == "planned"
    assert ingest_steps["media_analysis"]["auto_dispatch"] is False
    # No generation tools exist in this catalog, so the supplemental steps are
    # honestly skipped instead of silently faked.
    assert supplement_steps["image_generation"]["status"] == "skipped"
    assert supplement_steps["video_generation"]["status"] == "skipped"
    assert supplement_steps["tts"]["status"] == "skipped"
    assert supplement_steps["music_generation"]["status"] == "skipped"


def test_execution_plan_records_selector_fallback_candidates(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(
            tmp_path,
            base_catalog()
            + [
                {
                    "tool_key": "local_mvp_avatar_preview",
                    "display_name": "Local MVP Avatar Preview",
                    "capability": "avatar_video",
                    "provider": "internal",
                    "runtime_type": "local",
                    "status": "degraded",
                    "quality_profile": {"tier": "preview"},
                }
            ],
        ),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "测试脚本", "quality_mode": "provider_avatar"},
    )

    plan = build_execution_plan(preflight_report=preflight)
    avatar_decision = next(
        decision
        for decision in plan["decision_log"]
        if decision["capability"] == "avatar_video"
    )

    assert avatar_decision["selected_tool_key"] == "dashscope_avatar_video"
    assert avatar_decision["selection_reason"] is not None
    assert avatar_decision["fallback_plan"][0]["tool_key"] == "local_mvp_avatar_preview"
    assert avatar_decision["fallback_plan"][0]["fallback_reason"] is not None
    assert avatar_decision["options_considered"][0]["selection_role"] == "selected"
    assert avatar_decision["options_considered"][1]["selection_role"] == "fallback"


def test_stage_execution_manifest_maps_plan_to_dispatch_queue(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    preflight = build_preflight_report(
        pipeline=pipeline,
        tools=registry.list_tools(),
        run_input={"raw_script": "调度测试"},
    )
    execution_plan = build_execution_plan(preflight_report=preflight)

    manifest = build_stage_execution_manifest(
        run_id="run_test",
        pipeline=pipeline,
        execution_plan=execution_plan,
        execution_plan_artifact_id="artifact_plan",
    )

    tasks = {task["stage_key"]: task for task in manifest["stage_tasks"]}
    plan_task = tasks["plan"]
    script_task = tasks["script"]
    asset_task = tasks["asset_generation"]
    export_task = tasks["export"]
    asset_steps = {
        step["capability"]: step
        for step in asset_task["tool_steps"]
    }

    assert manifest["source"]["artifact_id"] == "artifact_plan"
    assert manifest["summary"]["total_stage_tasks"] == len(pipeline.stages)
    assert manifest["summary"]["human_review_gates"] == 5
    assert plan_task["executor"] == "agent_runner"
    assert plan_task["review_gate"]["required_before_next_stage"] is True
    assert all(step["auto_dispatch"] is False for step in plan_task["tool_steps"])
    assert script_task["executor"] == "agent_runner"
    assert asset_task["executor"] == "tool_worker"
    assert asset_steps["avatar_video"]["auto_dispatch"] is True
    assert asset_steps["avatar_video"]["dispatch_target"] == "background_worker"
    avatar_decision = next(
        decision
        for decision in execution_plan["decision_log"]
        if decision["capability"] == "avatar_video"
    )
    assert avatar_decision["selection_reason"] is not None
    assert avatar_decision["selected_tool"]["selection_role"] == "selected"
    assert asset_steps["avatar_video"]["operations"]["submit"] is True
    assert asset_steps["avatar_video"]["retry_policy"]["timeout_seconds"] == 600
    assert "resource_profile" in asset_steps["avatar_video"]
    assert "quality_profile" in asset_steps["avatar_video"]
    assert asset_steps["subtitle_generation"]["auto_dispatch"] is True
    assert export_task["executor"] == "system_executor"


def test_database_store_persists_execution_plan_artifact_and_event(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'execution-plan.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=registry)
    project = store.create_project(ProjectCreate(title="Execution plan run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="执行方案测试"),
        pipeline=pipeline,
    )
    preflight = store.run_preflight(run.run_id)

    artifact = store.plan_execution(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id, event_type="run.execution_plan_created")
    session.close()

    assert preflight is not None
    assert artifact is not None
    assert artifact.artifact_type == "execution_plan"
    assert artifact.payload["execution_policy"] == "proceed_with_degradations"
    assert artifact.payload["can_execute"] is True
    assert any(
        decision["selected_tool_key"] == "dashscope_avatar_video"
        for decision in artifact.payload["decision_log"]
    )
    assert any(
        decision["decision"] == "skip_optional"
        for decision in artifact.payload["decision_log"]
    )
    assert artifacts is not None
    assert {stored.artifact_type for stored in artifacts} >= {
        "preflight_report",
        "execution_plan",
    }
    assert events is not None
    assert events[-1].payload["artifact_id"] == artifact.artifact_id
    assert events[-1].payload["execution_policy"] == "proceed_with_degradations"


def test_database_store_persists_stage_execution_manifest_artifact_and_event(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(tmp_path, base_catalog()),
        env={"DASHSCOPE_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'stage-execution.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=registry)
    project = store.create_project(ProjectCreate(title="Stage execution run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="执行队列测试"),
        pipeline=pipeline,
    )

    artifact = store.plan_stage_execution(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(
        run.run_id,
        event_type="run.stage_execution_manifest_created",
    )
    session.close()

    assert artifact is not None
    assert artifact.artifact_type == "stage_execution_manifest"
    assert artifact.payload["source"]["artifact_id"] is not None
    assert artifact.payload["stage_tasks"][4]["stage_key"] == "asset_generation"
    assert artifact.payload["stage_tasks"][4]["executor"] == "tool_worker"
    assert artifacts is not None
    assert {stored.artifact_type for stored in artifacts} >= {
        "preflight_report",
        "execution_plan",
        "stage_execution_manifest",
    }
    assert events is not None
    assert events[-1].payload["artifact_id"] == artifact.artifact_id
    assert events[-1].payload["total_stage_tasks"] == len(pipeline.stages)


def test_database_store_blocks_start_when_execution_plan_is_blocked(tmp_path) -> None:
    registry = ToolRegistry(
        catalog_paths=write_catalog(
            tmp_path,
            [
                tool
                for tool in base_catalog()
                if tool["tool_key"] != "dashscope_avatar_video"
            ]
            + [
                {
                    "tool_key": "dashscope_avatar_video",
                    "display_name": "DashScope Avatar Video",
                    "capability": "avatar_video",
                    "provider": "dashscope",
                    "runtime_type": "api",
                    "required_env_vars": ["DASHSCOPE_API_KEY"],
                }
            ],
        ),
        env={},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'blocked-start.db'}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=registry)
    project = store.create_project(ProjectCreate(title="Blocked run"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="阻塞启动测试"),
        pipeline=pipeline,
    )

    try:
        store.start_run(run.run_id)
    except ValueError as exc:
        error_message = str(exc)
    else:
        raise AssertionError("blocked execution plan should prevent start_run")

    blocked_run = store.get_run(run.run_id)
    artifacts = store.list_artifacts(run.run_id, current_only=True)
    events = store.list_events(run.run_id, event_type="run.start_blocked")
    session.close()

    assert "execution plan is blocked" in error_message
    assert blocked_run is not None
    assert blocked_run.status == RunStatus.DRAFT
    assert artifacts is not None
    assert {artifact.artifact_type for artifact in artifacts} >= {
        "execution_plan",
    }
    assert events is not None
    assert events[-1].payload["execution_policy"] == "blocked"
    assert events[-1].payload["stage_execution_manifest_artifact_id"] is not None
