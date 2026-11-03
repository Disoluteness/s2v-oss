from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ArtifactPayloadModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class IntakeBriefPayload(ArtifactPayloadModel):
    version: str = "1.0"
    language: str = "zh-CN"
    raw_script: str
    target_platform: str
    aspect_ratio: str
    target_duration_seconds: int


class ProductionPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    planner: dict[str, Any]
    title: str
    delivery: dict[str, Any]
    creative_strategy: dict[str, Any]
    tool_plan: list[dict[str, Any]]
    stage_plan: list[dict[str, Any]]
    review_checklist: list[str]


class SpokenScriptSegment(ArtifactPayloadModel):
    segment_id: str
    start_seconds: float | int
    end_seconds: float | int
    text: str
    caption: str | None = None


class SpokenScriptPayload(ArtifactPayloadModel):
    version: str = "1.0"
    planner: dict[str, Any] | None = None
    language: str = "zh-CN"
    total_duration_seconds: float | int | None = None
    segments: list[SpokenScriptSegment] = Field(min_length=1)
    full_text: str | None = None
    caption_style: dict[str, Any] | None = None


class ShotPayload(ArtifactPayloadModel):
    shot_id: str
    spoken_segment_ids: list[str] = Field(default_factory=list)
    duration_seconds: float | int | None = None


class ShotListPayload(ArtifactPayloadModel):
    version: str = "1.0"
    planner: dict[str, Any] | None = None
    aspect_ratio: str | None = None
    shots: list[ShotPayload] = Field(min_length=1)
    asset_requirements: list[dict[str, Any]] = Field(default_factory=list)
    subtitle_plan: dict[str, Any] = Field(default_factory=dict)
    music_plan: dict[str, Any] = Field(default_factory=dict)


class PreflightReportPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    pipeline: dict[str, Any]
    overall_status: str
    summary: str | dict[str, Any]
    stages: list[dict[str, Any]]
    blocking_issues: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


class ExecutionPlanPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    source: dict[str, Any]
    pipeline: dict[str, Any]
    execution_policy: str
    can_execute: bool
    summary: str
    stage_plans: list[dict[str, Any]]
    decision_log: list[dict[str, Any]]
    blocking_issues: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


class ProposalOptionPayload(ArtifactPayloadModel):
    option_id: str
    label: str
    status: str
    positioning: str
    tool_path: list[dict[str, Any]] = Field(default_factory=list)
    stage_plan: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any]
    estimated_timeline: dict[str, Any]
    quality_tradeoffs: list[str] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    style_preview: dict[str, Any]
    recommended_for: list[str] = Field(default_factory=list)


class ProposalPacketPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    source: dict[str, Any]
    pipeline: dict[str, Any]
    run_input: dict[str, Any] = Field(default_factory=dict)
    summary: str
    recommended_option_id: str
    options: list[ProposalOptionPayload] = Field(min_length=2, max_length=3)
    budget_summary: dict[str, Any]
    timeline_summary: dict[str, Any]
    risk_summary: dict[str, Any]
    style_preview: dict[str, Any]
    approval_gate: dict[str, Any]
    next_actions: list[dict[str, Any]] = Field(default_factory=list)


class StageExecutionManifestPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    run_id: str
    pipeline: dict[str, Any]
    source: dict[str, Any]
    summary: dict[str, Any]
    stage_tasks: list[dict[str, Any]]


class AssetManifestPayload(ArtifactPayloadModel):
    version: str = "1.0"
    assets: list[dict[str, Any]]
    reference_assets: list[dict[str, Any]] = Field(default_factory=list)


class EditPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    timeline: list[dict[str, Any]]
    output: dict[str, Any]


class CompositionGraphPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    source: dict[str, Any]
    render_profile: dict[str, Any] = Field(default_factory=dict)
    canvas: dict[str, Any]
    duration_seconds: float | int
    timeline: dict[str, Any]
    tracks: list[dict[str, Any]]
    layers: list[dict[str, Any]]
    captions: dict[str, Any] = Field(default_factory=dict)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    effects: list[dict[str, Any]] = Field(default_factory=list)
    audio_mix: dict[str, Any] = Field(default_factory=dict)
    render_targets: list[dict[str, Any]] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)


class CanvasRenderReportPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    renderer: str
    composition_graph_artifact_id: str
    html_asset_id: str
    output: dict[str, Any]
    inputs: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    first_frame: dict[str, Any] = Field(default_factory=dict)


class RenderSummaryPayload(ArtifactPayloadModel):
    version: str = "1.0"
    renderer: str
    render_asset_id: str
    output: dict[str, Any]
    inputs: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)


class QualityReportPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    runner: str
    status: str
    quality_score: int
    min_score: int
    media: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]]
    summary: dict[str, Any]
    human_review: dict[str, Any]
    commands: list[list[str]] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)


class EditOperationReportPayload(ArtifactPayloadModel):
    schema_version: str = "1.0"
    generated_at: str
    operation: str
    status: str
    source_asset_ids: list[str]
    output_asset_ids: list[str]
    outputs: list[dict[str, Any]]
    settings: dict[str, Any] = Field(default_factory=dict)
    commands: list[list[str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PublishPackagePayload(ArtifactPayloadModel):
    version: str = "1.0"
    render_asset_id: str
    final_video_url: str | None = None
    workspace_url: str | None = None


class ReviewNotesPayload(ArtifactPayloadModel):
    version: str = "1.0"
    stage_key: str | None = None
    status: str | None = None
    notes: list[dict[str, Any]] = Field(default_factory=list)


class SourceMediaReviewPayload(ArtifactPayloadModel):
    version: str = "1.0"
    source_assets: list[dict[str, Any]] = Field(default_factory=list)
    source_links: list[dict[str, Any]] = Field(default_factory=list)
    ingest_notes: list[dict[str, Any]] = Field(default_factory=list)


class SourceAssetManifestPayload(ArtifactPayloadModel):
    version: str = "1.0"
    run_id: str
    source_assets: list[dict[str, Any]] = Field(default_factory=list)
    source_links: list[dict[str, Any]] = Field(default_factory=list)
    source_context: dict[str, Any] = Field(default_factory=dict)
    ingest_notes: list[dict[str, Any]] = Field(default_factory=list)
    analysis_entry: dict[str, Any] = Field(default_factory=dict)
    ingest_status: str = "ready"


class MediaAnalysisReportPayload(ArtifactPayloadModel):
    version: str = "1.0"
    media_summary: dict[str, Any] = Field(default_factory=dict)
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    transcript: dict[str, Any] | None = None
    analysis_notes: list[dict[str, Any]] = Field(default_factory=list)


class VideoAnalysisBriefPayload(ArtifactPayloadModel):
    version: str = "1.0"
    run_id: str
    summary: dict[str, Any] = Field(default_factory=dict)
    source_assets: list[dict[str, Any]] = Field(default_factory=list)
    clip_planning_inputs: dict[str, Any] = Field(default_factory=dict)
    limitations: list[dict[str, Any]] = Field(default_factory=list)


class ScreeningRoomShotPayload(ArtifactPayloadModel):
    shot_id: str
    shot_number: int
    start_seconds: float | int
    end_seconds: float | int
    duration_seconds: float | int
    visual: str
    dialogue: str | None = None
    caption: str | None = None
    camera: str | None = None
    pace: str | None = None
    music: str | None = None
    sfx: str | None = None
    notes: list[str] = Field(default_factory=list)


class ScreeningRoomScenePlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    source: dict[str, Any]
    title: str
    language: str = "zh-CN"
    target_platform: str
    aspect_ratio: str
    target_duration_seconds: float | int
    theme: str | None = None
    hook: str | None = None
    overview: str | None = None
    style: str | None = None
    shots: list[ScreeningRoomShotPayload] = Field(min_length=1)
    full_dialogue: str | None = None
    caption_text: str | None = None
    parse_warnings: list[dict[str, Any]] = Field(default_factory=list)


class ClipCandidateListPayload(ArtifactPayloadModel):
    version: str = "1.0"
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selection_criteria: list[str] = Field(default_factory=list)
    review_status: str = "pending_review"


class ClipExportManifestPayload(ArtifactPayloadModel):
    version: str = "1.0"
    clips: list[dict[str, Any]] = Field(default_factory=list)
    export_status: str = "planned"
    render_profile: dict[str, Any] = Field(default_factory=dict)


class DemoFlowPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    objective: str | None = None
    audience: str | None = None
    product_surface: dict[str, Any] = Field(default_factory=dict)
    demo_steps: list[dict[str, Any]] = Field(default_factory=list)
    prerequisites: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)


class RecordingPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    target_url: str | None = None
    viewport: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    privacy_masks: list[dict[str, Any]] = Field(default_factory=list)
    capture_settings: dict[str, Any] = Field(default_factory=dict)


class ScreenCaptureManifestPayload(ArtifactPayloadModel):
    version: str = "1.0"
    captures: list[dict[str, Any]] = Field(default_factory=list)
    screenshots: list[dict[str, Any]] = Field(default_factory=list)
    capture_status: str = "planned"
    metadata: dict[str, Any] = Field(default_factory=dict)


class InteractionAnnotationPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    annotations: list[dict[str, Any]] = Field(default_factory=list)
    callouts: list[dict[str, Any]] = Field(default_factory=list)
    cursor_plan: dict[str, Any] = Field(default_factory=dict)
    audio_cues: list[dict[str, Any]] = Field(default_factory=list)


class LocalizationPlanPayload(ArtifactPayloadModel):
    version: str = "1.0"
    target_languages: list[dict[str, Any]] = Field(default_factory=list)
    glossary: list[dict[str, Any]] = Field(default_factory=list)
    voice_casting: list[dict[str, Any]] = Field(default_factory=list)
    lip_sync_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DubbingManifestPayload(ArtifactPayloadModel):
    version: str = "1.0"
    dub_audio_tracks: list[dict[str, Any]] = Field(default_factory=list)
    subtitle_tracks: list[dict[str, Any]] = Field(default_factory=list)
    alignment_status: str = "planned"
    lip_sync_status: str = "not_requested"
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoryOutlinePayload(ArtifactPayloadModel):
    version: str = "1.0"
    beats: list[dict[str, Any]] = Field(default_factory=list)
    user_asset_plan: dict[str, Any] = Field(default_factory=dict)
    supplemental_asset_plan: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactValidationError(ValueError):
    def __init__(
        self,
        *,
        artifact_type: str,
        schema_key: str,
        errors: list[str],
    ) -> None:
        self.artifact_type = artifact_type
        self.schema_key = schema_key
        self.errors = errors
        joined = "; ".join(errors)
        super().__init__(f"{artifact_type} failed {schema_key} validation: {joined}")


@dataclass(frozen=True)
class ArtifactSchemaDefinition:
    artifact_type: str
    schema_version: str
    schema_key: str
    model: type[BaseModel]
    allow_placeholder: bool = False

    def validate(self, payload: dict[str, Any]) -> None:
        if self.allow_placeholder and _is_placeholder_payload(payload):
            return
        try:
            self.model.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactValidationError(
                artifact_type=self.artifact_type,
                schema_key=self.schema_key,
                errors=_format_validation_errors(exc),
            ) from exc


class ArtifactSchemaRegistry:
    def __init__(self, schemas: list[ArtifactSchemaDefinition] | None = None) -> None:
        self._schemas: dict[tuple[str, str], ArtifactSchemaDefinition] = {}
        for schema in schemas or _default_schemas():
            self._schemas[(schema.artifact_type, schema.schema_version)] = schema

    def schema_for(
        self,
        artifact_type: str,
        schema_version: str = "1.0",
    ) -> ArtifactSchemaDefinition | None:
        return self._schemas.get((artifact_type, schema_version))

    def schema_key_for(
        self,
        artifact_type: str,
        schema_version: str = "1.0",
    ) -> str | None:
        schema = self.schema_for(artifact_type, schema_version)
        return schema.schema_key if schema else None

    def validate(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        schema_version: str = "1.0",
    ) -> ArtifactSchemaDefinition | None:
        schema = self.schema_for(artifact_type, schema_version)
        if schema is None:
            return None
        schema.validate(payload)
        return schema

    def list_schemas(self) -> list[ArtifactSchemaDefinition]:
        return sorted(
            self._schemas.values(),
            key=lambda schema: (schema.artifact_type, schema.schema_version),
        )


def _default_schemas() -> list[ArtifactSchemaDefinition]:
    return [
        ArtifactSchemaDefinition("intake_brief", "1.0", "intake_brief.v1", IntakeBriefPayload),
        ArtifactSchemaDefinition(
            "production_plan",
            "1.0",
            "production_plan.v1",
            ProductionPlanPayload,
        ),
        ArtifactSchemaDefinition("spoken_script", "1.0", "spoken_script.v1", SpokenScriptPayload, allow_placeholder=True),
        ArtifactSchemaDefinition("shot_list", "1.0", "shot_list.v1", ShotListPayload, allow_placeholder=True),
        ArtifactSchemaDefinition(
            "preflight_report",
            "1.0",
            "preflight_report.v1",
            PreflightReportPayload,
        ),
        ArtifactSchemaDefinition(
            "execution_plan",
            "1.0",
            "execution_plan.v1",
            ExecutionPlanPayload,
        ),
        ArtifactSchemaDefinition(
            "proposal_packet",
            "1.0",
            "proposal_packet.v1",
            ProposalPacketPayload,
        ),
        ArtifactSchemaDefinition(
            "stage_execution_manifest",
            "1.0",
            "stage_execution_manifest.v1",
            StageExecutionManifestPayload,
        ),
        ArtifactSchemaDefinition(
            "asset_manifest",
            "1.0",
            "asset_manifest.v1",
            AssetManifestPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "edit_plan",
            "1.0",
            "edit_plan.v1",
            EditPlanPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "composition_graph",
            "1.0",
            "composition_graph.v1",
            CompositionGraphPayload,
        ),
        ArtifactSchemaDefinition(
            "canvas_render_report",
            "1.0",
            "canvas_render_report.v1",
            CanvasRenderReportPayload,
        ),
        ArtifactSchemaDefinition(
            "render_summary",
            "1.0",
            "render_summary.v1",
            RenderSummaryPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "quality_report",
            "1.0",
            "quality_report.v1",
            QualityReportPayload,
        ),
        ArtifactSchemaDefinition(
            "edit_operation_report",
            "1.0",
            "edit_operation_report.v1",
            EditOperationReportPayload,
        ),
        ArtifactSchemaDefinition(
            "publish_package",
            "1.0",
            "publish_package.v1",
            PublishPackagePayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "review_notes",
            "1.0",
            "review_notes.v1",
            ReviewNotesPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "source_media_review",
            "1.0",
            "source_media_review.v1",
            SourceMediaReviewPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "source_asset_manifest",
            "1.0",
            "source_asset_manifest.v1",
            SourceAssetManifestPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "media_analysis_report",
            "1.0",
            "media_analysis_report.v1",
            MediaAnalysisReportPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "video_analysis_brief",
            "1.0",
            "video_analysis_brief.v1",
            VideoAnalysisBriefPayload,
        ),
        ArtifactSchemaDefinition(
            "screening_room_scene_plan",
            "1.0",
            "screening_room_scene_plan.v1",
            ScreeningRoomScenePlanPayload,
        ),
        ArtifactSchemaDefinition(
            "clip_candidate_list",
            "1.0",
            "clip_candidate_list.v1",
            ClipCandidateListPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "clip_export_manifest",
            "1.0",
            "clip_export_manifest.v1",
            ClipExportManifestPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "demo_flow_plan",
            "1.0",
            "demo_flow_plan.v1",
            DemoFlowPlanPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "recording_plan",
            "1.0",
            "recording_plan.v1",
            RecordingPlanPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "screen_capture_manifest",
            "1.0",
            "screen_capture_manifest.v1",
            ScreenCaptureManifestPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "interaction_annotation_plan",
            "1.0",
            "interaction_annotation_plan.v1",
            InteractionAnnotationPlanPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "localization_plan",
            "1.0",
            "localization_plan.v1",
            LocalizationPlanPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "dubbing_manifest",
            "1.0",
            "dubbing_manifest.v1",
            DubbingManifestPayload,
            allow_placeholder=True,
        ),
        ArtifactSchemaDefinition(
            "story_outline",
            "1.0",
            "story_outline.v1",
            StoryOutlinePayload,
            allow_placeholder=True,
        ),
    ]


def _is_placeholder_payload(payload: dict[str, Any]) -> bool:
    return payload.get("status") == "placeholder" and isinstance(payload.get("stage_key"), str)


def _format_validation_errors(exc: ValidationError) -> list[str]:
    formatted = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        formatted.append(f"{location}: {error['msg']}")
    return formatted
