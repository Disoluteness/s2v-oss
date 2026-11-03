from pathlib import Path

from packages.pipeline_kit.loader import PipelineLoader


def test_load_avatar_spokesperson_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")

    assert pipeline.pipeline_key == "avatar_spokesperson"
    assert pipeline.display_name == "Avatar Spokesperson"
    assert pipeline.version == "0.4.0"
    assert pipeline.status == "production"
    assert pipeline.category == "avatar_spokesperson"
    assert pipeline.entrypoint_stage_key == "intake"
    assert pipeline.content_hash is not None
    assert len(pipeline.content_hash) == 64
    assert pipeline.metadata["legacy_pipeline_key"] == "spokesperson_ad"
    assert pipeline.metadata["roadmap_version"] == "V3.1"
    assert pipeline.metadata["supported_use_cases"] == [
        "brand_ad",
        "knowledge_spokesperson",
        "product_intro",
        "commerce_short_video",
    ]
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert pipeline.artifact_contracts["asset_manifest"]["schema_key"] == "asset_manifest.v1"
    assert pipeline.stages[0].stage_key == "intake"
    assert pipeline.stages[-1].stage_key == "export"
    assert any(stage.requires_human_review for stage in pipeline.stages)
    asset_stage = next(stage for stage in pipeline.stages if stage.stage_key == "asset_generation")
    assert asset_stage.dispatch_policy is not None
    assert asset_stage.dispatch_policy.mode == "stage_manifest"


def test_load_animated_explainer_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("animated_explainer")

    assert pipeline.pipeline_key == "animated_explainer"
    assert pipeline.display_name == "Animated Explainer"
    assert pipeline.version == "0.4.0"
    assert pipeline.status == "beta"
    assert pipeline.category == "animated_explainer"
    assert pipeline.metadata["roadmap_version"] == "V3.2"
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert "avatar_video" not in pipeline.required_capabilities
    assert {"subtitle_generation", "video_composition", "storage"}.issubset(
        set(pipeline.required_capabilities)
    )
    compose_stage = next(stage for stage in pipeline.stages if stage.stage_key == "compose")
    assert compose_stage.dispatch_policy is not None
    assert compose_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert compose_stage.required_capabilities == ["video_composition"]


def test_load_source_clip_factory_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("source_clip_factory")

    assert pipeline.pipeline_key == "source_clip_factory"
    assert pipeline.display_name == "Source Clip Factory"
    assert pipeline.version == "0.4.0"
    assert pipeline.status == "beta"
    assert pipeline.category == "source_clip_factory"
    assert pipeline.metadata["roadmap_version"] == "V3.3"
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert {"asset_ingestion", "media_analysis", "clip_selection", "clip_export", "storage"}.issubset(
        set(pipeline.required_capabilities)
    )
    assert pipeline.artifact_contracts["source_media_review"]["schema_key"] == (
        "source_media_review.v1"
    )
    assert pipeline.artifact_contracts["clip_export_manifest"]["schema_key"] == (
        "clip_export_manifest.v1"
    )
    export_stage = next(stage for stage in pipeline.stages if stage.stage_key == "export_clips")
    publish_stage = next(stage for stage in pipeline.stages if stage.stage_key == "publish")
    assert export_stage.dispatch_policy is not None
    assert export_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert export_stage.required_capabilities == ["clip_export"]
    assert publish_stage.dispatch_policy is not None
    assert publish_stage.dispatch_policy.auto_dispatch_required_tools is False


def test_load_screen_demo_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("screen_demo")

    assert pipeline.pipeline_key == "screen_demo"
    assert pipeline.display_name == "Screen Demo"
    assert pipeline.version == "0.4.0"
    assert pipeline.status == "beta"
    assert pipeline.category == "screen_demo"
    assert pipeline.metadata["roadmap_version"] == "V3.4"
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert {"browser_recording", "interaction_highlight", "video_composition", "storage"}.issubset(
        set(pipeline.required_capabilities)
    )
    assert pipeline.artifact_contracts["demo_flow_plan"]["schema_key"] == (
        "demo_flow_plan.v1"
    )
    assert pipeline.artifact_contracts["screen_capture_manifest"]["schema_key"] == (
        "screen_capture_manifest.v1"
    )
    capture_stage = next(stage for stage in pipeline.stages if stage.stage_key == "capture")
    compose_stage = next(stage for stage in pipeline.stages if stage.stage_key == "compose")
    export_stage = next(stage for stage in pipeline.stages if stage.stage_key == "export")
    assert capture_stage.dispatch_policy is not None
    assert capture_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert capture_stage.required_capabilities == ["browser_recording"]
    assert compose_stage.dispatch_policy is not None
    assert compose_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert export_stage.dispatch_policy is not None
    assert export_stage.dispatch_policy.auto_dispatch_required_tools is False


