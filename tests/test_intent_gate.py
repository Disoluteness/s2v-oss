"""T75: a declared quality mode must never be downgraded silently.

The registry used here is built with an empty environment and a binary checker
that reports every binary as present. That combination is exactly the situation
T75 is about: provider tools are unavailable because no key is configured, while
the local tools are perfectly usable — so nothing stops the run, and unless the
plan says something the caller never learns the quality changed.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models, routes  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.models import ProjectCreate, RunCreate, RunStatus, StartRunRequest
from apps.api.app.routes import start_run as start_run_route
from apps.api.app.run_gate import RunGateError
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.contracts import ToolDefinition, ToolStatus
from packages.tool_kit.execution_plan import build_execution_plan
from packages.tool_kit.preflight import build_preflight_report
from packages.tool_kit.registry import ToolRegistry
from packages.tool_kit.selector import select_tool_for_capability

WEB_ROOT = Path(__file__).resolve().parents[1] / "apps" / "web"


def _api_tool(
    tool_key: str,
    capability: str,
    provider: str = "dashscope",
    status: ToolStatus = ToolStatus.UNAVAILABLE,
) -> ToolDefinition:
    return ToolDefinition(
        tool_key=tool_key,
        display_name=tool_key,
        capability=capability,
        provider=provider,
        runtime_type="api",
        status=status,
        quality_profile={"tier": "standard"},
    )


def _local_tool(
    tool_key: str,
    capability: str,
    status: ToolStatus = ToolStatus.AVAILABLE,
) -> ToolDefinition:
    return ToolDefinition(
        tool_key=tool_key,
        display_name=tool_key,
        capability=capability,
        provider="internal",
        runtime_type="local",
        status=status,
        quality_profile={"tier": "preview"},
    )


def _isolated_registry() -> ToolRegistry:
    """A registry where no provider key exists but every binary does."""
    return ToolRegistry(env={}, binary_checker=lambda name: f"/fake/bin/{name}")


def _store_with_run(tmp_path, quality_mode: str | None, name: str = "intent-gate.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    store = DatabaseStore(Session(engine), tool_registry=_isolated_registry())
    project = store.create_project(ProjectCreate(title="intent gate"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(
            raw_script="秋天第一杯奶茶，还没人给你点？",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            quality_mode=quality_mode,
        ),
        pipeline=PipelineLoader().get_pipeline("avatar_spokesperson"),
    )
    return store, run


# --------------------------------------------------------------------------- #
# selector
# --------------------------------------------------------------------------- #


def test_unstated_quality_mode_makes_no_assertion() -> None:
    selection = select_tool_for_capability(
        capability="avatar_video",
        tools=[_local_tool("local_avatar", "avatar_video")],
        run_input={},
    )

    assert selection.intent is None
    assert selection.intent_satisfied is True
    assert selection.intent_mismatch_kind is None


def test_provider_intent_flags_a_local_fallback() -> None:
    selection = select_tool_for_capability(
        capability="avatar_video",
        tools=[
            _api_tool("dashscope_avatar_video", "avatar_video"),
            _local_tool("local_avatar", "avatar_video", status=ToolStatus.DEGRADED),
        ],
        run_input={"quality_mode": "provider_avatar"},
    )

    assert selection.selected_tool_key == "local_avatar"
    assert selection.intent_satisfied is False
    assert selection.intent_mismatch_kind == "runtime_downgrade"
    assert selection.intent_mismatch_reason is not None
    assert "dashscope_avatar_video" in selection.intent_mismatch_reason


def test_declared_capability_without_any_usable_tool_is_flagged() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[_api_tool("dashscope_tts", "tts")],
        run_input={"quality_mode": "provider_audio"},
    )

    assert selection.selected_tool_key is None
    assert selection.intent_satisfied is False
    assert selection.intent_mismatch_kind == "capability_missing"


def test_same_runtime_swap_is_not_a_downgrade() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[
            _api_tool("dashscope_tts", "tts"),
            _api_tool("elevenlabs_tts", "tts", provider="elevenlabs", status=ToolStatus.AVAILABLE),
        ],
        run_input={"quality_mode": "provider_audio"},
    )

    assert selection.selected_tool_key == "elevenlabs_tts"
    assert selection.intent_satisfied is True
    assert selection.intent_mismatch_kind is None


def test_local_preview_is_satisfied_by_a_local_tool() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[_local_tool("local_tts", "tts")],
        run_input={"quality_mode": "local_preview"},
    )

    assert selection.selected_tool_key == "local_tts"
    assert selection.intent_satisfied is True


# --------------------------------------------------------------------------- #
# preflight + execution plan
# --------------------------------------------------------------------------- #


def _pipeline_capabilities(pipeline) -> list[str]:
    capabilities = set(pipeline.required_capabilities) | set(pipeline.optional_capabilities)
    for stage in pipeline.stages:
        capabilities.update(stage.required_capabilities)
        capabilities.update(stage.optional_capabilities)
    return sorted(capabilities)


def _plan_for(quality_mode: str | None) -> tuple[dict, dict]:
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    # Every capability gets a usable local tool, so the plan is never blocked for
    # unrelated reasons; avatar_video additionally has a provider tool that is
    # registered but unusable, which is the downgrade under test.
    tools = [_local_tool(f"local_{capability}", capability) for capability in _pipeline_capabilities(pipeline)]
    tools.append(_api_tool("dashscope_avatar_video", "avatar_video"))
    run_input: dict = {"raw_script": "测试脚本"}
    if quality_mode is not None:
        run_input["quality_mode"] = quality_mode
    report = build_preflight_report(pipeline=pipeline, tools=tools, run_input=run_input)
    return report, build_execution_plan(preflight_report=report)


def test_preflight_records_the_intent_warning_per_stage() -> None:
    report, _plan = _plan_for("provider_avatar")

    asset_generation = next(
        stage for stage in report["stages"] if stage["stage_key"] == "asset_generation"
    )
    assert "avatar_video" in asset_generation["intent_mismatch_capabilities"]
    warning = next(
        item
        for item in asset_generation["intent_warnings"]
        if item["capability"] == "avatar_video"
    )
    assert warning["kind"] == "runtime_downgrade"
    assert warning["expected_runtime"] == "api"
    assert warning["quality_mode"] == "provider_avatar"
    assert warning["message"]
    # The capability is still usable, so it must not be reported as blocked.
    assert asset_generation["status"] != "blocked"
    assert report["overall_status"] != "blocked"


def test_execution_plan_requires_acknowledgement_for_a_downgrade() -> None:
    _report, plan = _plan_for("provider_avatar")

    assert plan["can_execute"] is True
    assert plan["execution_policy"] == "proceed_with_intent_downgrades"
    assert plan["requires_intent_acknowledgement"] is True
    assert "avatar_video" in plan["intent_downgrade_capabilities"]

    entry = next(
        item for item in plan["intent_downgrades"] if item["capability"] == "avatar_video"
    )
    assert entry["kind"] == "runtime_downgrade"
    assert entry["expected_runtime"] == "api"
    assert entry["selected_tool_key"] == "local_avatar_video"
    assert entry["selected_runtime"] == "local"

    decision = next(
        item
        for item in plan["decision_log"]
        if item["capability"] == "avatar_video" and item["stage_key"] == "asset_generation"
    )
    assert decision["decision"] == "use_tool_below_intent"
    assert decision["intent_satisfied"] is False
    assert decision["intent_mismatch_kind"] == "runtime_downgrade"


def test_plan_without_a_declared_quality_mode_is_unchanged() -> None:
    _report, plan = _plan_for(None)

    assert plan["intent_downgrades"] == []
    assert plan["intent_downgrade_capabilities"] == []
    assert plan["requires_intent_acknowledgement"] is False
    assert plan["execution_policy"] in {"proceed", "proceed_with_degradations"}


# --------------------------------------------------------------------------- #
# run start gate
# --------------------------------------------------------------------------- #


def test_start_blocks_a_silent_quality_downgrade(tmp_path) -> None:
    store, run = _store_with_run(tmp_path, "provider_avatar")

    with pytest.raises(RunGateError) as exc_info:
        store.start_run(run.run_id)

    error = exc_info.value
    assert error.code == "intent_downgrade_not_acknowledged"
    assert error.status_code == 400
    assert error.details["execution_policy"] == "proceed_with_intent_downgrades"
    assert error.details["resolution"]["body"] == {"accept_intent_downgrade": True}
    assert any(
        item["capability"] == "avatar_video" for item in error.details["intent_downgrades"]
    )

    blocked_events = [
        event
        for event in store.list_events(run.run_id, limit=100)
        if event.event_type == "run.start_blocked"
    ]
    assert blocked_events
    assert blocked_events[-1].payload["code"] == "intent_downgrade_not_acknowledged"


def test_start_proceeds_once_the_downgrade_is_acknowledged(tmp_path) -> None:
    store, run = _store_with_run(tmp_path, "provider_avatar", name="ack.db")

    started = store.start_run(run.run_id, accept_intent_downgrade=True)

    assert started is not None
    assert started.status is not RunStatus.DRAFT
    assert "run.intent_downgrade_accepted" in {
        event.event_type for event in store.list_events(run.run_id, limit=100)
    }


def test_run_without_a_declared_quality_mode_starts_normally(tmp_path) -> None:
    store, run = _store_with_run(tmp_path, None, name="no-intent.db")

    started = store.start_run(run.run_id)

    assert started is not None
    assert started.status is not RunStatus.DRAFT


# --------------------------------------------------------------------------- #
# http layer
# --------------------------------------------------------------------------- #


def test_start_endpoint_returns_the_structured_refusal(tmp_path) -> None:
    store, run = _store_with_run(tmp_path, "provider_avatar", name="api-blocked.db")

    with pytest.raises(HTTPException) as exc_info:
        start_run_route(run.run_id, store, StartRunRequest())

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "intent_downgrade_not_acknowledged"
    assert detail["intent_downgrades"]
    assert detail["resolution"]["body"] == {"accept_intent_downgrade": True}


def test_start_endpoint_accepts_an_acknowledgement(tmp_path) -> None:
    store, run = _store_with_run(tmp_path, "provider_avatar", name="api-accepted.db")

    result = start_run_route(run.run_id, store, StartRunRequest(accept_intent_downgrade=True))

    assert result["status"] != RunStatus.DRAFT.value


def test_start_route_treats_the_body_as_optional() -> None:
    operation = app.openapi()["paths"]["/api/runs/{run_id}/start"]["post"]

    assert operation["requestBody"].get("required", False) is False


# --------------------------------------------------------------------------- #
# web layer
# --------------------------------------------------------------------------- #


def _page(name: str) -> str:
    return (WEB_ROOT / name).read_text(encoding="utf-8")


def test_workspace_explains_the_downgrade_and_asks_for_confirmation() -> None:
    html = _page("review.html")

    assert "质量降级" in html
    assert "accept_intent_downgrade" in html
    assert "intent_downgrade_not_acknowledged" in html
    assert "声明的质量模式" in html


def test_run_board_shows_the_downgrade_reason() -> None:
    html = _page("run_board.html")

    assert "质量降级" in html
    assert "intent_downgrades" in html
