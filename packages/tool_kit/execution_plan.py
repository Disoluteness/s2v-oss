from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def build_execution_plan(*, preflight_report: dict[str, Any]) -> dict[str, Any]:
    """Convert a preflight report into an auditable execution plan."""

    stage_plans: list[dict[str, Any]] = []
    decision_log: list[dict[str, Any]] = []

    for stage in preflight_report.get("stages", []):
        stage_decisions = _stage_decisions(stage)
        stage_decisions = _number_decisions(stage_decisions, offset=len(decision_log))
        decision_log.extend(stage_decisions)
        stage_plans.append(
            {
                "stage_key": stage["stage_key"],
                "display_name": stage["display_name"],
                "order": stage["order"],
                "preflight_status": stage["status"],
                "execution_mode": _execution_mode(stage),
                "requires_human_review": stage["requires_human_review"],
                "selected_tools": stage["selected_tools"],
                "blocking_capabilities": stage["blocked_capabilities"],
                "degraded_capabilities": stage["degraded_capabilities"],
                "intent_mismatch_capabilities": stage.get("intent_mismatch_capabilities", []),
                "missing_dependencies": stage["missing_dependencies"],
                "decisions": [decision["decision_id"] for decision in stage_decisions],
            }
        )

    execution_policy = _execution_policy(stage_plans, decision_log)
    intent_downgrades = [
        {
            "stage_key": decision["stage_key"],
            "capability": decision["capability"],
            "required": decision["required"],
            "kind": decision.get("intent_mismatch_kind"),
            "quality_mode": (decision.get("intent") or {}).get("quality_mode"),
            "expected_runtime": (decision.get("intent") or {}).get("expected_runtime"),
            "selected_tool_key": decision["selected_tool_key"],
            "selected_provider": decision["selected_provider"],
            "selected_runtime": (decision.get("selected_tool") or {}).get("runtime"),
            "reason": decision["intent_mismatch_reason"],
        }
        for decision in decision_log
        if not decision["intent_satisfied"]
    ]
    plan = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "artifact_type": "preflight_report",
            "overall_status": preflight_report["overall_status"],
            "generated_at": preflight_report.get("generated_at"),
        },
        "pipeline": preflight_report["pipeline"],
        "run_input": preflight_report.get("run_input", {}),
        "execution_policy": execution_policy,
        "can_execute": execution_policy != "blocked",
        "summary": _summary(execution_policy, stage_plans),
        "stage_plans": stage_plans,
        "decision_log": decision_log,
        "intent_downgrades": intent_downgrades,
        "intent_downgrade_capabilities": sorted(
            {item["capability"] for item in intent_downgrades}
        ),
        "requires_intent_acknowledgement": bool(intent_downgrades),
        "blocking_issues": preflight_report.get("blocking_issues", []),
        "recommendations": preflight_report.get("recommendations", []),
        "human_checkpoints": [
            {
                "stage_key": stage["stage_key"],
                "display_name": stage["display_name"],
                "reason": "pipeline_stage_requires_review",
            }
            for stage in stage_plans
            if stage["requires_human_review"]
        ],
    }
    # The reservation is derived from the plan itself so a re-planned run always
    # carries a figure that matches the tools it is actually about to call.
    plan["cost_reservation"] = build_cost_reservation(plan)
    return plan


def declared_budget_limit(input_payload: dict[str, Any] | None) -> float | None:
    """Read the run's declared cost budget, if any.

    Single source of truth for the ``cost_budget`` / ``cost_budget_usd`` aliases;
    the run start gate and the plan's reservation both go through it.
    """

    payload = input_payload or {}
    raw_value = payload.get("cost_budget")
    if raw_value is None:
        raw_value = payload.get("cost_budget_usd")
    if raw_value is None:
        return None
    return max(0.0, float(raw_value))


