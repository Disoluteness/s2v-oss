from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from apps.api.app.db import Base


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class TenantRecord(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    plan: Mapped[str] = mapped_column(String(64), default="internal")
    status: Mapped[str] = mapped_column(String(32), default="active")

    users: Mapped[list[UserRecord]] = relationship(back_populates="tenant")
    projects: Mapped[list[ProjectRecord]] = relationship(back_populates="tenant")


class UserRecord(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(64), default="owner")
    status: Mapped[str] = mapped_column(String(32), default="active")
    # V1.1 username-password login. Both columns are optional on purpose: every
    # identity that predates the login feature (header- and API-key-provisioned
    # reviewers) has no password and must keep working unchanged, which is why
    # "no hash" reads as "this user cannot log in", never as "this row is broken".
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    password_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped[TenantRecord] = relationship(back_populates="users")
    sessions: Mapped[list[UserSessionRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserSessionRecord(Base, TimestampMixin):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    # The same split the API keys use: a short point-lookup handle plus a digest
    # of the full token. The plaintext is returned once at login and never
    # stored; only the peppered SHA-256 is.
    token_prefix: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Revocation is a timestamp rather than a status string: "not revoked yet"
    # and "revoked" are the only two states a session has, and a NULL column
    # keeps the check a single comparison in the hot verify path.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[UserRecord] = relationship(back_populates="sessions")


class ProjectRecord(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_aspect_ratio: Mapped[str] = mapped_column(String(16), default="9:16")
    default_language: Mapped[str] = mapped_column(String(16), default="zh-CN")
    default_style_preset_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # V8.3 attribution edge: which team's quota this project's spend counts
    # against. NULL is legal and means *the default team* — the same reading the
    # actor layer gives an unseated identity, and the only value a migration can
    # write before anyone has been seated.
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")

    tenant: Mapped[TenantRecord] = relationship(back_populates="projects")
    runs: Mapped[list[ProductionRunRecord]] = relationship(back_populates="project")


class BrandKitRecord(Base, TimestampMixin):
    __tablename__ = "brand_kits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    color_palette: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    typography: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    safe_area: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    brand_voice: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    banned_terms: Mapped[list[str]] = mapped_column(JSON, default=list)
    brand_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class StylePresetRecord(Base, TimestampMixin):
    __tablename__ = "style_presets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    brand_kit_id: Mapped[str | None] = mapped_column(
        ForeignKey("brand_kits.id"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    style_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    aspect_ratio: Mapped[str | None] = mapped_column(String(16), nullable=True)
    subtitle_style: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    visual_style: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tone: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prompt_guidelines: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    render_defaults: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    guardrails: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    preset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class ProviderCredentialRecord(Base, TimestampMixin):
    __tablename__ = "provider_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    credential_type: Mapped[str] = mapped_column(String(64), default="api_key")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    secret_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    quota: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    credential_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_check_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_check_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EncryptedSecretRecord(Base, TimestampMixin):
    """Ciphertext for a value the platform must be able to hand back out (V8.4).

    Kept in its own table rather than as a column on the rows that reference it,
    for a reason that shows up on the read path: listing provider credentials no
    longer loads any secret material at all, and re-sealing after a key rotation
    rewrites this table without touching the credential row.

    ``algorithm`` and ``key_version`` travel with each blob, so a rotation is a
    data migration over this table alone.
    """

    __tablename__ = "encrypted_secrets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    algorithm: Mapped[str] = mapped_column(String(32), default="AESGCM-256")
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    ciphertext: Mapped[str] = mapped_column(Text)
    secret_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineVersionRecord(Base, TimestampMixin):
    __tablename__ = "pipeline_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pipeline_key: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    dsl_version: Mapped[str] = mapped_column(String(32), default="1.0")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ProductionRunRecord(Base, TimestampMixin):
    __tablename__ = "production_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    pipeline_key: Mapped[str] = mapped_column(String(120), index=True)
    pipeline_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("pipeline_versions.id"),
        nullable=True,
        index=True,
    )
    pipeline_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_dsl_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pipeline_definition_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    target_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    style_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    actual_cost: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)

    project: Mapped[ProjectRecord] = relationship(back_populates="runs")
    stages: Mapped[list[StageRunRecord]] = relationship(back_populates="run")
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="run")


class StageRunRecord(Base, TimestampMixin):
    __tablename__ = "stage_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(120), index=True)
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    output_artifact_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    invalidated_by_stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[ProductionRunRecord] = relationship(back_populates="stages")


class ArtifactRecord(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage_key: Mapped[str] = mapped_column(String(120), index=True)
    artifact_type: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_version: Mapped[str] = mapped_column(String(32), default="1.0")
    schema_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    producer: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lineage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    derived_from: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    run: Mapped[ProductionRunRecord] = relationship(back_populates="artifacts")


class AssetRecord(Base, TimestampMixin):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(120), index=True)
    provider: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tool_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    storage_url: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ToolInvocationRecord(Base, TimestampMixin):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_key: Mapped[str] = mapped_column(String(120), index=True)
    provider: Mapped[str] = mapped_column(String(120), index=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    cost: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_taxonomy_code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_remediation: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackgroundJobRecord(Base, TimestampMixin):
    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_taxonomy_code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    queue: Mapped[str] = mapped_column(String(40), default="media", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UsageLedgerRecord(Base, TimestampMixin):
    __tablename__ = "usage_ledger_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    tool_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(160), index=True)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    actual_cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asset_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage_quantity: Mapped[float | None] = mapped_column(Numeric(16, 4), nullable=True)
    entry_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EventLogRecord(Base, TimestampMixin):
    __tablename__ = "event_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tool_invocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(32), default="info", index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RunDecisionRecord(Base, TimestampMixin):
    """Why a choice was made, as opposed to who changed what.

    ``audit_logs`` answers "who touched this"; this table answers "why this tool
    and not the other one". One row is one decision with the alternatives that
    were considered, so a reviewer can audit the reasoning after the fact
    instead of re-running the selector and hoping it agrees.

    Rows are append-only — re-planning a run adds rows rather than rewriting
    them — and the current value of a ``(category, subject)`` pair is the
    highest ``sequence`` row. That is the same convention ``review_decisions``
    already uses, and it keeps the history available for "what changed and when".
    """

    __tablename__ = "run_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``tool_selection`` / ``review_decision`` / ``quality_downgrade`` /
    # ``paid_provider_consent``. Free text rather than an enum column: a new
    # write point should not need a migration.
    category: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    decision: Mapped[str] = mapped_column(String(64), index=True)
    # ``chosen`` is the winner in a stable shape ({tool_key, provider, label}),
    # ``candidates`` the runners-up with their scores. Both are JSON because the
    # shape legitimately differs per category and querying inside them is not a
    # requirement — the endpoint filters by category/subject, not by payload.
    chosen: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Nullable on purpose: a human decision has no score, and inventing one would
    # make the column look more authoritative than the data behind it.
    # ``confidence_basis`` says which measurement produced the number.
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    confidence_basis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), default="system")
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReviewRequestRecord(Base, TimestampMixin):
    """A single gate review item bound to one stage execution and one artifact scope."""

    __tablename__ = "review_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("production_runs.id"), index=True)
    stage_run_id: Mapped[str] = mapped_column(ForeignKey("stage_runs.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(120), index=True)
    pipeline_key: Mapped[str] = mapped_column(String(120), index=True)
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    current_decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    gate_type: Mapped[str] = mapped_column(String(32), default="human_review")
    gates_stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    artifact_scope: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    review_focus: Mapped[list[str]] = mapped_column(JSON, default=list)
    success_criteria: Mapped[list[str]] = mapped_column(JSON, default=list)
    assignee_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    decisions: Mapped[list[ReviewDecisionRecord]] = relationship(
        back_populates="review_request",
        order_by="ReviewDecisionRecord.sequence",
    )
    comments: Mapped[list[ReviewCommentRecord]] = relationship(
        back_populates="review_request",
    )
    action_items: Mapped[list[ReviewActionItemRecord]] = relationship(
        back_populates="review_request",
    )
    attachments: Mapped[list[ReviewAttachmentRecord]] = relationship(
        back_populates="review_request",
    )


class ReviewDecisionRecord(Base, TimestampMixin):
    """Append-only decision log. The current value is the highest sequence row."""

    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    review_request_id: Mapped[str] = mapped_column(
        ForeignKey("review_requests.id"),
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    round: Mapped[int] = mapped_column(Integer, default=1)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    actor_display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    review_request: Mapped[ReviewRequestRecord] = relationship(back_populates="decisions")


class ReviewCommentRecord(Base, TimestampMixin):
    """A reviewer remark anchored to a specific artifact version."""

    __tablename__ = "review_comments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    review_request_id: Mapped[str] = mapped_column(
        ForeignKey("review_requests.id"),
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    stage_key: Mapped[str] = mapped_column(String(120), index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    author_display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    severity: Mapped[str] = mapped_column(String(24), default="suggestion", index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    resolved_by_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    review_request: Mapped[ReviewRequestRecord] = relationship(back_populates="comments")


class ReviewActionItemRecord(Base, TimestampMixin):
    """A reviewer ask turned into a trackable action reusing existing action contracts."""

    __tablename__ = "review_action_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    review_request_id: Mapped[str] = mapped_column(
        ForeignKey("review_requests.id"),
        index=True,
    )
    decision_id: Mapped[str] = mapped_column(ForeignKey("review_decisions.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    stage_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    artifact_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    stage_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    linked_comment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_action: Mapped[str | None] = mapped_column(String(48), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    produced_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    review_request: Mapped[ReviewRequestRecord] = relationship(back_populates="action_items")


class ReviewAttachmentRecord(Base, TimestampMixin):
    """An evidence asset mounted on a review request (reference, not a copy)."""

    __tablename__ = "review_attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    review_request_id: Mapped[str] = mapped_column(
        ForeignKey("review_requests.id"),
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    added_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    added_by_display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)

    review_request: Mapped[ReviewRequestRecord] = relationship(back_populates="attachments")


class AssetCollectionRecord(Base, TimestampMixin):
    """A reusable, project-scoped box of assets (person art, voice, music, clips)."""

    __tablename__ = "asset_collections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    collection_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    items: Mapped[list[AssetCollectionItemRecord]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
    )


class AssetCollectionItemRecord(Base, TimestampMixin):
    """A reference from a collection to a run asset, carrying provenance and rights.

    ``usage_role`` and ``license_terms`` are named that way on purpose: ``role`` is a
    reserved word on MySQL 8 and ``license`` reads ambiguously next to the asset's own
    metadata. The API still exposes them as ``role`` / ``license``.
    """

    __tablename__ = "asset_collection_items"
    __table_args__ = (
        UniqueConstraint(
            "collection_id",
            "asset_id",
            name="uq_asset_collection_items_collection_asset",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    collection_id: Mapped[str] = mapped_column(ForeignKey("asset_collections.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    usage_role: Mapped[str] = mapped_column(String(120), default="reference", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    license_terms: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    collection: Mapped[AssetCollectionRecord] = relationship(back_populates="items")


class TeamRecord(Base, TimestampMixin):
    """A collaboration unit inside a tenant. One deployment keeps a default team."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_teams_tenant_slug"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    memberships: Mapped[list[TeamMembershipRecord]] = relationship(
        back_populates="team",
        cascade="all, delete-orphan",
    )


class TeamMembershipRecord(Base, TimestampMixin):
    """Binds a user to a team with a role.

    ``member_role`` avoids the MySQL 8 reserved word ``role``; the API exposes it
    as ``role``, matching how the asset library maps ``usage_role``.
    """

    __tablename__ = "team_memberships"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_user"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    member_role: Mapped[str] = mapped_column(String(32), default="member", index=True)
    invited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    team: Mapped[TeamRecord] = relationship(back_populates="memberships")


class ProjectMembershipRecord(Base, TimestampMixin):
    """Narrows or widens a user's access to one project, on top of the team role."""

    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_user"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    member_role: Mapped[str] = mapped_column(String(32), default="editor", index=True)
    invited_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class TeamQuotaRecord(Base, TimestampMixin):
    """One team's spend ceiling for one period (V8.3).

    Kept as a single mutable row per team rather than an append-only history:
    the quota is read on the hot path of every paid call, so "what is in force
    right now" has to be a primary-key-ish lookup. Changes are auditable through
    ``audit_logs`` (V6.5), which is where privileged, non-run-scoped events
    already live.

    ``limit_*`` columns are all nullable and ``NULL`` means *no ceiling on this
    dimension*; a row with every limit null is therefore indistinguishable from
    no quota at all, and both read as ``not_configured``.
    """

    __tablename__ = "team_quotas"
    __table_args__ = (UniqueConstraint("team_id", name="uq_team_quotas_team_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), index=True)
    period: Mapped[str] = mapped_column(String(32), default="calendar_month")
    limit_cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    limit_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enforcement: Mapped[str] = mapped_column(String(16), default="hard")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLogRecord(Base, TimestampMixin):
    """Append-only trail for privileged, non-run-scoped operations.

    ``event_logs`` requires a non-null ``run_id``, so membership and team changes
    have nowhere to go there. This table is the V6.5 home for them.
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class IdempotencyKeyRecord(Base, TimestampMixin):
    """One claimed ``Idempotency-Key`` and the response it produced.

    ``idempotency_key`` avoids the MySQL 8 reserved word ``key``; the API and the
    SDK both call it ``idempotency_key`` as well, so there is no alias layer.
    ``state`` is ``in_progress`` until a 2xx JSON response is recorded, at which
    point it becomes ``completed`` and later retries replay ``response_body``.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "actor_id",
            "idempotency_key",
            name="uq_idempotency_keys_scope_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512))
    request_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="in_progress", index=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


class ApiKeyRecord(Base, TimestampMixin):
    """A programmatic credential (V7.2).

    Only a salted hash of the credential is kept — the plaintext is returned once,
    at creation, and never again. ``key_prefix`` is the non-secret half and is the
    unique handle authentication looks up on, so verifying a key is one indexed
    point read instead of a compare against every row.

    ``actor_id`` carries no foreign key on purpose: identities originate in
    whatever SSO / gateway sits in front of the API (the same reason
    ``audit_logs.actor_id`` is unconstrained), and ``ApiKeyRepository`` registers
    the owner through ``UserRepository.ensure_user`` when a key is issued.
    ``scopes`` is ``None`` for "everything the owner may do" or a JSON array of
    permission strings that narrows the key below the owner's own permissions —
    narrowing only, never widening.
    """

    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160))
    key_prefix: Mapped[str] = mapped_column(String(32), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )


class WebhookSubscriptionRecord(Base, TimestampMixin):
    """A tenant's endpoint subscription to published webhook events (V7.3).

    ``event_types`` holds :class:`~apps.api.app.webhooks.WebhookEvent` values —
    the *public* vocabulary — so what a subscriber asked for is stored in the
    same words it asked in, independent of how the internal event log is named.

    ``secret`` is stored as-is rather than hashed, and that is not an oversight:
    the platform has to *produce* an HMAC with it on every delivery, so unlike an
    API key it cannot be reduced to a digest. ``provider_credentials`` makes the
    same trade; encrypting secrets at rest is V8.4's subject, and keeping the
    shape here identical means that work has one place to land rather than two.

    ``consecutive_failures`` exists so a dead endpoint can be paused
    automatically; without it a subscriber that moved on keeps accumulating
    delivery rows and retry traffic forever.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160))
    url: Mapped[str] = mapped_column(String(500))
    secret: Mapped[str] = mapped_column(String(64))
    event_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class WebhookDeliveryRecord(Base, TimestampMixin):
    """One attempt-able copy of one published event bound for one subscription.

    This is a transactional outbox row, not a log: it is written in the same
    transaction as the event that caused it, and the worker's job is to drain it.
    A failed delivery is retried with backoff until ``max_attempts``; after that
    it is ``abandoned`` rather than deleted, so an operator can still see what was
    missed and redeliver it by hand.

    ``run_id`` and ``event_id`` are plain indexed strings rather than foreign
    keys, because a delivery is a *snapshot* of the event (``payload`` is copied,
    not referenced). Tying it to rows that could be reshaped later would make the
    record stop meaning what it said at the time it was queued.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("webhook_subscriptions.id"),
        index=True,
    )
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signature: Mapped[str | None] = mapped_column(String(160), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ExternalIntegrationRecord(Base, TimestampMixin):
    """A registered host platform that may embed the Studio (V7.4).

    The row is a *registration*, not a session: embed sessions are stateless
    signed tokens, so nothing here grows with usage and there is no cleanup job.
    What the platform needs from this row at verification time is only two things
    — is it still ``active``, and which origins may frame it — which is one
    indexed point read.

    ``allowed_origins`` holds complete origins (``https://portal.example.com``),
    not patterns: the value is emitted verbatim as the embed page's
    ``frame-ancestors``, and a permissive entry there is a clickjacking hole, so
    the format is kept to something a reviewer can check at a glance.

    Disabling an integration is the effective revocation lever — every token it
    minted stops verifying immediately, without needing a token table.
    """

    __tablename__ = "external_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_external_integrations_tenant_slug"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_origins: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DeploymentSettingRecord(Base, TimestampMixin):
    """The install's own settings — one row, and the only non-tenant-scoped table.

    Everything else in this schema belongs to a tenant; a deployment does not.
    The distinction matters for two reasons: the permission that guards these
    endpoints is a *tenant* permission statement about an install-wide row (so a
    project role can never confer it), and the row must be readable by every
    replica without a tenant filter, because "is this install in maintenance" is
    a property of the process, not of the caller.

    ``maintenance_mode`` is the one column written on the request hot path's
    read side: a conditional write guard reads it per mutating request. It is a
    single indexed primary-key lookup with no join, which is the cheapest shape
    that still lets one replica flip the switch for all of them — a cached
    in-process copy would be faster and wrong, because the replica that flipped
    it is not necessarily the one serving the next request.
    """

    __tablename__ = "deployment_settings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    maintenance_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    maintenance_updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backup_retention_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    last_backup_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_backup_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
