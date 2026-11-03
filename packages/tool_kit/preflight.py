from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from packages.pipeline_kit.models import PipelineDefinition, StageDefinition
from packages.tool_kit.contracts import ToolDefinition, ToolStatus
from packages.tool_kit.selector import ToolSelectionCandidate, select_tool_for_capability


def build_preflight_report(
    *,
    pipeline: PipelineDefinition,
    tools: Iterable[ToolDefinition],
    run_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a pipeline capability audit from registered runtime tools."""

    tool_list = sorted(tools, key=lambda tool: (tool.capability, tool.provider, tool.tool_key))
    capability_reports = _capability_reports(
        pipeline=pipeline,
        tools=tool_list,
        run_input=run_input or {},
    )
    stage_reports = [
        _stage_report(stage=stage, capability_reports=capability_reports)
        for stage in pipeline.ordered_stages()
    ]
    overall_status = _overall_status(
        pipeline=pipeline,
        capability_reports=capability_reports,
        stage_reports=stage_reports,
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "pipeline": {
            "pipeline_key": pipeline.pipeline_key,
            "display_name": pipeline.display_name,
            "version": pipeline.version,
            "status": pipeline.status,
        },
        "run_input": run_input or {},
        "overall_status": overall_status,
        "summary": _summary(overall_status, stage_reports),
        "required_capabilities": [
            capability_reports[capability]
            for capability in sorted(set(pipeline.required_capabilities))
        ],
        "optional_capabilities": [
            capability_reports[capability]
            for capability in sorted(set(pipeline.optional_capabilities))
        ],
        "stages": stage_reports,
        "blocking_issues": _blocking_issues(
            pipeline=pipeline,
            stage_reports=stage_reports,
            capability_reports=capability_reports,
        ),
        "recommendations": _recommendations(
            stage_reports=stage_reports,
            capability_reports=capability_reports,
        ),
    }


def _capability_reports(
    *,
    pipeline: PipelineDefinition,
    tools: list[ToolDefinition],
    run_input: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    capabilities = set(pipeline.required_capabilities) | set(pipeline.optional_capabilities)
    for stage in pipeline.stages:
        capabilities.update(stage.required_capabilities)
        capabilities.update(stage.optional_capabilities)

    tools_by_capability: dict[str, list[ToolDefinition]] = {}
    for tool in tools:
        tools_by_capability.setdefault(tool.capability, []).append(tool)

    return {
        capability: _capability_report(
            capability,
            tools_by_capability.get(capability, []),
            run_input=run_input,
        )
        for capability in sorted(capabilities)
    }


def _capability_report(
    capability: str,
    tools: list[ToolDefinition],
    *,
    run_input: dict[str, Any],
) -> dict[str, Any]:
    available = [tool for tool in tools if tool.status == ToolStatus.AVAILABLE]
    degraded = [tool for tool in tools if tool.status == ToolStatus.DEGRADED]
    unavailable = [tool for tool in tools if tool.status == ToolStatus.UNAVAILABLE]
    selection = select_tool_for_capability(
        capability=capability,
        tools=tools,
        run_input=run_input,
    )
    recommended = next(
        (tool for tool in tools if tool.tool_key == selection.selected_tool_key),
        None,
    )
    selection_candidates = {
        candidate.tool_key: candidate for candidate in selection.candidates
    }
    if recommended and recommended.status == ToolStatus.AVAILABLE:
        status = "available"
    elif recommended and recommended.status == ToolStatus.DEGRADED:
        status = "degraded"
    else:
        status = "unavailable"
    declared_fallbacks = sorted(
        {fallback for tool in tools for fallback in tool.fallback_tool_keys}
    )
    return {
        "capability": capability,
        "status": status,
        "recommended_tool_key": recommended.tool_key if recommended else None,
        "recommended_provider": recommended.provider if recommended else None,
        "selection_reason": selection.selection_reason,
        "available_tool_keys": [tool.tool_key for tool in available],
        "degraded_tool_keys": [tool.tool_key for tool in degraded],
        "unavailable_tool_keys": [tool.tool_key for tool in unavailable],
        "fallback_tool_keys": sorted(
            set(selection.fallback_tool_keys) | set(declared_fallbacks)
        ),
        "missing_dependencies": _unique(
            dependency for tool in unavailable for dependency in tool.missing_dependencies
        ),
        "selector": selection.model_dump(),
        "intent": selection.intent,
        "intent_satisfied": selection.intent_satisfied,
        "intent_mismatch_kind": selection.intent_mismatch_kind,
        "intent_mismatch_reason": selection.intent_mismatch_reason,
        "tools": [
            _tool_report(tool, selection=selection_candidates.get(tool.tool_key))
            for tool in tools
        ],
    }


def _tool_report(
    tool: ToolDefinition,
    *,
    selection: ToolSelectionCandidate | None = None,
) -> dict[str, Any]:
    runtime = tool.runtime or tool.runtime_type
    report = {
        "contract_version": tool.contract_version,
        "tool_key": tool.tool_key,
        "display_name": tool.display_name,
        "capability": tool.capability,
        "provider": tool.provider,
        "status": tool.status.value,
        "runtime": runtime.value,
        "runtime_type": tool.runtime_type.value,
        "dependencies": [
            dependency.model_dump(exclude_none=True)
            for dependency in tool.dependencies
        ],
        "missing_dependencies": tool.missing_dependencies,
        "fallback_tool_keys": tool.fallback_tool_keys,
        "status_reason": tool.status_reason,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "resource_profile": tool.resource_profile.model_dump(exclude_none=True),
        "retry_policy": tool.retry_policy.model_dump(exclude_none=True),
        "cost_model": tool.cost_model.model_dump(exclude_none=True),
        "quality_profile": tool.quality_profile.model_dump(exclude_none=True),
        "operations": tool.operations.model_dump(),
    }
    if selection is not None:
        report["selection"] = selection.model_dump()
    return report


def _stage_report(
    *,
    stage: StageDefinition,
    capability_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    required = [capability_reports[capability] for capability in stage.required_capabilities]
    optional = [capability_reports[capability] for capability in stage.optional_capabilities]
    blocked_capabilities = [
        report["capability"] for report in required if report["status"] == "unavailable"
    ]
    degraded_capabilities = [
        report["capability"]
        for report in required
        if report["status"] == "degraded"
    ] + [
        report["capability"]
        for report in optional
        if report["status"] != "available"
    ]
    if blocked_capabilities:
        status = "blocked"
    elif degraded_capabilities:
        status = "degraded"
    else:
        status = "ready"
    # A capability can be "available" and still not be what the run asked for:
    # the selector falls back to a local tool when the requested provider is not
    # configured. That mismatch is tracked separately so it never hides inside a
    # ready/degraded verdict.
    intent_mismatch_capabilities = sorted(
        report["capability"]
        for report in required + optional
        if not report["intent_satisfied"]
    )
    return {
        "stage_key": stage.stage_key,
        "display_name": stage.display_name,
        "order": stage.order,
        "status": status,
        "requires_human_review": stage.requires_human_review,
        "required_capabilities": required,
        "optional_capabilities": optional,
        "selected_tools": {
            report["capability"]: report["recommended_tool_key"]
            for report in required + optional
            if report["recommended_tool_key"]
        },
        "blocked_capabilities": blocked_capabilities,
        "degraded_capabilities": sorted(set(degraded_capabilities)),
        "intent_mismatch_capabilities": intent_mismatch_capabilities,
        "intent_warnings": [
            {
                "capability": report["capability"],
                "kind": report["intent_mismatch_kind"],
                "quality_mode": (report["intent"] or {}).get("quality_mode"),
                "expected_runtime": (report["intent"] or {}).get("expected_runtime"),
                "selected_tool_key": report["recommended_tool_key"],
                "selected_provider": report["recommended_provider"],
                "message": report["intent_mismatch_reason"],
            }
            for report in required + optional
            if not report["intent_satisfied"]
        ],
        "missing_dependencies": _unique(
            dependency
            for report in required
            for dependency in report["missing_dependencies"]
        ),
    }


def _overall_status(
    *,
    pipeline: PipelineDefinition,
    capability_reports: dict[str, dict[str, Any]],
    stage_reports: list[dict[str, Any]],
) -> str:
    pipeline_required_blocked = any(
        capability_reports[capability]["status"] == "unavailable"
        for capability in pipeline.required_capabilities
    )
    if pipeline_required_blocked or any(stage["status"] == "blocked" for stage in stage_reports):
        return "blocked"
    if any(stage["status"] == "degraded" for stage in stage_reports):
        return "degraded"
    return "ready"


def _blocking_issues(
    *,
    pipeline: PipelineDefinition,
    stage_reports: list[dict[str, Any]],
    capability_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    issues = []
    for capability in sorted(set(pipeline.required_capabilities)):
        report = capability_reports[capability]
        if report["status"] == "unavailable":
            issues.append(
                {
                    "scope": "pipeline",
                    "capability": capability,
                    "missing_dependencies": report["missing_dependencies"],
                    "message": f"Required pipeline capability is unavailable: {capability}",
                }
            )
    for stage in stage_reports:
        for capability in stage["blocked_capabilities"]:
            report = capability_reports[capability]
            issues.append(
                {
                    "scope": "stage",
                    "stage_key": stage["stage_key"],
                    "capability": capability,
                    "missing_dependencies": report["missing_dependencies"],
                    "message": (
                        f"Stage {stage['stage_key']} requires unavailable "
                        f"capability: {capability}"
                    ),
                }
            )
    return issues


def _recommendations(
    *,
    stage_reports: list[dict[str, Any]],
    capability_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    recommendations = []
    seen: set[tuple[str, str]] = set()
    for stage in stage_reports:
        for capability in stage["blocked_capabilities"]:
            report = capability_reports[capability]
            key = ("blocked", capability)
            if key in seen:
                continue
            seen.add(key)
            recommendations.append(
                {
                    "priority": "high",
                    "capability": capability,
                    "action": "configure_or_replace_provider",
                    "missing_dependencies": report["missing_dependencies"],
                    "fallback_tool_keys": report["fallback_tool_keys"],
                }
            )
        for capability in stage["degraded_capabilities"]:
            report = capability_reports[capability]
            key = ("degraded", capability)
            if key in seen:
                continue
            seen.add(key)
            recommendations.append(
                {
                    "priority": "medium",
                    "capability": capability,
                    "action": "improve_optional_capability",
                    "missing_dependencies": report["missing_dependencies"],
                    "fallback_tool_keys": report["fallback_tool_keys"],
                }
            )
        for capability in stage["intent_mismatch_capabilities"]:
            report = capability_reports[capability]
            key = ("intent", capability)
            if key in seen:
                continue
            seen.add(key)
            recommendations.append(
                {
                    "priority": "high",
                    "capability": capability,
                    "action": "restore_declared_quality",
                    "quality_mode": (report["intent"] or {}).get("quality_mode"),
                    "expected_runtime": (report["intent"] or {}).get("expected_runtime"),
                    "missing_dependencies": report["missing_dependencies"],
                    "fallback_tool_keys": report["fallback_tool_keys"],
                }
            )
    priority_order = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        recommendations,
        key=lambda item: (priority_order.get(item["priority"], 99), item["capability"]),
    )


def _summary(overall_status: str, stage_reports: list[dict[str, Any]]) -> str:
    counts = {
        "ready": sum(1 for stage in stage_reports if stage["status"] == "ready"),
        "degraded": sum(1 for stage in stage_reports if stage["status"] == "degraded"),
        "blocked": sum(1 for stage in stage_reports if stage["status"] == "blocked"),
    }
    return (
        f"Preflight {overall_status}: {counts['ready']} ready, "
        f"{counts['degraded']} degraded, {counts['blocked']} blocked stage(s)."
    )


def _unique(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})
