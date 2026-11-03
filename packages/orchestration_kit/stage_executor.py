from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.pipeline_kit.models import PipelineDefinition, StageDefinition

AGENT_CONTROLLED_CAPABILITIES = {"llm_generation"}
INLINE_SYSTEM_CAPABILITIES = {"storage"}


def build_stage_execution_manifest(
    *,
    run_id: str,
    pipeline: PipelineDefinition,
    execution_plan: dict[str, Any],
    execution_plan_artifact_id: str | None = None,
) -> dict[str, Any]:
    """Build a concrete, auditable stage execution queue from an execution plan."""

    stage_plans = {
        stage["stage_key"]: stage
        for stage in execution_plan.get("stage_plans", [])
    }
    decisions = {
        decision["decision_id"]: decision
        for decision in execution_plan.get("decision_log", [])
    }
    stage_tasks = [
        _stage_task(
            stage=stage,
            stage_plan=stage_plans.get(stage.stage_key, {}),
            decisions=decisions,
            index=index,
        )
        for index, stage in enumerate(pipeline.ordered_stages(), start=1)
    ]

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "pipeline": {
            "pipeline_key": pipeline.pipeline_key,
            "display_name": pipeline.display_name,
            "version": pipeline.version,
        },
        "source": {
            "artifact_type": "execution_plan",
            "artifact_id": execution_plan_artifact_id,
            "execution_policy": execution_plan.get("execution_policy"),
            "can_execute": execution_plan.get("can_execute", False),
        },
        "summary": _summary(stage_tasks),
        "stage_tasks": stage_tasks,
    }


def _stage_task(
    *,
    stage: StageDefinition,
    stage_plan: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    stage_decisions = [
        decisions[decision_id]
        for decision_id in stage_plan.get("decisions", [])
        if decision_id in decisions
    ]
    tool_steps = [_tool_step(stage, decision) for decision in stage_decisions]
    execution_mode = stage_plan.get("execution_mode", "automatic")
    is_blocked = execution_mode == "blocked"

    return {
        "task_id": f"task_{stage.order:02d}_{stage.stage_key}",
        "stage_key": stage.stage_key,
        "display_name": stage.display_name,
        "order": stage.order,
        "status": "blocked" if is_blocked else "planned",
        "ready_to_dispatch": index == 1 and not is_blocked,
        "executor": _stage_executor(stage=stage, execution_mode=execution_mode, tool_steps=tool_steps),
        "execution_mode": execution_mode,
        "preflight_status": stage_plan.get("preflight_status"),
        "required_input_artifacts": stage.required_input_artifacts,
        "output_artifact_types": stage.output_artifact_types,
        "timeout_seconds": stage.timeout_seconds,
        "depends_on_stage_keys": [],
        "requires_human_review": stage.requires_human_review,
        "skippable": stage.skippable,
        "skip_policy": stage.skip_policy,
        "review_gate": _review_gate(stage) if stage.requires_human_review else None,
        "selected_tools": stage_plan.get("selected_tools", {}),
        "decision_ids": stage_plan.get("decisions", []),
        "tool_steps": tool_steps,
        "blocking_capabilities": stage_plan.get("blocking_capabilities", []),
        "degraded_capabilities": stage_plan.get("degraded_capabilities", []),
    }


def _tool_step(stage: StageDefinition, decision: dict[str, Any]) -> dict[str, Any]:
    capability = decision["capability"]
    auto_dispatch = _auto_dispatch(stage=stage, decision=decision)
    selected_tool = decision.get("selected_tool") or {}
    return {
        "step_id": f"{stage.stage_key}.tool.{capability}",
        "step_type": "tool_call",
        "capability": capability,
        "required": decision["required"],
        "status": _tool_step_status(decision),
        "auto_dispatch": auto_dispatch,
        "dispatch_target": _dispatch_target(capability),
        "tool_key": decision.get("selected_tool_key"),
        "provider": decision.get("selected_provider"),
        "decision_id": decision["decision_id"],
        "decision": decision["decision"],
        "preflight_status": decision.get("preflight_status"),
        "operations": selected_tool.get("operations", {}),
        "resource_profile": selected_tool.get("resource_profile", {}),
        "retry_policy": selected_tool.get("retry_policy", {}),
        "cost_model": selected_tool.get("cost_model", {}),
        "quality_profile": selected_tool.get("quality_profile", {}),
        "degraded": decision["decision"] == "use_degraded_tool",
        "missing_dependencies": decision.get("missing_dependencies", []),
    }


def _auto_dispatch(*, stage: StageDefinition, decision: dict[str, Any]) -> bool:
    if decision["decision"] not in {"use_tool", "use_degraded_tool"}:
        return False
    if not decision["required"]:
        return False
    if stage.dispatch_policy and not stage.dispatch_policy.auto_dispatch_required_tools:
        return False
    return decision["capability"] not in AGENT_CONTROLLED_CAPABILITIES


def _tool_step_status(decision: dict[str, Any]) -> str:
    if decision["decision"] == "blocked":
        return "blocked"
    if decision["decision"] == "skip_optional":
        return "skipped"
    return "planned"


def _dispatch_target(capability: str) -> str:
    if capability in AGENT_CONTROLLED_CAPABILITIES:
        return "agent_runner"
    if capability in INLINE_SYSTEM_CAPABILITIES:
        return "inline_system"
    return "background_worker"


def _stage_executor(
    *,
    stage: StageDefinition,
    execution_mode: str,
    tool_steps: list[dict[str, Any]],
) -> str:
    if execution_mode == "blocked":
        return "blocked"
    auto_dispatch_targets = {
        step["dispatch_target"]
        for step in tool_steps
        if step["auto_dispatch"]
    }
    if "background_worker" in auto_dispatch_targets:
        return "tool_worker"
    if "inline_system" in auto_dispatch_targets:
        return "system_executor"
    if stage.output_artifact_types:
        return "agent_runner"
    return "noop"


def _review_gate(stage: StageDefinition) -> dict[str, Any]:
    return {
        "status": "planned",
        "gate_type": "human_review",
        "stage_key": stage.stage_key,
        "required_before_next_stage": True,
    }


def _summary(stage_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    tool_steps = [
        step
        for task in stage_tasks
        for step in task["tool_steps"]
    ]
    return {
        "total_stage_tasks": len(stage_tasks),
        "blocked_stage_tasks": sum(1 for task in stage_tasks if task["status"] == "blocked"),
        "human_review_gates": sum(1 for task in stage_tasks if task["review_gate"] is not None),
        "tool_steps": len(tool_steps),
        "auto_dispatch_tool_steps": sum(1 for step in tool_steps if step["auto_dispatch"]),
    }
