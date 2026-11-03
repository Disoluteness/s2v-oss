from packages.tool_kit.contracts import ToolDefinition, ToolStatus
from packages.tool_kit.selector import select_tool_for_capability


def test_selector_prefers_requested_provider_when_candidates_are_usable() -> None:
    tools = [
        ToolDefinition(
            tool_key="dashscope_tts",
            display_name="DashScope TTS",
            capability="tts",
            provider="dashscope",
            runtime_type="api",
            status=ToolStatus.AVAILABLE,
            quality_profile={"tier": "standard"},
        ),
        ToolDefinition(
            tool_key="elevenlabs_tts",
            display_name="ElevenLabs TTS",
            capability="tts",
            provider="elevenlabs",
            runtime_type="api",
            status=ToolStatus.AVAILABLE,
            quality_profile={"tier": "standard"},
        ),
    ]

    selection = select_tool_for_capability(
        capability="tts",
        tools=tools,
        run_input={"provider_preferences": {"tts": "elevenlabs"}},
    )

    assert selection.selector_key == "tts_selector"
    assert selection.selected_tool_key == "elevenlabs_tts"
    assert selection.selected_provider == "elevenlabs"
    assert selection.selection_reason is not None
    assert selection.candidates[0].selection_role == "selected"
    assert selection.candidates[0].score_breakdown["preference"] == 180
    assert selection.candidates[1].selection_role == "fallback"
    assert selection.candidates[1].fallback_reason is not None


def test_selector_uses_available_provider_before_degraded_fallback() -> None:
    tools = [
        ToolDefinition(
            tool_key="dashscope_avatar_video",
            display_name="DashScope Avatar Video",
            capability="avatar_video",
            provider="dashscope",
            runtime_type="api",
            status=ToolStatus.AVAILABLE,
            quality_profile={"tier": "standard"},
        ),
        ToolDefinition(
            tool_key="local_mvp_avatar_preview",
            display_name="Local MVP Avatar Preview",
            capability="avatar_video",
            provider="internal",
            runtime_type="local",
            status=ToolStatus.DEGRADED,
            quality_profile={"tier": "preview"},
        ),
    ]

    selection = select_tool_for_capability(
        capability="avatar_video",
        tools=tools,
        run_input={"quality_mode": "provider_avatar"},
    )

    assert selection.selector_key == "avatar_selector"
    assert selection.selected_tool_key == "dashscope_avatar_video"
    assert selection.fallback_tool_keys == ["local_mvp_avatar_preview"]
    assert selection.candidates[0].selection_role == "selected"
    assert selection.candidates[1].selection_role == "fallback"
    assert selection.candidates[1].fallback_reason is not None


def test_selector_does_not_select_unavailable_preference() -> None:
    tools = [
        ToolDefinition(
            tool_key="dashscope_tts",
            display_name="DashScope TTS",
            capability="tts",
            provider="dashscope",
            runtime_type="api",
            status=ToolStatus.UNAVAILABLE,
            missing_dependencies=["env:DASHSCOPE_API_KEY"],
        ),
        ToolDefinition(
            tool_key="local_tts",
            display_name="Local TTS",
            capability="tts",
            provider="internal",
            runtime_type="local",
            status=ToolStatus.DEGRADED,
        ),
    ]

    selection = select_tool_for_capability(
        capability="tts",
        tools=tools,
        run_input={"provider_preferences": {"tts": "dashscope"}},
    )

    assert selection.selected_tool_key == "local_tts"
    assert selection.candidates[0].tool_key == "local_tts"
    assert selection.candidates[0].selection_role == "selected"
    assert selection.candidates[1].tool_key == "dashscope_tts"
    assert selection.candidates[1].selection_role == "blocked"
    assert selection.candidates[1].eligible is False
