from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from packages.tool_kit.contracts import RuntimeType, ToolDefinition, ToolStatus

SELECTOR_VERSION = "1.0"

SELECTOR_KEYS = {
    "tts": "tts_selector",
    "image_generation": "image_selector",
    "avatar_video": "avatar_selector",
    "browser_recording": "browser_recording_selector",
    "interaction_highlight": "interaction_highlight_selector",
    "screen_capture": "screen_capture_selector",
    "video_generation": "video_selector",
    "music_generation": "music_selector",
    "subtitle_generation": "subtitle_selector",
    "video_composition": "compose_selector",
}

QUALITY_SCORE = {
    "production": 140.0,
    "premium": 140.0,
    "standard": 110.0,
    "draft": 70.0,
    "preview": 35.0,
}

# What each declared quality mode asks a capability to run on. A run that asks
# for provider audio and silently receives a local synth has been downgraded,
# even though the capability itself is "available" — that is the case the
# intent check exists to surface.
QUALITY_MODE_RUNTIME = {
    "local_preview": RuntimeType.LOCAL,
    "provider_audio": RuntimeType.API,
    "provider_avatar": RuntimeType.API,
}

# Capabilities a quality mode speaks about. A mode is only an assertion about
# the capabilities it actually names; ``local_preview`` covers every capability
# because it is the explicit "I want the local path" choice.
QUALITY_MODE_CAPABILITIES: dict[str, set[str] | None] = {
    "local_preview": None,
    "provider_audio": {"tts", "music_generation"},
    "provider_avatar": {"avatar_video"},
}


class ToolSelectionCandidate(BaseModel):
    tool_key: str
    provider: str
    status: str
    runtime: str
    score: float
    rank: int
    eligible: bool
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    selection_role: str = "rejected"
    selection_reason: str | None = None
    fallback_reason: str | None = None


class ToolSelectionResult(BaseModel):
    selector_version: str = SELECTOR_VERSION
    selector_key: str
    capability: str
    policy: str = "best_available"
    selected_tool_key: str | None = None
    selected_provider: str | None = None
    selection_reason: str | None = None
    fallback_tool_keys: list[str] = Field(default_factory=list)
    candidates: list[ToolSelectionCandidate] = Field(default_factory=list)
    intent: dict[str, Any] | None = None
    intent_satisfied: bool = True
    intent_mismatch_kind: str | None = None
    intent_mismatch_reason: str | None = None


