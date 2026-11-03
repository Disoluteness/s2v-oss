from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def build_proposal_packet(
    *,
    preflight_report: dict[str, Any],
    execution_plan: dict[str, Any],
    current_artifacts: dict[str, Any] | None = None,
    source_artifacts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build a user-approvable production proposal before paid generation starts."""

    artifacts = current_artifacts or {}
    source_ids = source_artifacts or {}
    run_input = execution_plan.get("run_input") or preflight_report.get("run_input") or {}
    selected_tools = _selected_tool_options(execution_plan)
    style_preview = _style_preview(run_input=run_input, current_artifacts=artifacts)
    risks = _risk_summary(
        preflight_report=preflight_report,
        execution_plan=execution_plan,
        selected_tools=selected_tools,
    )
    options = _proposal_options(
        execution_plan=execution_plan,
        selected_tools=selected_tools,
        risks=risks,
        style_preview=style_preview,
    )
    recommended_option_id = _recommended_option_id(
        options=options,
        execution_policy=execution_plan.get("execution_policy", "blocked"),
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "preflight_artifact_id": source_ids.get("preflight_report"),
            "execution_plan_artifact_id": source_ids.get("execution_plan"),
            "source_artifact_types": sorted(artifacts),
        },
        "pipeline": execution_plan.get("pipeline") or preflight_report.get("pipeline") or {},
        "run_input": run_input,
        "summary": _summary(
            execution_policy=execution_plan.get("execution_policy", "blocked"),
            options=options,
            risks=risks,
        ),
        "recommended_option_id": recommended_option_id,
        "options": options,
        "budget_summary": _budget_summary(options),
        "timeline_summary": _timeline_summary(options),
        "risk_summary": risks,
        "style_preview": style_preview,
        "approval_gate": {
            "required_before": "asset_generation",
            "reason": "Confirm production approach before paid or long-running generation.",
            "recommended_action": (
                "Resolve blocking issues before starting the run."
                if execution_plan.get("execution_policy") == "blocked"
                else "Approve one proposal option before dispatching provider tasks."
            ),
        },
        "next_actions": _next_actions(
            execution_policy=execution_plan.get("execution_policy", "blocked"),
            risks=risks,
            recommended_option_id=recommended_option_id,
        ),
    }


def _proposal_options(
    *,
    execution_plan: dict[str, Any],
    selected_tools: list[dict[str, Any]],
    risks: dict[str, Any],
    style_preview: dict[str, Any],
) -> list[dict[str, Any]]:
    execution_policy = execution_plan.get("execution_policy", "blocked")
    base_stage_plans = execution_plan.get("stage_plans", [])
    options = [
        _make_option(
            option_id="option_fast_draft",
            label="快速草案",
            positioning="优先快速看到可审核视频，尽量使用本地或低延迟工具。",
            multiplier=0.65,
            stage_plans=base_stage_plans,
            selected_tools=selected_tools,
            risks=risks,
            style_preview=style_preview,
            tradeoffs=[
                "会优先保留核心叙事与成片结构。",
                "可跳过非必需增强项，视觉细节和音乐完整度可能较弱。",
            ],
            recommended_for=["内部快速预览", "确认脚本节奏", "低成本试跑"],
            execution_policy=execution_policy,
        ),
        _make_option(
            option_id="option_balanced",
            label="标准制作",
            positioning="按当前执行计划选择工具，平衡质量、成本和稳定性。",
            multiplier=1.0,
            stage_plans=base_stage_plans,
            selected_tools=selected_tools,
            risks=risks,
            style_preview=style_preview,
            tradeoffs=[
                "按已选 provider 和本地工具执行，质量和可控性最均衡。",
                "遇到 provider 排队或回调延迟时，总耗时会随外部服务波动。",
            ],
            recommended_for=["常规广告样片", "脚本放映室验证", "团队审核交付"],
            execution_policy=execution_policy,
        ),
        _make_option(
            option_id="option_quality_provider",
            label="质量优先",
            positioning="保留 provider 能力和人工审核空间，优先提高人物、字幕、音乐与合成完成度。",
            multiplier=1.45,
            stage_plans=base_stage_plans,
            selected_tools=selected_tools,
            risks=risks,
            style_preview=style_preview,
            tradeoffs=[
                "更适合对外展示或客户确认版本。",
                "预算和等待时间更高；如 provider 额度不足，需要先补齐配置。",
            ],
            recommended_for=["客户提案", "品牌素材验证", "高质量口播成片"],
            execution_policy=execution_policy,
        ),
    ]
    if execution_policy == "blocked":
        options[0] = {
            **options[0],
            "option_id": "option_resolve_blockers",
            "label": "先解除阻塞",
            "positioning": "先补齐必需能力，再进入任何付费或长耗时生成。",
            "status": "blocked",
            "quality_tradeoffs": [
                "当前必需能力不可用，直接生成会失败。",
                "解除阻塞后可重新生成提案包并选择正式制作方案。",
            ],
            "recommended_for": ["依赖缺失修复", "部署配置核查"],
        }
    return options


def _make_option(
    *,
    option_id: str,
    label: str,
    positioning: str,
    multiplier: float,
    stage_plans: list[dict[str, Any]],
    selected_tools: list[dict[str, Any]],
    risks: dict[str, Any],
    style_preview: dict[str, Any],
    tradeoffs: list[str],
    recommended_for: list[str],
    execution_policy: str,
) -> dict[str, Any]:
    budget = _estimate_budget(selected_tools=selected_tools, multiplier=multiplier)
    timeline = _estimate_timeline(
        stage_plans=stage_plans,
        selected_tools=selected_tools,
        multiplier=multiplier,
    )
    return {
        "option_id": option_id,
        "label": label,
        "status": "blocked" if execution_policy == "blocked" else "available",
        "positioning": positioning,
        "tool_path": _tool_path(selected_tools),
        "stage_plan": _compact_stage_plan(stage_plans),
        "budget": budget,
        "estimated_timeline": timeline,
        "quality_tradeoffs": tradeoffs,
        "risks": _option_risks(risks),
        "style_preview": style_preview,
        "recommended_for": recommended_for,
    }


def _selected_tool_options(execution_plan: dict[str, Any]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in execution_plan.get("decision_log", []):
        tool = decision.get("selected_tool")
        if not isinstance(tool, dict):
            continue
        key = (decision.get("stage_key") or "", tool.get("tool_key") or "")
        selected[key] = {
            "stage_key": decision.get("stage_key"),
            "capability": decision.get("capability"),
            "required": decision.get("required", False),
            "decision": decision.get("decision"),
            "selection_reason": decision.get("selection_reason"),
            **tool,
        }
    return list(selected.values())


def _tool_path(selected_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "stage_key": tool.get("stage_key"),
            "capability": tool.get("capability"),
            "tool_key": tool.get("tool_key"),
            "provider": tool.get("provider"),
            "runtime": tool.get("runtime") or tool.get("runtime_type"),
            "quality_tier": (tool.get("quality_profile") or {}).get("tier", "draft"),
            "cost_unit": (tool.get("cost_model") or {}).get("unit", "operation"),
            "estimated_cost": _round_money(_base_tool_cost(tool)),
            "latency_seconds": (tool.get("resource_profile") or {}).get(
                "expected_latency_seconds"
            ),
            "selection_reason": tool.get("selection_reason"),
        }
        for tool in selected_tools
    ]


def _compact_stage_plan(stage_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "stage_key": stage.get("stage_key"),
            "display_name": stage.get("display_name"),
            "execution_mode": stage.get("execution_mode"),
            "requires_human_review": stage.get("requires_human_review", False),
            "selected_tools": stage.get("selected_tools", {}),
        }
        for stage in stage_plans
    ]


def _estimate_budget(
    *,
    selected_tools: list[dict[str, Any]],
    multiplier: float,
) -> dict[str, Any]:
    costs = [_base_tool_cost(tool) for tool in selected_tools]
    estimated_cost = sum(costs) * multiplier
    confidence = "medium" if costs and any(cost > 0 for cost in costs) else "low"
    unknown_api_tools = [
        tool.get("tool_key")
        for tool in selected_tools
        if _runtime(tool) in {"api", "hybrid"} and _base_tool_cost(tool) == 0
    ]
    return {
        "estimated_cost": _round_money(estimated_cost),
        "currency": _currency(selected_tools),
        "confidence": "low" if unknown_api_tools else confidence,
        "cost_drivers": _cost_drivers(selected_tools),
        "unknown_price_tools": unknown_api_tools,
    }


def _estimate_timeline(
    *,
    stage_plans: list[dict[str, Any]],
    selected_tools: list[dict[str, Any]],
    multiplier: float,
) -> dict[str, Any]:
    tool_seconds = sum(_tool_latency_seconds(tool) for tool in selected_tools)
    review_seconds = 300 * sum(
        1 for stage in stage_plans if stage.get("requires_human_review")
    )
    orchestration_seconds = max(120, 45 * max(1, len(stage_plans)))
    total_seconds = (tool_seconds + review_seconds + orchestration_seconds) * multiplier
    minimum = max(1, round(total_seconds * 0.75 / 60))
    maximum = max(minimum + 1, round(total_seconds * 1.35 / 60))
    return {
        "total_minutes": max(1, round(total_seconds / 60)),
        "range_minutes": [minimum, maximum],
        "critical_path": _critical_path(stage_plans, selected_tools),
        "confidence": "medium" if selected_tools else "low",
    }


def _base_tool_cost(tool: dict[str, Any]) -> float:
    cost_model = tool.get("cost_model") or {}
    value = _float_or_none(cost_model.get("estimated_cost"))
    if value is not None:
        return value
    pricing = cost_model.get("pricing") or tool.get("pricing") or {}
    numeric_values = [
        candidate
        for key, raw_value in pricing.items()
        if any(token in str(key).lower() for token in ("cost", "price", "usd"))
        for candidate in [_float_or_none(raw_value)]
        if candidate is not None
    ]
    if numeric_values:
        return max(numeric_values)
    return 0.0


def _tool_latency_seconds(tool: dict[str, Any]) -> int:
    resource = tool.get("resource_profile") or {}
    latency = _int_or_none(resource.get("expected_latency_seconds"))
    if latency is not None:
        return latency
    if _runtime(tool) == "local":
        return 60
    if _runtime(tool) == "hybrid":
        return 360
    return 480


def _currency(selected_tools: list[dict[str, Any]]) -> str:
    for tool in selected_tools:
        currency = (tool.get("cost_model") or {}).get("currency")
        if currency:
            return currency
    return "USD"


def _cost_drivers(selected_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    drivers = []
    for tool in selected_tools:
        cost = _base_tool_cost(tool)
        if cost <= 0 and _runtime(tool) == "local":
            continue
        drivers.append(
            {
                "tool_key": tool.get("tool_key"),
                "provider": tool.get("provider"),
                "capability": tool.get("capability"),
                "runtime": _runtime(tool),
                "estimated_cost": _round_money(cost),
                "unit": (tool.get("cost_model") or {}).get("unit", "operation"),
            }
        )
    return drivers


def _critical_path(
    stage_plans: list[dict[str, Any]],
    selected_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latency_by_stage: dict[str, int] = {}
    for tool in selected_tools:
        stage_key = tool.get("stage_key") or "unknown"
        latency_by_stage[stage_key] = latency_by_stage.get(stage_key, 0) + _tool_latency_seconds(tool)
    return [
        {
            "stage_key": stage.get("stage_key"),
            "display_name": stage.get("display_name"),
            "estimated_minutes": max(1, round(latency_by_stage.get(stage.get("stage_key"), 45) / 60)),
        }
        for stage in stage_plans
    ]


def _risk_summary(
    *,
    preflight_report: dict[str, Any],
    execution_plan: dict[str, Any],
    selected_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    degraded = []
    for stage in execution_plan.get("stage_plans", []):
        for capability in stage.get("degraded_capabilities", []):
            degraded.append(
                {
                    "stage_key": stage.get("stage_key"),
                    "capability": capability,
                    "severity": "warning",
                    "message": "Capability is degraded or optional capability will be skipped.",
                }
            )
    provider_wait = [
        {
            "tool_key": tool.get("tool_key"),
            "provider": tool.get("provider"),
            "severity": "info",
            "message": "Provider-backed task may require polling, callback, quota, or manual retry.",
        }
        for tool in selected_tools
        if _runtime(tool) in {"api", "hybrid"}
    ]
    return {
        "blocking": preflight_report.get("blocking_issues", []),
        "degraded": degraded,
        "provider_wait": provider_wait,
        "recommendations": execution_plan.get("recommendations", []),
    }


def _option_risks(risks: dict[str, Any]) -> list[dict[str, Any]]:
    combined = []
    combined.extend(risks.get("blocking", []))
    combined.extend(risks.get("degraded", [])[:4])
    combined.extend(risks.get("provider_wait", [])[:4])
    return combined


def _style_preview(
    *,
    run_input: dict[str, Any],
    current_artifacts: dict[str, Any],
) -> dict[str, Any]:
    snapshot = run_input.get("style_preset_snapshot") or {}
    style_preset = snapshot.get("style_preset") or {}
    brand_kit = snapshot.get("brand_kit") or {}
    scene_plan = current_artifacts.get("screening_room_scene_plan") or {}
    production_plan = current_artifacts.get("production_plan") or {}
    creative_strategy = production_plan.get("creative_strategy") or {}
    caption_style = _first_dict(
        style_preset.get("subtitle_style"),
        (current_artifacts.get("spoken_script") or {}).get("caption_style"),
        scene_plan.get("caption_style"),
    )
    visual_style = _first_dict(
        style_preset.get("visual_style"),
        creative_strategy.get("style_preset"),
        {"style_key": run_input.get("style_key")},
    )
    return {
        "title": scene_plan.get("title") or production_plan.get("title"),
        "brand_name": run_input.get("brand_name") or brand_kit.get("name"),
        "product_name": run_input.get("product_name"),
        "style_key": run_input.get("style_key") or style_preset.get("style_key"),
        "aspect_ratio": run_input.get("aspect_ratio") or style_preset.get("aspect_ratio"),
        "target_platform": run_input.get("target_platform"),
        "target_duration_seconds": run_input.get("target_duration_seconds"),
        "colors": brand_kit.get("color_palette") or {},
        "typography": brand_kit.get("typography") or {},
        "subtitle_style": caption_style,
        "visual_style": visual_style,
        "tone": style_preset.get("tone") or brand_kit.get("brand_voice") or {},
        "guardrails": _merged_guardrails(style_preset=style_preset, brand_kit=brand_kit),
        "sample_caption": _sample_caption(current_artifacts=current_artifacts),
    }


def _merged_guardrails(
    *,
    style_preset: dict[str, Any],
    brand_kit: dict[str, Any],
) -> dict[str, Any]:
    guardrails = dict(style_preset.get("guardrails") or {})
    banned_terms = sorted(
        set(guardrails.get("banned_terms") or []) | set(brand_kit.get("banned_terms") or [])
    )
    if banned_terms:
        guardrails["banned_terms"] = banned_terms
    return guardrails


def _sample_caption(*, current_artifacts: dict[str, Any]) -> str | None:
    scene_plan = current_artifacts.get("screening_room_scene_plan") or {}
    if scene_plan.get("caption_text"):
        return scene_plan["caption_text"]
    spoken_script = current_artifacts.get("spoken_script") or {}
    segments = spoken_script.get("segments") or []
    if segments:
        first = segments[0]
        return first.get("caption") or first.get("text")
    return None


def _budget_summary(options: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [option["budget"]["estimated_cost"] for option in options]
    return {
        "currency": options[0]["budget"]["currency"] if options else "USD",
        "min_estimated_cost": min(costs) if costs else 0.0,
        "max_estimated_cost": max(costs) if costs else 0.0,
        "confidence": _combined_confidence(
            [option["budget"].get("confidence", "low") for option in options]
        ),
    }


def _timeline_summary(options: list[dict[str, Any]]) -> dict[str, Any]:
    minutes = [option["estimated_timeline"]["total_minutes"] for option in options]
    return {
        "min_minutes": min(minutes) if minutes else 0,
        "max_minutes": max(minutes) if minutes else 0,
        "confidence": _combined_confidence(
            [option["estimated_timeline"].get("confidence", "low") for option in options]
        ),
    }


def _summary(
    *,
    execution_policy: str,
    options: list[dict[str, Any]],
    risks: dict[str, Any],
) -> str:
    blocked = len(risks.get("blocking", []))
    degraded = len(risks.get("degraded", []))
    return (
        f"Proposal packet {execution_policy}: {len(options)} option(s), "
        f"{blocked} blocking issue(s), {degraded} degradation warning(s)."
    )


def _recommended_option_id(
    *,
    options: list[dict[str, Any]],
    execution_policy: str,
) -> str:
    if execution_policy == "blocked":
        return options[0]["option_id"]
    return "option_balanced"


def _next_actions(
    *,
    execution_policy: str,
    risks: dict[str, Any],
    recommended_option_id: str,
) -> list[dict[str, Any]]:
    if execution_policy == "blocked":
        return [
            {
                "action": "resolve_blocking_issues",
                "label": "补齐阻塞依赖",
                "details": risks.get("blocking", []),
            },
            {
                "action": "rebuild_proposal_packet",
                "label": "重新生成制作提案包",
                "details": {"after": "blocking issues are resolved"},
            },
        ]
    return [
        {
            "action": "approve_option",
            "label": "确认制作方案",
            "details": {"recommended_option_id": recommended_option_id},
        },
        {
            "action": "start_or_dispatch_run",
            "label": "启动任务或派发执行清单",
            "details": {"requires_review": "before provider generation"},
        },
    ]


def _combined_confidence(values: list[str]) -> str:
    if "low" in values:
        return "low"
    if "medium" in values:
        return "medium"
    return "high"


def _runtime(tool: dict[str, Any]) -> str | None:
    runtime = tool.get("runtime") or tool.get("runtime_type")
    return str(runtime).lower() if runtime else None


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict) and value:
            return value
    return {}


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else None


def _round_money(value: float) -> float:
    return round(value, 4)
