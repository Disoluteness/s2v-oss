import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field, model_validator


class StageArtifactContract(BaseModel):
    artifact_type: str
    required: bool = True
    schema_key: str | None = None
    source_stage_key: str | None = None
    description: str | None = None


class StageToolRequirement(BaseModel):
    capability: str
    required: bool = True
    selector_policy: str = "best_available"
    dispatch_policy: str = "planned"
    description: str | None = None


class StageReviewPolicy(BaseModel):
    required: bool = False
    gate_type: str = "human_review"
    required_before_next_stage: bool = True
    review_focus: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class StageDispatchPolicy(BaseModel):
    mode: str = "planned"
    auto_dispatch_required_tools: bool = True
    idempotency_scope: str = "run_stage_tool"


class StageDefinition(BaseModel):
    stage_key: str
    display_name: str
    description: str | None = None
    order: int
    required_input_artifacts: list[str] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    timeout_seconds: int = 600
    depends_on_stage_keys: list[str] = Field(default_factory=list)
    input_artifacts: list[StageArtifactContract] = Field(default_factory=list)
    output_artifacts: list[StageArtifactContract] = Field(default_factory=list)
    tool_requirements: list[StageToolRequirement] = Field(default_factory=list)
    review_policy: StageReviewPolicy | None = None
    dispatch_policy: StageDispatchPolicy | None = None
    success_criteria: list[str] = Field(default_factory=list)
    skippable: bool = False
    skip_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_dsl_fields(self) -> "StageDefinition":
        if not self.input_artifacts and self.required_input_artifacts:
            self.input_artifacts = [
                StageArtifactContract(artifact_type=artifact_type, required=True)
                for artifact_type in self.required_input_artifacts
            ]
        if not self.output_artifacts and self.output_artifact_types:
            self.output_artifacts = [
                StageArtifactContract(artifact_type=artifact_type, required=True)
                for artifact_type in self.output_artifact_types
            ]
        if not self.required_input_artifacts:
            self.required_input_artifacts = [
                artifact.artifact_type for artifact in self.input_artifacts if artifact.required
            ]
        if not self.output_artifact_types:
            self.output_artifact_types = [
                artifact.artifact_type for artifact in self.output_artifacts
            ]
        if not self.tool_requirements and (
            self.required_capabilities or self.optional_capabilities
        ):
            self.tool_requirements = [
                StageToolRequirement(capability=capability, required=True)
                for capability in self.required_capabilities
            ] + [
                StageToolRequirement(capability=capability, required=False)
                for capability in self.optional_capabilities
            ]
        if not self.required_capabilities:
            self.required_capabilities = [
                requirement.capability
                for requirement in self.tool_requirements
                if requirement.required
            ]
        if not self.optional_capabilities:
            self.optional_capabilities = [
                requirement.capability
                for requirement in self.tool_requirements
                if not requirement.required
            ]
        if self.review_policy is None:
            self.review_policy = StageReviewPolicy(
                required=self.requires_human_review,
                success_criteria=self.success_criteria,
            )
        self.requires_human_review = bool(
            self.requires_human_review or self.review_policy.required
        )
        if not self.success_criteria and self.review_policy.success_criteria:
            self.success_criteria = list(self.review_policy.success_criteria)
        if self.dispatch_policy is None:
            self.dispatch_policy = StageDispatchPolicy()
        return self


class PipelineDefinition(BaseModel):
    dsl_version: str = "1.0"
    pipeline_key: str
    display_name: str
    description: str
    version: str
    category: str = "custom"
    status: str = "draft"
    supported_platforms: list[str] = Field(default_factory=list)
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    default_language: str = "zh-CN"
    default_stage_timeout_seconds: int = 600
    entrypoint_stage_key: str | None = None
    artifact_contracts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    stages: list[StageDefinition]

    @model_validator(mode="after")
    def normalize_pipeline(self) -> "PipelineDefinition":
        self.stages = self.ordered_stages()
        if self.entrypoint_stage_key is None and self.stages:
            self.entrypoint_stage_key = self.stages[0].stage_key
        if not self.required_capabilities:
            self.required_capabilities = sorted(
                {
                    capability
                    for stage in self.stages
                    for capability in stage.required_capabilities
                    if capability != "llm_generation"
                }
            )
        if not self.optional_capabilities:
            self.optional_capabilities = sorted(
                {
                    capability
                    for stage in self.stages
                    for capability in stage.optional_capabilities
                }
            )
        return self

    def ordered_stages(self) -> list[StageDefinition]:
        return sorted(self.stages, key=lambda stage: stage.order)

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def stable_content_hash(self) -> str:
        snapshot = self.snapshot()
        snapshot.pop("content_hash", None)
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()
