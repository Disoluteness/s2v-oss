"""P4 selector explainability: seven dimensions, weights, continuity.

The hard constraint this module locks: the re-grouping must not move a single
selection. With the default weights the weighted sum of the seven dimensions is
bit-for-bit the pre-P4 total, because the old signals were re-grouped without
being re-valued. The legacy six-key ``score_breakdown`` stays verbatim so an
existing reader comparing breakdowns sees the same numbers.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import ProjectCreate, RunCreate
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.contracts import ToolDefinition, ToolStatus
from packages.tool_kit.execution_plan import build_execution_plan
from packages.tool_kit.registry import ToolRegistry
from packages.tool_kit.selector import (
    CONTINUITY_BONUS,
    DIMENSIONS,
    SELECTOR_VERSION,
    select_tool_for_capability,
)


def write_catalog(tmp_path, tools: list[dict]) -> list:
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    return [path]

DASHSCOPE_TTS = ToolDefinition(
    tool_key="dashscope_tts",
    display_name="DashScope TTS",
    capability="tts",
    provider="dashscope",
    runtime_type="api",
    status=ToolStatus.AVAILABLE,
    quality_profile={"tier": "standard"},
)

ELEVENLABS_TTS = ToolDefinition(
    tool_key="elevenlabs_tts",
    display_name="ElevenLabs TTS",
    capability="tts",
    provider="elevenlabs",
    runtime_type="api",
    status=ToolStatus.AVAILABLE,
    quality_profile={"tier": "standard"},
)


def _by_key(selection) -> dict:
    return {candidate.tool_key: candidate for candidate in selection.candidates}


def test_default_weights_reproduce_the_legacy_total_exactly() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS, ELEVENLABS_TTS],
        run_input={"provider_preferences": {"tts": "elevenlabs"}},
    )
    candidate = _by_key(selection)["elevenlabs_tts"]

    # The regrouped dimensions sum to the same total the six legacy keys sum to.
    assert abs(sum(candidate.dimension_scores.values()) - sum(candidate.score_breakdown.values())) < 1e-9
    # The legacy breakdown is verbatim — including the folded preference.
    assert candidate.score_breakdown["preference"] == 180
    assert candidate.dimension_scores["task_fit"] == 180
    # And the selection itself did not move.
    assert selection.selected_tool_key == "elevenlabs_tts"


def test_every_candidate_carries_all_seven_dimensions() -> None:
    selection = select_tool_for_capability(capability="tts", tools=[DASHSCOPE_TTS])
    candidate = selection.candidates[0]
    assert set(candidate.dimension_scores) == set(DIMENSIONS)
    assert candidate.strongest_dimension in DIMENSIONS
    assert candidate.dimension_scores["reliability"] == 600.0
    assert selection.dimension_weights == {dimension: 1.0 for dimension in DIMENSIONS}
    # The reason names the strongest dimension, in the board's language too.
    assert "reliability" in selection.selection_reason


def test_deterministic_bonus_lives_on_control_not_quality() -> None:
    deterministic = ToolDefinition(
        tool_key="local_tts",
        display_name="Local TTS",
        capability="tts",
        provider="internal",
        runtime_type="local",
        status=ToolStatus.AVAILABLE,
        quality_profile={"tier": "standard", "deterministic": True},
    )
    selection = select_tool_for_capability(capability="tts", tools=[deterministic])
    candidate = selection.candidates[0]
    assert candidate.dimension_scores["control"] == 10.0
    # The tier score alone, without the old +10 the legacy key still carries.
    assert candidate.dimension_scores["quality"] == 110.0
    assert candidate.score_breakdown["quality"] == 120.0
    assert abs(
        sum(candidate.dimension_scores.values()) - sum(candidate.score_breakdown.values())
    ) < 1e-9


def test_continuity_flips_a_tie_only_when_the_run_declares_it() -> None:
    baseline = select_tool_for_capability(capability="tts", tools=[DASHSCOPE_TTS, ELEVENLABS_TTS])
    # Without a declaration the candidates tie and the alphabetical tie-break wins.
    assert baseline.selected_tool_key == "dashscope_tts"
    assert all(candidate.dimension_scores["continuity"] == 0.0 for candidate in baseline.candidates)

    declared = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS, ELEVENLABS_TTS],
        run_input={"continuity_provider": "elevenlabs"},
    )
    winner = _by_key(declared)["elevenlabs_tts"]
    assert declared.selected_tool_key == "elevenlabs_tts"
    assert winner.dimension_scores["continuity"] == CONTINUITY_BONUS
    assert winner.strongest_dimension in DIMENSIONS


def test_continuity_map_scores_only_the_named_capability() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS],
        run_input={"continuity_providers": {"tts": "dashscope", "image_generation": "dashscope"}},
    )
    assert selection.candidates[0].dimension_scores["continuity"] == 45.0


def test_zero_weight_switches_a_dimension_off_without_hiding_the_signal() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS, ELEVENLABS_TTS],
        run_input={
            "continuity_provider": "dashscope",
            "dimension_weights": {"continuity": 0},
        },
    )
    candidates = _by_key(selection)
    # The weight is echoed, the signal is still computed (an auditor sees it),
    # but it no longer moves the total: the candidates tie again.
    assert selection.dimension_weights["continuity"] == 0.0
    assert candidates["dashscope_tts"].dimension_scores["continuity"] == CONTINUITY_BONUS
    assert abs(candidates["dashscope_tts"].score - candidates["elevenlabs_tts"].score) < 1e-9


def test_fractional_weight_scales_the_dimension_linearly() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS, ELEVENLABS_TTS],
        run_input={
            "continuity_provider": "dashscope",
            "dimension_weights": {"continuity": 0.5},
        },
    )
    candidates = _by_key(selection)
    gap = candidates["dashscope_tts"].score - candidates["elevenlabs_tts"].score
    assert abs(gap - CONTINUITY_BONUS * 0.5) < 1e-6


def test_unknown_weight_keys_are_ignored() -> None:
    selection = select_tool_for_capability(
        capability="tts",
        tools=[DASHSCOPE_TTS],
        run_input={"dimension_weights": {"cost": 2.0, "nope": 99}},
    )
    assert selection.dimension_weights["cost"] == 2.0
    assert selection.dimension_weights["task_fit"] == 1.0
    assert "nope" not in selection.dimension_weights


def test_selector_version_marks_the_explainability_bump() -> None:
    selection = select_tool_for_capability(capability="tts", tools=[DASHSCOPE_TTS])
    assert selection.selector_version == SELECTOR_VERSION == "1.1"


def test_execution_plan_options_carry_the_dimension_breakdown() -> None:
    """The plan is where an auditor meets the dimensions; they must ride along."""

    preflight = {
        "pipeline": {"pipeline_key": "spokesperson_ad"},
        "overall_status": "available",
        "generated_at": "2026-11-23T00:00:00+00:00",
        "stages": [
            {
                "stage_key": "narration",
                "display_name": "旁白",
                "order": 1,
                "status": "available",
                "requires_human_review": False,
                "selected_tools": ["elevenlabs_tts"],
                "blocked_capabilities": [],
                "degraded_capabilities": [],
                "missing_dependencies": [],
                "required_capabilities": [
                    {
                        "capability": "tts",
                        "required": True,
                        "status": "available",
                        "recommended_tool_key": "elevenlabs_tts",
                        "recommended_provider": "elevenlabs",
                        "selection_reason": "Selected elevenlabs_tts.",
                        "tools": [
                            {
                                "contract_version": "2.0",
                                "tool_key": "elevenlabs_tts",
                                "provider": "elevenlabs",
                                "status": "available",
                                "runtime_type": "api",
                                "runtime": "api",
                                "dependencies": [],
                                "missing_dependencies": [],
                                "fallback_tool_keys": [],
                                "operations": {},
                                "resource_profile": {},
                                "retry_policy": {},
                                "cost_model": {},
                                "quality_profile": {},
                                "selection": {
                                    "score": 850.0,
                                    "rank": 1,
                                    "selection_role": "selected",
                                    "selection_reason": "Selected elevenlabs_tts.",
                                    "score_breakdown": {"preference": 180.0},
                                    "dimension_scores": {"task_fit": 180.0, "cost": 100.0},
                                    "strongest_dimension": "task_fit",
                                    "dimension_weights": {dimension: 1.0 for dimension in DIMENSIONS},
                                },
                            }
                        ],
                    }
                ],
                "optional_capabilities": [],
            }
        ],
        "blocking_issues": [],
        "recommendations": [],
    }
    plan = build_execution_plan(preflight_report=preflight)
    option = plan["decision_log"][0]["options_considered"][0]
    assert option["dimension_scores"]["task_fit"] == 180.0
    assert option["strongest_dimension"] == "task_fit"
    assert option["dimension_weights"]["task_fit"] == 1.0
    # Legacy key still rides along for pre-1.0.3 readers.
    assert option["score_breakdown"]["preference"] == 180.0


def test_run_create_publishes_the_new_selector_keys() -> None:
    """The API layer must not swallow the P4 run inputs (review finding)."""

    payload = RunCreate(
        raw_script="维度测试",
        dimension_weights={"cost": 0, "continuity": 0.5},
        continuity_provider="dashscope",
        continuity_providers={"tts": "dashscope"},
    )
    dumped = payload.model_dump()
    # model_dump is exactly what becomes run.input_payload — the selector only
    # ever sees what survives this dict.
    assert dumped["dimension_weights"] == {"cost": 0, "continuity": 0.5}
    assert dumped["continuity_provider"] == "dashscope"
    assert dumped["continuity_providers"] == {"tts": "dashscope"}

    with pytest.raises(ValidationError):
        RunCreate(raw_script="维度测试", dimension_weights={"cost": -1})


def test_a_run_created_through_the_store_carries_dimensions_into_its_plan(tmp_path) -> None:
    """End to end: run input -> preflight -> execution plan -> dimensions."""

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
            {
                "tool_key": "elevenlabs_tts",
                "display_name": "ElevenLabs TTS",
                "capability": "tts",
                "provider": "elevenlabs",
                "runtime_type": "api",
                "required_env_vars": ["ELEVENLABS_API_KEY"],
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
        env={"DASHSCOPE_API_KEY": "present", "ELEVENLABS_API_KEY": "present"},
        binary_checker=lambda binary: "/usr/bin/ffmpeg" if binary == "ffmpeg" else None,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'dims.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(session, tool_registry=registry)
    project = store.create_project(ProjectCreate(title="维度透传"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="端到端维度", continuity_provider="dashscope"),
        pipeline=PipelineLoader().get_pipeline("spokesperson_ad"),
    )
    store.run_preflight(run.run_id)
    plan_artifact = store.plan_execution(run.run_id)
    session.close()

    assert plan_artifact is not None
    decisions = [
        decision
        for decision in plan_artifact.payload["decision_log"]
        if decision["capability"] == "tts"
    ]
    assert decisions, "the plan must decide about tts"
    decision = decisions[0]
    # Same-tier tie, flipped only by the declared continuity provider.
    assert decision["selected_tool_key"] == "dashscope_tts"
    candidates = {option["tool_key"]: option for option in decision["options_considered"]}
    winner = candidates["dashscope_tts"]
    assert winner["dimension_scores"]["continuity"] == CONTINUITY_BONUS
    assert candidates["elevenlabs_tts"]["dimension_scores"]["continuity"] == 0.0
    assert set(winner["dimension_scores"]) == set(DIMENSIONS)
    # Reliability (600 for an available tool) stays the strongest dimension;
    # continuity is what decided the pick, not what dominates the score.
    assert winner["strongest_dimension"] == "reliability"
    # Weights live on the selection result and are propagated to the options.
    assert winner["dimension_weights"]["continuity"] == 1.0
    assert decision["selector"]["dimension_weights"]["continuity"] == 1.0
    # The legacy breakdown rides along for pre-1.0.3 readers.
    assert set(winner["score_breakdown"]) == {
        "availability", "cost", "latency", "quality", "preference", "task_fit",
    }
