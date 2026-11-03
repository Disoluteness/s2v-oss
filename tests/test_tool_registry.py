import json

from packages.tool_kit.contracts import RuntimeType, ToolDefinition, ToolStatus
from packages.tool_kit.registry import ToolRegistry


def write_catalog(tmp_path, tools: list[dict]) -> list:
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return [path]


def test_tool_registry_resolves_env_and_binary_status(tmp_path) -> None:
    catalog_paths = write_catalog(
        tmp_path,
        [
            {
                "tool_key": "api_tool",
                "display_name": "API Tool",
                "capability": "tts",
                "provider": "voiceco",
                "runtime_type": "api",
                "required_env_vars": ["VOICECO_API_KEY"],
            },
            {
                "tool_key": "local_tool",
                "display_name": "Local Tool",
                "capability": "video_composition",
                "provider": "local",
                "runtime_type": "local",
                "required_binaries": ["ffmpeg"],
            },
        ],
    )
    registry = ToolRegistry(
        catalog_paths=catalog_paths,
        env={"VOICECO_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )

    tools = registry.list_tools()

    assert {tool.tool_key: tool.status for tool in tools} == {
        "api_tool": ToolStatus.AVAILABLE,
        "local_tool": ToolStatus.AVAILABLE,
    }
    assert registry.get_tool("api_tool").status_reason == "Required API configuration is present."


def test_tool_registry_reports_missing_dependencies_and_filters(tmp_path) -> None:
    catalog_paths = write_catalog(
        tmp_path,
        [
            {
                "tool_key": "avatar_api",
                "display_name": "Avatar API",
                "capability": "avatar_video",
                "provider": "dashscope",
                "runtime_type": "api",
                "required_env_vars": ["DASHSCOPE_API_KEY"],
                "fallback_tool_keys": ["manual_upload"],
            },
            {
                "tool_key": "manual_upload",
                "display_name": "Manual Upload",
                "capability": "asset_ingestion",
                "provider": "internal",
                "runtime_type": "local",
            },
        ],
    )
    registry = ToolRegistry(catalog_paths=catalog_paths, env={}, binary_checker=lambda _: None)

    avatar_tools = registry.list_tools(capability="avatar_video")
    available_tools = registry.list_tools(status=ToolStatus.AVAILABLE)
    capability_summary = {
        summary.capability: summary for summary in registry.list_capabilities()
    }
    provider_summary = {summary.provider: summary for summary in registry.list_providers()}

    assert avatar_tools[0].status == ToolStatus.UNAVAILABLE
    assert avatar_tools[0].missing_dependencies == ["env:DASHSCOPE_API_KEY"]
    assert avatar_tools[0].fallback_tool_keys == ["manual_upload"]
    assert [tool.tool_key for tool in available_tools] == ["manual_upload"]
    assert capability_summary["avatar_video"].fallback_tool_keys == ["manual_upload"]
    assert capability_summary["asset_ingestion"].available == 1
    assert provider_summary["dashscope"].unavailable == 1
    assert provider_summary["internal"].available == 1


def test_tool_registry_uses_credential_env_overrides_and_disabled_providers(tmp_path) -> None:
    catalog_paths = write_catalog(
        tmp_path,
        [
            {
                "tool_key": "dashscope_tts",
                "display_name": "DashScope TTS",
                "capability": "tts",
                "provider": "dashscope",
                "runtime_type": "api",
                "required_env_vars": ["DASHSCOPE_API_KEY"],
            },
        ],
    )

    available_registry = ToolRegistry(
        catalog_paths=catalog_paths,
        env={},
        env_overrides={"DASHSCOPE_API_KEY": "db-key"},
        binary_checker=lambda _: None,
    )
    disabled_registry = ToolRegistry(
        catalog_paths=catalog_paths,
        env={"DASHSCOPE_API_KEY": "env-key"},
        disabled_providers={"dashscope"},
        binary_checker=lambda _: None,
    )

    assert available_registry.get_tool("dashscope_tts").status == ToolStatus.AVAILABLE
    disabled_tool = disabled_registry.get_tool("dashscope_tts")
    assert disabled_tool.status == ToolStatus.UNAVAILABLE
    assert disabled_tool.missing_dependencies == ["provider_disabled:dashscope"]
    assert disabled_tool.status_reason == (
        "Provider is disabled by credential manager: dashscope"
    )


def test_tool_definition_normalizes_v2_contract_fields_from_legacy_catalog() -> None:
    definition = ToolDefinition.model_validate(
        {
            "tool_key": "dashscope_tts",
            "display_name": "DashScope TTS",
            "capability": "tts",
            "provider": "dashscope",
            "runtime": "api",
            "required_env_vars": ["DASHSCOPE_API_KEY"],
            "input_contract": {
                "required": ["text"],
                "properties": {"text": "Text to synthesize."},
            },
            "output_contract": {
                "asset_type": "tts_audio",
                "mime_type": "audio/wav",
            },
            "pricing": {"unit": "character"},
            "timeout_seconds": 120,
        }
    )

    assert definition.contract_version == "2.0"
    assert definition.runtime_type == RuntimeType.API
    assert definition.runtime == RuntimeType.API
    assert definition.dependencies[0].model_dump() == {
        "kind": "env",
        "name": "DASHSCOPE_API_KEY",
        "required": True,
        "description": None,
    }
    assert definition.input_schema["required"] == ["text"]
    assert definition.input_schema["properties"]["text"]["description"] == "Text to synthesize."
    assert definition.output_schema["asset_type"] == "tts_audio"
    assert definition.cost_model.pricing == {"unit": "character"}
    assert definition.retry_policy.timeout_seconds == 120
    assert definition.operations.validate_ is True
    assert definition.operations.submit is True


def test_default_tool_registry_catalog_loads_project_tools() -> None:
    registry = ToolRegistry(env={}, binary_checker=lambda _: None)
    tools = registry.list_tools()
    capabilities = {summary.capability for summary in registry.list_capabilities()}
    avatar_tool = next(tool for tool in tools if tool.tool_key == "dashscope_avatar_video")

    assert any(tool.tool_key == "rule_based_planner" for tool in tools)
    assert any(tool.tool_key == "dashscope_avatar_video" for tool in tools)
    assert any(tool.tool_key == "ffmpeg_media_probe" for tool in tools)
    assert any(tool.tool_key == "html_canvas_renderer" for tool in tools)
    assert any(tool.tool_key == "local_clip_selector" for tool in tools)
    assert any(tool.tool_key == "ffmpeg_clip_export" for tool in tools)
    assert any(tool.tool_key == "local_demo_recording_placeholder" for tool in tools)
    assert any(tool.tool_key == "local_interaction_highlight_planner" for tool in tools)
    assert "avatar_video" in capabilities
    assert "browser_recording" in capabilities
    assert "clip_export" in capabilities
    assert "clip_selection" in capabilities
    assert "interaction_highlight" in capabilities
    assert "media_analysis" in capabilities
    assert "screen_capture" in capabilities
    assert "video_composition" in capabilities
    assert all(tool.contract_version == "2.0" for tool in tools)
    assert avatar_tool.runtime == RuntimeType.API
    assert avatar_tool.dependencies[0].model_dump(exclude_none=True) == {
        "kind": "env",
        "name": "DASHSCOPE_API_KEY",
        "required": True,
    }
    # Only the two media URLs are required now: the default model (wan2.2-s2v)
    # takes the raw image and never asks for a bbox.
    assert avatar_tool.input_schema["required"] == [
        "image_url",
        "audio_url",
    ]
    assert "resolution" in avatar_tool.input_schema["properties"]
    assert avatar_tool.retry_policy.timeout_seconds == 600
    assert avatar_tool.resource_profile.network_required is True
    assert avatar_tool.operations.poll is True
    assert avatar_tool.operations.archive is True
    assert avatar_tool.quality_profile.tier == "draft"
