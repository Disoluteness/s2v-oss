from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeType(StrEnum):
    LOCAL = "local"
    API = "api"
    HYBRID = "hybrid"


class ToolStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


class ToolDependency(BaseModel):
    kind: str
    name: str
    required: bool = True
    description: str | None = None


class ToolOperations(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    validate_: bool = Field(default=True, alias="validate")
    estimate: bool = True
    submit: bool = True
    poll: bool = False
    archive: bool = False
    cancel: bool = False


class ToolResourceProfile(BaseModel):
    cpu: str | None = None
    memory_mb: int | None = None
    gpu_required: bool = False
    network_required: bool = False
    expected_latency_seconds: int | None = None
    max_runtime_seconds: int | None = None


class ToolRetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff_seconds: float = 5.0
    poll_interval_seconds: float | None = None
    timeout_seconds: int | None = None
    retryable_error_categories: list[str] = Field(
        default_factory=lambda: [
            "transient_provider_error",
            "rate_limited",
            "network_error",
            "timeout",
        ]
    )


class ToolCostModel(BaseModel):
    unit: str = "operation"
    estimated_cost: float = 0.0
    currency: str = "USD"
    pricing: dict[str, Any] = Field(default_factory=dict)


class ToolQualityProfile(BaseModel):
    tier: str = "draft"
    supports_preview: bool = True
    deterministic: bool = False
    notes: list[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    contract_version: str = "2.0"
    tool_key: str
    display_name: str
    capability: str
    provider: str
    runtime_type: RuntimeType
    runtime: RuntimeType | None = None
    status: ToolStatus = ToolStatus.UNAVAILABLE
    description: str = ""
    best_for: list[str] = Field(default_factory=list)
    required_env_vars: list[str] = Field(default_factory=list)
    required_binaries: list[str] = Field(default_factory=list)
    dependencies: list[ToolDependency] = Field(default_factory=list)
    install_instructions: str | None = None
    missing_dependencies: list[str] = Field(default_factory=list)
    status_reason: str | None = None
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    resource_profile: ToolResourceProfile = Field(default_factory=ToolResourceProfile)
    retry_policy: ToolRetryPolicy = Field(default_factory=ToolRetryPolicy)
    cost_model: ToolCostModel = Field(default_factory=ToolCostModel)
    quality_profile: ToolQualityProfile = Field(default_factory=ToolQualityProfile)
    operations: ToolOperations = Field(default_factory=ToolOperations)
    pricing: dict[str, Any] = Field(default_factory=dict)
    fallback_tool_keys: list[str] = Field(default_factory=list)
    timeout_seconds: int = 600

    @model_validator(mode="before")
    @classmethod
    def _copy_runtime_alias(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "runtime_type" not in normalized and "runtime" in normalized:
            normalized["runtime_type"] = normalized["runtime"]
        if "operations" not in normalized:
            normalized["operations"] = _default_operations_for_capability(
                str(normalized.get("capability", ""))
            )
        if "resource_profile" not in normalized and normalized.get("runtime_type") in {
            RuntimeType.API,
            RuntimeType.HYBRID,
            RuntimeType.API.value,
            RuntimeType.HYBRID.value,
        }:
            normalized["resource_profile"] = {"network_required": True}
        return normalized

    @model_validator(mode="after")
    def _normalize_v2_contract(self) -> "ToolDefinition":
        self.runtime = self.runtime or self.runtime_type
        if not self.dependencies:
            self.dependencies = [
                ToolDependency(kind="env", name=name)
                for name in self.required_env_vars
            ] + [
                ToolDependency(kind="binary", name=name)
                for name in self.required_binaries
            ]
        if not self.input_schema and self.input_contract:
            self.input_schema = _contract_to_json_schema(self.input_contract)
        if not self.output_schema and self.output_contract:
            self.output_schema = _contract_to_json_schema(self.output_contract)
        if not self.cost_model.pricing and self.pricing:
            self.cost_model.pricing = self.pricing
        if self.retry_policy.timeout_seconds is None:
            self.retry_policy.timeout_seconds = self.timeout_seconds
        return self


def _contract_to_json_schema(contract: dict[str, Any]) -> dict[str, Any]:
    properties = contract.get("properties")
    if isinstance(properties, dict):
        return {
            "type": "object",
            "required": contract.get("required", []),
            "properties": {
                key: _property_to_json_schema(value)
                for key, value in properties.items()
            },
        }
    return dict(contract)


def _property_to_json_schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {"description": str(value)}


def _default_operations_for_capability(capability: str) -> dict[str, bool]:
    operations = {
        "validate": True,
        "estimate": True,
        "submit": True,
        "poll": False,
        "archive": False,
        "cancel": False,
    }
    if capability in {"avatar_video", "video_generation"}:
        operations["poll"] = True
        operations["archive"] = True
    elif capability in {
        "asset_ingestion",
        "browser_recording",
        "clip_export",
        "clip_selection",
        "image_generation",
        "interaction_highlight",
        "media_analysis",
        "music_generation",
        "screen_capture",
        "speech_to_text",
        "subtitle_generation",
        "tts",
        "video_composition",
    }:
        operations["archive"] = True
    return operations


class ToolCapabilitySummary(BaseModel):
    capability: str
    total: int
    available: int
    degraded: int
    unavailable: int
    providers: list[str]
    available_providers: list[str]
    tool_keys: list[str]
    fallback_tool_keys: list[str]


class ToolProviderSummary(BaseModel):
    provider: str
    total: int
    available: int
    degraded: int
    unavailable: int
    capabilities: list[str]
    tool_keys: list[str]


class ToolExecutionRequest(BaseModel):
    run_id: str
    stage_key: str
    tool_key: str
    payload: dict[str, Any]


class ToolExecutionResult(BaseModel):
    success: bool
    provider: str
    tool_key: str
    external_task_id: str | None = None
    assets: list[dict[str, Any]] = Field(default_factory=list)
    cost: float = 0.0
    duration_ms: int = 0
    raw_response_ref: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ToolAdapter(Protocol):
    definition: ToolDefinition

    def check_availability(self) -> ToolStatus:
        ...

    def estimate_cost(self, payload: dict[str, Any]) -> float:
        ...

    def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        ...
