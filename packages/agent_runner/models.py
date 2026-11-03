from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentInput(BaseModel):
    run_id: str
    pipeline_key: str
    stage_key: str
    stage_definition: dict[str, Any]
    current_artifacts: dict[str, Any] = Field(default_factory=dict)
    available_tools: list[dict[str, Any]] = Field(default_factory=list)
    user_preferences: dict[str, Any] = Field(default_factory=dict)
    cost_policy: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    next_action: Literal["write_artifact", "call_tools", "request_review", "fail"]
    artifact_type: str | None = None
    artifact_payload: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    review_required: bool = False
    summary: str
    risk_flags: list[str] = Field(default_factory=list)