def capability_intent(
    *,
    capability: str,
    run_input: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Describe the quality the run input asked for on this capability.

    Returns ``None`` when the run declared nothing, or declared something that
    does not speak about this capability. ``None`` means "no assertion", never
    "assertion satisfied".
    """
    quality_mode = (run_input or {}).get("quality_mode")
    if not isinstance(quality_mode, str):
        return None
    normalized = quality_mode.strip().lower()
    if normalized not in QUALITY_MODE_RUNTIME:
        return None
    covered = QUALITY_MODE_CAPABILITIES[normalized]
    if covered is not None and capability not in covered:
        return None
    return {
        "source": "quality_mode",
        "quality_mode": normalized,
        "expected_runtime": QUALITY_MODE_RUNTIME[normalized].value,
    }


def select_tool_for_capability(
    *,
    capability: str,
    tools: list[ToolDefinition],
    run_input: dict[str, Any] | None = None,
    policy: str = "best_available",
) -> ToolSelectionResult:
    """Score capability providers and return the best usable candidate."""

    run_input = run_input or {}
    intent = capability_intent(capability=capability, run_input=run_input)
    preferences = _selection_preferences(capability=capability, run_input=run_input)
    scored = [
        _score_candidate(tool=tool, capability=capability, preferences=preferences)
        for tool in tools
    ]
    scored.sort(key=lambda candidate: (-candidate.score, candidate.provider, candidate.tool_key))
    ranked = [
        candidate.model_copy(update={"rank": index})
        for index, candidate in enumerate(scored, start=1)
    ]
    selected = next((candidate for candidate in ranked if candidate.eligible), None)
    selected_tool_key = selected.tool_key if selected else None
    selected_provider = selected.provider if selected else None
    fallback_tool_keys = [
        candidate.tool_key
        for candidate in ranked
        if candidate.eligible and candidate.tool_key != selected_tool_key
    ]
    candidates = [
        _candidate_with_role(candidate, selected_tool_key=selected_tool_key)
        for candidate in ranked
    ]
    intent_satisfied, intent_mismatch_kind, intent_mismatch_reason = _intent_outcome(
        intent=intent,
        selected=selected,
        ranked=ranked,
    )
    return ToolSelectionResult(
        selector_key=SELECTOR_KEYS.get(capability, "generic_selector"),
        capability=capability,
        policy=policy,
        selected_tool_key=selected_tool_key,
        selected_provider=selected_provider,
        selection_reason=_selection_reason(selected) if selected else None,
        fallback_tool_keys=fallback_tool_keys,
        candidates=candidates,
        intent=intent,
        intent_satisfied=intent_satisfied,
        intent_mismatch_kind=intent_mismatch_kind,
        intent_mismatch_reason=intent_mismatch_reason,
    )


def _intent_outcome(
    *,
    intent: dict[str, Any] | None,
    selected: ToolSelectionCandidate | None,
    ranked: list[ToolSelectionCandidate],
) -> tuple[bool, str | None, str | None]:
    """Compare the outcome against the declared intent.

    Two ways a declared intent goes unmet:

    * ``capability_missing`` — nothing usable is left, so the capability is
      dropped or blocked outright. An optional capability would otherwise be
      skipped without ever mentioning that the run asked for it.
    * ``runtime_downgrade`` — a tool was selected, but on the other runtime
      tier (typically the local path standing in for a paid provider).

    Only a cross-runtime move counts as a downgrade. Swapping one API provider
    for another is normal fallback behaviour and stays reported through
    ``fallback_reason`` instead of blocking anything.
    """
    if intent is None:
        return True, None, None
    expected_runtime = intent["expected_runtime"]
    if selected is None:
        return False, "capability_missing", _capability_missing_reason(intent=intent, ranked=ranked)
    if selected.runtime == expected_runtime:
        return True, None, None
    return (
        False,
        "runtime_downgrade",
        _intent_mismatch_reason(intent=intent, selected=selected, ranked=ranked),
    )


def _capability_missing_reason(
    *,
    intent: dict[str, Any],
    ranked: list[ToolSelectionCandidate],
) -> str:
    expected_runtime = intent["expected_runtime"]
    described = (
        ", ".join(
            f"{candidate.tool_key} ({candidate.provider}, {candidate.status})"
            for candidate in ranked
        )
        or "no tool is registered for this capability"
    )
    return (
        f"quality_mode={intent['quality_mode']} asked for a {expected_runtime} runtime tool, "
        f"but this capability has no usable tool. Registered: {described}."
    )


def _intent_mismatch_reason(
    *,
    intent: dict[str, Any],
    selected: ToolSelectionCandidate,
    ranked: list[ToolSelectionCandidate],
) -> str:
    expected_runtime = intent["expected_runtime"]
    alternatives = [candidate for candidate in ranked if candidate.runtime == expected_runtime]
    if alternatives:
        described = ", ".join(
            f"{candidate.tool_key} ({candidate.provider}, {candidate.status})"
            for candidate in alternatives
        )
        detail = f"Candidates on {expected_runtime} runtime: {described}."
    else:
        detail = f"No {expected_runtime} runtime tool is registered for this capability."
    return (
        f"quality_mode={intent['quality_mode']} asked for a {expected_runtime} runtime tool, "
        f"but {selected.tool_key} ({selected.provider}, {selected.runtime}) was selected. "
        f"{detail}"
    )



def _score_candidate(
    *,
    tool: ToolDefinition,
    capability: str,
    preferences: dict[str, Any],
) -> ToolSelectionCandidate:
    runtime = tool.runtime or tool.runtime_type
    breakdown = {
        "availability": _availability_score(tool.status),
        "cost": _cost_score(tool),
        "latency": _latency_score(tool),
        "quality": _quality_score(tool),
        "preference": _preference_score(tool, preferences),
        "task_fit": _task_fit_score(tool, capability, preferences),
    }
    score = sum(breakdown.values())
    eligible = tool.status in {ToolStatus.AVAILABLE, ToolStatus.DEGRADED}
    if not eligible:
        score = min(score, 0.0)
    return ToolSelectionCandidate(
        tool_key=tool.tool_key,
        provider=tool.provider,
        status=tool.status.value,
        runtime=runtime.value,
        score=round(score, 3),
        rank=0,
        eligible=eligible,
        score_breakdown={key: round(value, 3) for key, value in breakdown.items()},
    )


def _candidate_with_role(
    candidate: ToolSelectionCandidate,
    *,
    selected_tool_key: str | None,
) -> ToolSelectionCandidate:
    if candidate.tool_key == selected_tool_key:
        return candidate.model_copy(
            update={
                "selection_role": "selected",
                "selection_reason": _selection_reason(candidate),
            }
        )
    if candidate.eligible:
        return candidate.model_copy(
            update={
                "selection_role": "fallback",
                "fallback_reason": _fallback_reason(candidate),
            }
        )
    return candidate.model_copy(
        update={
            "selection_role": "blocked",
            "fallback_reason": "Tool is unavailable because required dependencies are missing.",
        }
    )


def _selection_preferences(
    *,
    capability: str,
    run_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        "preferred_tool_keys": _preferred_values(run_input, capability, "tool"),
        "preferred_providers": _preferred_values(run_input, capability, "provider"),
        "quality_mode": run_input.get("quality_mode"),
        "context": " ".join(
            str(value)
            for key, value in run_input.items()
            if key
            in {
                "raw_script",
                "brand_name",
                "product_name",
                "target_platform",
                "style_key",
                "aspect_ratio",
                "quality_mode",
            }
            and value is not None
        ).lower(),
    }


def _preferred_values(
    run_input: dict[str, Any],
    capability: str,
    preference_type: str,
) -> list[str]:
    values: list[str] = []
    singular = f"preferred_{preference_type}"
    plural = f"preferred_{preference_type}s"
    capability_key = f"{capability}_{preference_type}"
    mapping_key = f"{preference_type}_preferences"
    for key in (singular, plural, capability_key):
        values.extend(_as_list(run_input.get(key)))
    preferences = run_input.get(mapping_key)
    if isinstance(preferences, dict):
        values.extend(_as_list(preferences.get(capability)))
        values.extend(_as_list(preferences.get("default")))
    else:
        values.extend(_as_list(preferences))
    return _unique(values)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _availability_score(status: ToolStatus) -> float:
    if status == ToolStatus.AVAILABLE:
        return 600.0
    if status == ToolStatus.DEGRADED:
        return 350.0
    return -1000.0


def _cost_score(tool: ToolDefinition) -> float:
    estimated = max(tool.cost_model.estimated_cost, 0.0)
    return max(0.0, 100.0 - min(estimated * 100.0, 100.0))


def _latency_score(tool: ToolDefinition) -> float:
    latency = tool.resource_profile.expected_latency_seconds
    if latency is None:
        latency = 20 if tool.runtime_type == RuntimeType.LOCAL else 60
    return max(0.0, 100.0 - min(float(latency), 100.0))


def _quality_score(tool: ToolDefinition) -> float:
    tier = tool.quality_profile.tier.lower()
    score = QUALITY_SCORE.get(tier, 70.0)
    if tool.quality_profile.deterministic:
        score += 10.0
    return score


def _preference_score(tool: ToolDefinition, preferences: dict[str, Any]) -> float:
    score = 0.0
    if tool.tool_key in preferences["preferred_tool_keys"]:
        score += 260.0
    if tool.provider in preferences["preferred_providers"]:
        score += 180.0
    return score


def _task_fit_score(
    tool: ToolDefinition,
    capability: str,
    preferences: dict[str, Any],
) -> float:
    score = 0.0
    quality_mode = preferences.get("quality_mode")
    runtime = tool.runtime or tool.runtime_type
    if quality_mode == "provider_avatar" and capability == "avatar_video":
        score += 120.0 if runtime == RuntimeType.API else -60.0
    if quality_mode == "provider_audio" and capability in {"tts", "music_generation"}:
        score += 80.0 if runtime == RuntimeType.API else -30.0
    if quality_mode == "local_preview" and runtime == RuntimeType.LOCAL:
        score += 40.0

    context = preferences.get("context", "")
    for phrase in tool.best_for:
        normalized = phrase.lower()
        if normalized and normalized in context:
            score += 12.0
    return score


def _selection_reason(candidate: ToolSelectionCandidate | None) -> str | None:
    if candidate is None:
        return None
    strongest = max(candidate.score_breakdown.items(), key=lambda item: item[1])[0]
    return (
        f"Selected {candidate.tool_key} with score {candidate.score}; "
        f"strongest signal: {strongest}."
    )


def _fallback_reason(candidate: ToolSelectionCandidate) -> str:
    return (
        f"Kept {candidate.tool_key} as fallback with score {candidate.score}; "
        f"selected candidate ranked higher for this capability."
    )


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