def build_cost_reservation(plan: dict[str, Any]) -> dict[str, Any]:
    """Reserve budget up front by summing every selected tool's cost estimate (T76).

    Budget checks used to happen one dispatch at a time, which means the first
    few paid calls were already spent before the run learned it could not afford
    the rest. Summing the plan's selections lets the run be stopped *before* the
    first real charge, and makes the total visible on the plan itself.
    """

    line_items: list[dict[str, Any]] = []
    paid_tools: list[dict[str, Any]] = []
    total = 0.0
    currency: str | None = None

    for decision in plan.get("decision_log", []):
        if decision.get("decision") in {"blocked", "skip_optional"}:
            continue
        tool_key = decision.get("selected_tool_key")
        if not tool_key:
            continue
        selected = decision.get("selected_tool") or {}
        cost_model = selected.get("cost_model") or {}
        amount = float(cost_model.get("estimated_cost") or 0.0)
        item_currency = cost_model.get("currency") or "USD"
        runtime = selected.get("runtime") or selected.get("runtime_type")
        currency = currency or item_currency
        item = {
            "stage_key": decision.get("stage_key"),
            "capability": decision.get("capability"),
            "tool_key": tool_key,
            "provider": decision.get("selected_provider"),
            "runtime": runtime,
            "estimated_cost": round(amount, 6),
            "currency": item_currency,
        }
        line_items.append(item)
        total += amount
        # A paid provider is an API-runtime tool that charges per operation. Local
        # tools are free, so they never need a first-use confirmation.
        if amount > 0.0 and runtime == "api":
            paid_tools.append(item)

    budget_limit = declared_budget_limit(plan.get("run_input"))
    reserved_cost = round(total, 6)
    if budget_limit is None:
        budget_status = "not_configured"
    elif reserved_cost > budget_limit:
        budget_status = "exceeds_budget"
    else:
        budget_status = "within_budget"

    return {
        "schema_version": "1.0",
        "reserved_cost": reserved_cost,
        "currency": currency or "USD",
        "budget_limit": budget_limit,
        "budget_status": budget_status,
        "line_items": line_items,
        "paid_tools": paid_tools,
        "paid_providers": sorted(
            {item["provider"] for item in paid_tools if item.get("provider")}
        ),
    }