def test_load_localization_dub_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("localization_dub")

    assert pipeline.pipeline_key == "localization_dub"
    assert pipeline.display_name == "Localization Dub"
    assert pipeline.version == "0.1.0"
    assert pipeline.status == "beta"
    assert pipeline.category == "localization_dub"
    assert pipeline.metadata["roadmap_version"] == "V3.5"
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert {"media_analysis", "speech_to_text", "tts", "subtitle_generation", "video_composition", "storage"}.issubset(
        set(pipeline.required_capabilities)
    )
    assert pipeline.artifact_contracts["localization_plan"]["schema_key"] == (
        "localization_plan.v1"
    )
    assert pipeline.artifact_contracts["dubbing_manifest"]["schema_key"] == (
        "dubbing_manifest.v1"
    )
    dub_stage = next(stage for stage in pipeline.stages if stage.stage_key == "dub")
    compose_stage = next(stage for stage in pipeline.stages if stage.stage_key == "compose")
    export_stage = next(stage for stage in pipeline.stages if stage.stage_key == "export")
    assert dub_stage.dispatch_policy is not None
    assert dub_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert compose_stage.dispatch_policy is not None
    assert compose_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert export_stage.dispatch_policy is not None
    assert export_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert {requirement.capability for requirement in dub_stage.tool_requirements} >= {"tts"}


def test_load_hybrid_story_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("hybrid_story")

    assert pipeline.pipeline_key == "hybrid_story"
    assert pipeline.display_name == "Hybrid Story"
    assert pipeline.version == "0.1.0"
    assert pipeline.status == "beta"
    assert pipeline.category == "hybrid_story"
    assert pipeline.metadata["roadmap_version"] == "V3.6"
    assert Path(pipeline.metadata["strategy_document"]).is_file()
    assert {"asset_ingestion", "media_analysis", "video_composition", "storage"}.issubset(
        set(pipeline.required_capabilities)
    )
    assert pipeline.artifact_contracts["story_outline"]["schema_key"] == (
        "story_outline.v1"
    )
    ingest_stage = next(stage for stage in pipeline.stages if stage.stage_key == "ingest")
    compose_stage = next(stage for stage in pipeline.stages if stage.stage_key == "compose")
    assert ingest_stage.dispatch_policy is not None
    assert ingest_stage.dispatch_policy.auto_dispatch_required_tools is False
    assert ingest_stage.required_capabilities == ["asset_ingestion", "media_analysis"]
    assert compose_stage.dispatch_policy is not None
    assert compose_stage.dispatch_policy.auto_dispatch_required_tools is False


def test_load_spokesperson_pipeline() -> None:
    pipeline = PipelineLoader().get_pipeline("spokesperson_ad")

    assert pipeline.pipeline_key == "spokesperson_ad"
    assert pipeline.dsl_version == "1.0"
    assert pipeline.category == "avatar_spokesperson"
    assert pipeline.entrypoint_stage_key == "intake"
    assert pipeline.content_hash is not None
    assert len(pipeline.content_hash) == 64
    assert pipeline.artifact_contracts["spoken_script"]["schema_key"] == "spoken_script.v1"
    assert pipeline.stages[0].stage_key == "intake"
    assert pipeline.stages[-1].stage_key == "export"
    assert any(stage.requires_human_review for stage in pipeline.stages)
    asset_stage = next(stage for stage in pipeline.stages if stage.stage_key == "asset_generation")
    assert asset_stage.depends_on_stage_keys == ["storyboard"]
    assert {requirement.capability for requirement in asset_stage.tool_requirements} >= {
        "avatar_video",
        "subtitle_generation",
    }
    assert asset_stage.dispatch_policy is not None
    assert asset_stage.dispatch_policy.mode == "stage_manifest"


def test_list_pipelines_includes_legacy_and_formal_spokesperson_keys() -> None:
    pipeline_keys = {pipeline.pipeline_key for pipeline in PipelineLoader().list_pipelines()}

    assert {
        "animated_explainer",
        "avatar_spokesperson",
        "hybrid_story",
        "localization_dub",
        "screen_demo",
        "source_clip_factory",
        "spokesperson_ad",
    }.issubset(pipeline_keys)


def test_pipeline_snapshot_can_round_trip() -> None:
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    snapshot = pipeline.snapshot()

    restored = type(pipeline).model_validate(snapshot)

    assert restored.pipeline_key == pipeline.pipeline_key
    assert restored.version == pipeline.version
    assert restored.dsl_version == "1.0"
    assert restored.stages[0].stage_key == "intake"
    assert restored.stable_content_hash() == pipeline.stable_content_hash()