def _stage_decisions(stage: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = []
    for capability in stage["required_capabilities"]:
        decisions.append(
            _capability_decision(
                stage=stage,
                capability=capability,
                required=True,
            )
        )
    for capability in stage["optional_capabilities"]:
        decisions.append(
            _capability_decision(
                stage=stage,
                capability=capability,
                required=False,
            )
        )
    return decisions


def _capability_decision(
    *,
    stage: dict[str, Any],
    capability: dict[str, Any],
    required: bool,
) -> dict[str, Any]:
    capability_key = capability["capability"]
    selected_tool_key = capability.get("recommended_tool_key")
    selected_provider = capability.get("recommended_provider")
    status = capability["status"]
    intent_satisfied = bool(capability.get("intent_satisfied", True))

    if status in {"available", "degraded"} and selected_tool_key:
        if status == "degraded":
            decision = "use_degraded_tool"
        elif intent_satisfied:
            decision = "use_tool"
        else:
            # Available, but only below what the run declared it wanted. Not a
            # hard stop — it is a question the operator has to answer.
            decision = "use_tool_below_intent"
        if not intent_satisfied:
            rationale = (
                "Capability is available, but only below the quality this run declared."
            )
        elif required:
            rationale = (
                "Required capability has an available tool."
                if status == "available"
                else "Required capability has only a degraded tool."
            )
        else:
            rationale = (
                "Optional capability has an available tool."
                if status == "available"
                else "Optional capability has only a degraded tool."
            )
    elif required:
        decision = "blocked"
        rationale = "Required capability has no usable tool."
    else:
        decision = "skip_optional"
        rationale = "Optional capability has no usable tool; continue without it."

    options_considered = [
        _tool_option(tool)
        for tool in capability.get("tools", [])
    ]
    selected_tool = next(
        (
            option
            for option in options_considered
            if option["tool_key"] == selected_tool_key
        ),
        None,
    )
    fallback_plan = [
        option
        for option in options_considered
        if option.get("selection_role") == "fallback"
    ]

    return {
        "decision_id": "",
        "category": "tool_selection",
        "subject": f"{stage['stage_key']}:{capability_key}",
        "stage_key": stage["stage_key"],
        "capability": capability_key,
        "required": required,
        "decision": decision,
        "selected_tool_key": selected_tool_key,
        "selected_provider": selected_provider,
        "selected_tool": selected_tool,
        "preflight_status": status,
        "rationale": rationale,
        "intent": capability.get("intent"),
        "intent_satisfied": intent_satisfied,
        "intent_mismatch_kind": capability.get("intent_mismatch_kind"),
        "intent_mismatch_reason": capability.get("intent_mismatch_reason"),
        "selector": capability.get("selector", {}),
        "selection_reason": capability.get("selection_reason"),
        "missing_dependencies": capability.get("missing_dependencies", []),
        "fallback_tool_keys": capability.get("fallback_tool_keys", []),
        "fallback_plan": fallback_plan,
        "options_considered": options_considered,
    }


def _tool_option(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": tool.get("contract_version", "1.0"),
        "tool_key": tool["tool_key"],
        "provider": tool["provider"],
        "status": tool["status"],
        "runtime": tool.get("runtime", tool.get("runtime_type")),
        "runtime_type": tool.get("runtime_type"),
        "dependencies": tool.get("dependencies", []),
        "missing_dependencies": tool["missing_dependencies"],
        "fallback_tool_keys": tool.get("fallback_tool_keys", []),
        "operations": tool.get("operations", {}),
        "resource_profile": tool.get("resource_profile", {}),
        "retry_policy": tool.get("retry_policy", {}),
        "cost_model": tool.get("cost_model", {}),
        "quality_profile": tool.get("quality_profile", {}),
        "selector_score": (tool.get("selection") or {}).get("score"),
        "selector_rank": (tool.get("selection") or {}).get("rank"),
        "selection_role": (tool.get("selection") or {}).get("selection_role"),
        "selection_reason": (tool.get("selection") or {}).get("selection_reason"),
        "fallback_reason": (tool.get("selection") or {}).get("fallback_reason"),
        "score_breakdown": (tool.get("selection") or {}).get("score_breakdown", {}),
    }


def _execution_mode(stage: dict[str, Any]) -> str:
    if stage["blocked_capabilities"]:
        return "blocked"
    if stage["requires_human_review"]:
        return "human_review"
    if stage["status"] == "degraded":
        return "automatic_with_degradation"
    return "automatic"


def _execution_policy(
    stage_plans: list[dict[str, Any]],
    decision_log: list[dict[str, Any]],
) -> str:
    """Rank the plan verdicts from hardest to softest.

    An intent downgrade outranks an ordinary degradation: the capability is
    available, but it is not the one the run asked for, so it needs a decision
    rather than a footnote.
    """
    if any(stage["execution_mode"] == "blocked" for stage in stage_plans):
        return "blocked"
    if any(not decision["intent_satisfied"] for decision in decision_log):
        return "proceed_with_intent_downgrades"
    if any(
        decision["decision"] in {"use_degraded_tool", "skip_optional"}
        for decision in decision_log
    ):
        return "proceed_with_degradations"
    return "proceed"


def _number_decisions(
    decision_log: list[dict[str, Any]],
    *,
    offset: int = 0,
) -> list[dict[str, Any]]:
    numbered = []
    for index, decision in enumerate(decision_log, start=1):
        numbered.append({**decision, "decision_id": f"decision_{offset + index:03d}"})
    return numbered


def _summary(execution_policy: str, stage_plans: list[dict[str, Any]]) -> str:
    blocked = sum(1 for stage in stage_plans if stage["execution_mode"] == "blocked")
    review = sum(1 for stage in stage_plans if stage["execution_mode"] == "human_review")
    degraded = sum(
        1 for stage in stage_plans if stage["execution_mode"] == "automatic_with_degradation"
    )
    below_intent = sum(
        1 for stage in stage_plans if stage.get("intent_mismatch_capabilities")
    )
    return (
        f"Execution plan {execution_policy}: {blocked} blocked, "
        f"{degraded} degraded, {below_intent} below-intent, "
        f"{review} human-review stage(s)."
    )
