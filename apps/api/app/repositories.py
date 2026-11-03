from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from secrets import token_hex
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from apps.api.app.config import settings
from apps.api.app.db_models import (
    ApiKeyRecord,
    ArtifactRecord,
    AssetCollectionItemRecord,
    AssetCollectionRecord,
    AssetRecord,
    AuditLogRecord,
    BackgroundJobRecord,
    BrandKitRecord,
    DeploymentSettingRecord,
    EncryptedSecretRecord,
    EventLogRecord,
    ExternalIntegrationRecord,
    IdempotencyKeyRecord,
    PipelineVersionRecord,
    ProductionRunRecord,
    ProjectMembershipRecord,
    ProjectRecord,
    ProviderCredentialRecord,
    ReviewActionItemRecord,
    ReviewAttachmentRecord,
    ReviewCommentRecord,
    ReviewDecisionRecord,
    ReviewRequestRecord,
    StageRunRecord,
    StylePresetRecord,
    TeamMembershipRecord,
    TeamQuotaRecord,
    TeamRecord,
    TenantRecord,
    ToolInvocationRecord,
    UsageLedgerRecord,
    UserRecord,
    WebhookDeliveryRecord,
    WebhookSubscriptionRecord,
)
from apps.api.app.deployment import DEPLOYMENT_SETTINGS_ID


def _sorted(statement, record_cls, sort, fallback):
    """Apply list ordering: explicit sort clauses when asked, else the default.

    ``sort`` is the parsed ``(field, descending)`` tuple from
    ``apps.api.app.sorting.SortSpec.resolve`` — already whitelisted, with the
    primary key implicitly appended as the final tiebreaker. ``None`` keeps the
    repository's own default so a request without ``sort`` behaves exactly as
    it did before controlled sorting existed.
    """
    if not sort:
        return statement.order_by(*fallback)
    order = []
    for field, descending in sort:
        column = getattr(record_cls, field)
        order.append(column.desc() if descending else column.asc())
    return statement.order_by(*order)

from apps.api.app.embed import (
    EMBED_ALLOWED_SCOPES,
    EMBED_DEFAULT_SCOPES,
    EMBED_PAGES,
    EMBED_PATH,
    validate_origins,
)
from apps.api.app.embed import (
    default_ttl_seconds as embed_default_ttl_seconds,
)
from apps.api.app.embed import (
    max_ttl_seconds as embed_max_ttl_seconds,
)
from apps.api.app.models import (
    ApiKey,
    ApiKeyCreate,
    Artifact,
    Asset,
    AssetCollection,
    AssetCollectionCreate,
    AssetCollectionImportRequest,
    AssetCollectionImportResult,
    AssetCollectionItem,
    AssetCollectionItemAdd,
    AssetCollectionItemUpdate,
    AssetCollectionUpdate,
    AssetLicense,
    AssetProvenance,
    AuditLogEntry,
    BackgroundJob,
    BackgroundJobCreate,
    BackgroundJobStatus,
    BrandKit,
    BrandKitCreate,
    BrandKitUpdate,
    EncryptedSecretSummary,
    EventLog,
    EventSeverity,
    EventSourceType,
    ExternalIntegration,
    ExternalIntegrationCapabilities,
    ExternalIntegrationCreate,
    ExternalIntegrationUpdate,
    IdempotencyKeyEntry,
    ProductionRun,
    Project,
    ProjectAssetLibrary,
    ProjectCreate,
    ProjectMemberAdd,
    ProjectMembership,
    ProjectMemberUpdate,
    ProviderCredential,
    ProviderCredentialCheckResult,
    ProviderCredentialCreate,
    ProviderCredentialRotation,
    ProviderCredentialStatus,
    ProviderCredentialUpdate,
    ProviderSecretSource,
    ReviewActionItem,
    ReviewActionKind,
    ReviewActionStatus,
    ReviewArtifactRef,
    ReviewAttachment,
    ReviewComment,
    ReviewCommentSeverity,
    ReviewCommentStatus,
    ReviewDecision,
    ReviewDecisionType,
    ReviewRequest,
    ReviewRequestDetail,
    ReviewRequestStatus,
    RunCreate,
    RunStatus,
    StageRun,
    StageStatus,
    StylePreset,
    StylePresetCreate,
    StylePresetUpdate,
    Team,
    TeamCreate,
    TeamMemberAdd,
    TeamMembership,
    TeamMemberUpdate,
    TeamQuota,
    TeamQuotaEnforcement,
    TeamQuotaPeriod,
    TeamQuotaUpdate,
    TeamQuotaUsage,
    TeamUpdate,
    ToolInvocation,
    ToolInvocationStatus,
    UsageLedgerEntry,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
    WebhookSubscriptionCreate,
    WebhookSubscriptionStatus,
    WebhookSubscriptionUpdate,
)
from apps.api.app.permissions import (
    Permission,
    ProjectRole,
    TeamRole,
    coerce_project_role,
    coerce_team_role,
    effective_permissions,
)
from apps.api.app.quota import (
    STATUS_PAUSED,
    QuotaUsage,
    QuotaWindow,
    evaluate_quota,
)
from apps.api.app.secrets import (
    MANAGED_SECRET_PREFIX,
    SecretBox,
    SecretEncryptionError,
    is_managed_secret_ref,
)
from apps.api.app.webhooks import (
    WebhookEvent,
    build_webhook_payload,
    generate_webhook_secret,
    normalize_webhook_url,
    validate_webhook_url,
    webhook_event_for_internal,
)
from packages.artifact_kit import ArtifactSchemaRegistry
from packages.pipeline_kit.models import PipelineDefinition

DEFAULT_TENANT_ID = "tenant_default"
DEFAULT_TENANT_NAME = "Default Tenant"

#: Attempts a delivery gets before it is abandoned. Six with the default backoff
#: spans roughly half an hour, which covers a deploy or a brief outage without
#: letting a permanently dead endpoint accumulate retries for days.
WEBHOOK_DEFAULT_MAX_ATTEMPTS = 6


class SecretRepository:
    """The ciphertext vault (V8.4).

    Deliberately small and deliberately not enumerable: there is no "list every
    secret" method, because the only thing that may read a secret is something
    that already holds its reference. Rotation walks a credential's own
    references; nothing walks the table.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def put(
        self,
        *,
        tenant_id: str,
        name: str,
        plaintext: str,
        box: SecretBox,
        secret_metadata: dict[str, Any] | None = None,
    ) -> EncryptedSecretRecord:
        sealed = box.seal(plaintext, tenant_id=tenant_id, name=name)
        record = EncryptedSecretRecord(
            id=f"{MANAGED_SECRET_PREFIX}{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=name,
            algorithm=sealed.algorithm,
            key_version=sealed.key_version,
            ciphertext=sealed.ciphertext,
            secret_metadata=secret_metadata or {},
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get(self, secret_id: str) -> EncryptedSecretRecord | None:
        return self.session.get(EncryptedSecretRecord, secret_id)

    def open(
        self,
        secret_id: str,
        *,
        tenant_id: str,
        name: str,
        box: SecretBox,
    ) -> str:
        record = self.get(secret_id)
        if record is None:
            raise SecretEncryptionError(f"secret '{secret_id}' is referenced but missing")
        return box.open(
            record.ciphertext,
            tenant_id=tenant_id,
            name=name,
            key_version=record.key_version,
            algorithm=record.algorithm,
        )

    def reseal(
        self,
        record: EncryptedSecretRecord,
        *,
        box: SecretBox,
        tenant_id: str,
        name: str,
    ) -> None:
        """Decrypt under the stored key version and re-encrypt under the current one."""
        plaintext = box.open(
            record.ciphertext,
            tenant_id=tenant_id,
            name=name,
            key_version=record.key_version,
            algorithm=record.algorithm,
        )
        sealed = box.seal(plaintext, tenant_id=tenant_id, name=name)
        record.ciphertext = sealed.ciphertext
        record.algorithm = sealed.algorithm
        record.key_version = sealed.key_version
        record.rotated_at = datetime.now(UTC).replace(tzinfo=None)
        self.session.flush()

    def delete(self, secret_id: str) -> None:
        record = self.get(secret_id)
        if record is not None:
            self.session.delete(record)
            self.session.flush()


class ProviderCredentialRepository:
    """Provider credentials, with secret material routed through the vault.

    The write path is uniform and easy to state: **every value a caller submits is
    treated as plaintext**. When encryption is configured it is sealed into
    ``encrypted_secrets`` and the credential row keeps only the reference;
    otherwise the value is stored inline exactly as it was before V8.4. Accepting
    a ``sec_`` string from a caller is deliberately not supported — that would let
    a request point a credential at somebody else's vault row.
    """

    def __init__(self, session: Session, *, secret_box: SecretBox | None = None) -> None:
        self.session = session
        self.secrets = SecretRepository(session)
        # An unconfigured box is a *disabled* box, not an error: that is what
        # makes encryption opt-in rather than a deployment gate.
        self.secret_box = secret_box or SecretBox(key=None)

    # --- write -------------------------------------------------------------

    def _locators_for(
        self,
        values: dict[str, str],
        *,
        tenant_id: str,
    ) -> dict[str, str]:
        """Turn ``{name: plaintext}`` into what actually gets stored."""
        if not self.secret_box.enabled:
            return {name: str(value) for name, value in values.items()}
        locators: dict[str, str] = {}
        for name, value in values.items():
            sealed = self.secrets.put(
                tenant_id=tenant_id,
                name=name,
                plaintext=str(value),
                box=self.secret_box,
                secret_metadata={"provider": None},
            )
            locators[name] = sealed.id
        return locators

    def create_credential(
        self,
        payload: ProviderCredentialCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ProviderCredential:
        record = ProviderCredentialRecord(
            id=f"cred_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=payload.project_id,
            provider=payload.provider,
            label=payload.label,
            credential_type=payload.credential_type,
            status=payload.status.value,
            secret_values=self._locators_for(dict(payload.secret_values), tenant_id=tenant_id),
            quota=payload.quota,
            credential_metadata=payload.credential_metadata,
        )
        self.session.add(record)
        self.session.flush()
        return self._to_dto(record)

    def get_credential(self, credential_id: str, *, tenant_id: str | None = None) -> ProviderCredential | None:
        record = self.session.get(ProviderCredentialRecord, credential_id)
        # T97: ``tenant_id`` is the read's scope when given. Internal callers
        # (update/rotate, which already hold the row) omit it and behave as before.
        if record is not None and tenant_id is not None and record.tenant_id != tenant_id:
            return None
        return self._to_dto(record) if record else None

    def get_credential_record(self, credential_id: str) -> ProviderCredentialRecord | None:
        return self.session.get(ProviderCredentialRecord, credential_id)

    def list_credentials(
        self,
        *,
        provider: str | None = None,
        status: ProviderCredentialStatus | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[ProviderCredential]:
        statement = (
            select(ProviderCredentialRecord)
            .where(ProviderCredentialRecord.tenant_id == tenant_id)
            .order_by(
                ProviderCredentialRecord.provider,
                ProviderCredentialRecord.project_id,
                ProviderCredentialRecord.created_at,
                ProviderCredentialRecord.id,
            )
        )
        if provider is not None:
            statement = statement.where(ProviderCredentialRecord.provider == provider)
        if status is not None:
            statement = statement.where(ProviderCredentialRecord.status == status.value)
        return [self._to_dto(record) for record in self.session.scalars(statement)]

    def update_credential(
        self,
        credential_id: str,
        payload: ProviderCredentialUpdate,
    ) -> ProviderCredential | None:
        record = self.session.get(ProviderCredentialRecord, credential_id)
        if record is None:
            return None
        update_data = payload.model_dump(exclude_unset=True)
        if "label" in update_data:
            record.label = payload.label
        if "project_id" in update_data:
            record.project_id = payload.project_id
        if payload.status is not None:
            record.status = payload.status.value
        if payload.secret_values is not None:
            self._retire_locators(record)
            record.secret_values = self._locators_for(
                dict(payload.secret_values), tenant_id=record.tenant_id
            )
        if payload.quota is not None:
            record.quota = payload.quota
        if payload.credential_metadata is not None:
            record.credential_metadata = payload.credential_metadata
        self.session.flush()
        return self._to_dto(record)

    def _retire_locators(self, record: ProviderCredentialRecord) -> None:
        """Drop vault rows this credential is about to stop referencing.

        Without this a replaced secret would linger as an unreferenced ciphertext
        — invisible through the API, and still one more copy of the plaintext on
        disk than there needs to be.
        """
        for value in (record.secret_values or {}).values():
            if is_managed_secret_ref(value):
                self.secrets.delete(str(value))

    def check_credential(
        self,
        credential_id: str,
        *,
        tool_statuses: list[dict],
        missing_dependencies: list[str],
        ok: bool,
        message: str,
    ) -> ProviderCredentialCheckResult | None:
        record = self.session.get(ProviderCredentialRecord, credential_id)
        if record is None:
            return None
        record.last_check_status = "available" if ok else "unavailable"
        record.last_check_message = message
        record.last_checked_at = datetime.now(UTC).replace(tzinfo=None)
        self.session.flush()
        self.session.refresh(record)
        return ProviderCredentialCheckResult(
            credential=self._to_dto(record),
            ok=ok,
            provider=record.provider,
            message=message,
            env_var_names=sorted((record.secret_values or {}).keys()),
            missing_dependencies=missing_dependencies,
            tool_statuses=tool_statuses,
        )

    def rotate_credential(self, credential_id: str) -> ProviderCredentialRotation | None:
        """Move inline secrets into the vault and re-seal what is already there.

        One endpoint for two runbooks — "we just enabled encryption" and "we just
        rotated the key" — because both reduce to the same operation. Idempotent:
        a second call re-seals already-vaulted values and seals nothing new.
        """
        record = self.session.get(ProviderCredentialRecord, credential_id)
        if record is None:
            return None
        if not self.secret_box.enabled:
            raise SecretEncryptionError(
                "secret encryption is not configured; set SECRET_ENCRYPTION_KEY"
            )
        values = dict(record.secret_values or {})
        sealed_count = 0
        resealed_count = 0
        locked_count = 0
        summaries: list[EncryptedSecretSummary] = []
        for name, value in values.items():
            if is_managed_secret_ref(value):
                existing = self.secrets.get(str(value))
                if existing is None:
                    locked_count += 1
                    continue
                self.secrets.reseal(
                    existing,
                    box=self.secret_box,
                    tenant_id=record.tenant_id,
                    name=name,
                )
                resealed_count += 1
                summaries.append(_encrypted_secret_summary(existing))
            else:
                created = self.secrets.put(
                    tenant_id=record.tenant_id,
                    name=name,
                    plaintext=str(value),
                    box=self.secret_box,
                )
                values[name] = created.id
                sealed_count += 1
                summaries.append(_encrypted_secret_summary(created))
        record.secret_values = values
        self.session.flush()
        self.session.refresh(record)
        return ProviderCredentialRotation(
            credential=self._to_dto(record),
            sealed=sealed_count,
            resealed=resealed_count,
            locked=locked_count,
            secrets=summaries,
        )

    def environment_overrides(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        project_id: str | None = None,
    ) -> dict[str, str]:
        """Plaintext for the tool registry, resolved from whichever store holds it.

        A value that cannot be opened is *omitted* rather than passed through as
        its reference: handing ``sec_abc123`` to a provider would look like a
        configured-but-wrong key, while omitting it looks like what it is —
        a credential that is currently unusable.
        """
        env: dict[str, str] = {}
        for record in self._active_records(tenant_id=tenant_id, project_id=project_id):
            env.update(self._plaintext_values(record)[0])
        return env

    def secrets_status(
        self,
        credential_id: str,
    ) -> tuple[ProviderSecretSource, dict[str, str]]:
        """Where this credential's values live, and the ones that are readable."""
        record = self.session.get(ProviderCredentialRecord, credential_id)
        if record is None:
            return ProviderSecretSource.NONE, {}
        resolved, locked = self._plaintext_values(record)
        return self._secret_source(record, resolved=resolved, locked=locked), resolved

    def _plaintext_values(
        self,
        record: ProviderCredentialRecord,
    ) -> tuple[dict[str, str], int]:
        values = record.secret_values or {}
        resolved: dict[str, str] = {}
        locked = 0
        for name, value in values.items():
            if is_managed_secret_ref(value):
                try:
                    resolved[name] = self.secrets.open(
                        str(value),
                        tenant_id=record.tenant_id,
                        name=name,
                        box=self.secret_box,
                    )
                except SecretEncryptionError:
                    locked += 1
            elif isinstance(value, str) and value:
                resolved[name] = value
            else:
                locked += 1
        return resolved, locked

    def _secret_source(
        self,
        record: ProviderCredentialRecord,
        *,
        resolved: dict[str, str],
        locked: int,
    ) -> ProviderSecretSource:
        values = record.secret_values or {}
        if not values:
            return ProviderSecretSource.NONE
        if locked:
            # Anything unreadable outranks the mix: the credential is not usable
            # as stored, and "locked" is the only label that says so.
            return ProviderSecretSource.LOCKED
        managed = sum(1 for value in values.values() if is_managed_secret_ref(value))
        if managed == len(values):
            return ProviderSecretSource.VAULT
        if managed == 0:
            return ProviderSecretSource.INLINE
        return ProviderSecretSource.MIXED

    def _to_dto(self, record: ProviderCredentialRecord) -> ProviderCredential:
        resolved, locked = self._plaintext_values(record)
        source = self._secret_source(record, resolved=resolved, locked=locked)
        return provider_credential_from_record(
            record,
            resolved=resolved,
            source=source,
        )

    def disabled_providers(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        project_id: str | None = None,
    ) -> set[str]:
        records = self._records_for_scope(tenant_id=tenant_id, project_id=project_id)
        active = {
            record.provider
            for record in records
            if record.status == ProviderCredentialStatus.ACTIVE.value
        }
        disabled = {
            record.provider
            for record in records
            if record.status == ProviderCredentialStatus.DISABLED.value
        }
        return disabled - active

    def _active_records(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
    ) -> list[ProviderCredentialRecord]:
        return [
            record
            for record in self._records_for_scope(tenant_id=tenant_id, project_id=project_id)
            if record.status == ProviderCredentialStatus.ACTIVE.value
        ]

    def _records_for_scope(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
    ) -> list[ProviderCredentialRecord]:
        statement = (
            select(ProviderCredentialRecord)
            .where(ProviderCredentialRecord.tenant_id == tenant_id)
            .where(
                or_(
                    ProviderCredentialRecord.project_id.is_(None),
                    ProviderCredentialRecord.project_id == project_id,
                )
            )
            .order_by(
                ProviderCredentialRecord.project_id.is_(None).desc(),
                ProviderCredentialRecord.updated_at,
                ProviderCredentialRecord.created_at,
            )
        )
        return list(self.session.scalars(statement))


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_tenant(
        self,
        tenant_id: str = DEFAULT_TENANT_ID,
        name: str = DEFAULT_TENANT_NAME,
    ) -> TenantRecord:
        tenant = self.session.get(TenantRecord, tenant_id)
        if tenant is not None:
            return tenant
        tenant = TenantRecord(id=tenant_id, name=name, plan="internal", status="active")
        self.session.add(tenant)
        self.session.flush()
        return tenant

    def create_project(
        self,
        payload: ProjectCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        created_by: str | None = None,
        team_id: str | None = None,
    ) -> Project:
        self.ensure_tenant(tenant_id)
        record = ProjectRecord(
            id=f"project_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            created_by=created_by,
            title=payload.title,
            description=payload.description,
            default_language=payload.default_language,
            default_aspect_ratio=payload.default_aspect_ratio,
            # V8.3: attribution only, and an explicit argument rather than a DTO
            # field. ``None`` is a first-class value meaning "the default team",
            # so an ordinary create stays unqualified instead of silently
            # binding the project to whichever team happens to be default at
            # the moment of the call. It is deliberately *not* settable from the
            # create request: attribution decides which team's ceiling applies,
            # so it must be moved by ``quota:manage`` (see
            # ``DatabaseStore.assign_project_team``), never by whoever can make
            # a project.
            team_id=team_id,
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        return project_from_record(record)

    def assign_team(self, project_id: str, team_id: str | None) -> Project | None:
        """Point a project at a team, or at none. ``None`` = the team does not exist."""
        record = self.session.get(ProjectRecord, project_id)
        if record is None:
            return None
        record.team_id = team_id
        self.session.flush()
        return project_from_record(record)

    def get_project(self, project_id: str) -> Project | None:
        record = self.session.get(ProjectRecord, project_id)
        return project_from_record(record) if record else None

    def list_projects(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> list[Project]:
        # T97: a tenant column the list used to ignore. Every other tenant-scoped
        # list already filters; this one predated multi-tenancy and leaked sibling
        # tenants' project rows to anyone who could reach the endpoint at all.
        statement = (
            select(ProjectRecord)
            .where(ProjectRecord.tenant_id == tenant_id)
            .order_by(ProjectRecord.updated_at.desc(), ProjectRecord.id)
        )
        return [project_from_record(record) for record in self.session.scalars(statement)]


class UserRepository:
    """Registers the human identities the review system points at.

    Reviewer ids arrive from whatever identity provider a deployment uses, so
    the first time an id is written to ``assignee_user_id`` / ``actor_user_id`` /
    ``author_user_id`` a row has to exist in ``users``: MySQL enforces that
    foreign key, while SQLite ignores it unless ``PRAGMA foreign_keys=ON``. That
    asymmetry is exactly how a missing row can pass local checks and then fail
    with IntegrityError 1452 on a real deployment.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_user(
        self,
        user_id: str | None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        display_name: str | None = None,
        email: str | None = None,
        role: str = "reviewer",
    ) -> str | None:
        """Return ``user_id`` once a ``users`` row is guaranteed to back it."""
        if not user_id:
            return None
        record = self.session.get(UserRecord, user_id)
        if record is not None:
            if display_name and record.name != display_name:
                # A later call knows the human behind the id; keep the roster
                # label in step with it.
                record.name = display_name[:160]
            if email and record.email != email:
                record.email = email[:255]
            return record.id
        ProjectRepository(self.session).ensure_tenant(tenant_id)
        record = UserRecord(
            id=user_id,
            tenant_id=tenant_id,
            email=(email or f"{user_id}@reviewer.invalid")[:255],
            name=(display_name or user_id)[:160],
            role=role[:64],
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        return record.id


class TeamMembershipOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    TEAM_NOT_FOUND = "team_not_found"
    INVALID_ROLE = "invalid_role"


class ProjectMembershipOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    PROJECT_NOT_FOUND = "project_not_found"
    INVALID_ROLE = "invalid_role"


class ProjectTeamOutcome(StrEnum):
    """What came of pointing a project at a team (V8.3)."""

    ASSIGNED = "assigned"
    CLEARED = "cleared"
    PROJECT_NOT_FOUND = "project_not_found"
    TEAM_NOT_FOUND = "team_not_found"


# Highest role wins when a user somehow holds more than one team membership in a
# tenant; the union of the roles would be wrong (a viewer seat must not widen an
# admin seat into team:manage).
_TEAM_ROLE_RANK: dict[TeamRole, int] = {
    TeamRole.OWNER: 3,
    TeamRole.ADMIN: 2,
    TeamRole.MEMBER: 1,
    TeamRole.VIEWER: 0,
}


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned[:120] or "team"


def team_from_record(record: TeamRecord, *, member_count: int = 0) -> Team:
    return Team(
        team_id=record.id,
        tenant_id=record.tenant_id,
        name=record.name,
        slug=record.slug,
        description=record.description,
        is_default=record.is_default,
        status=record.status,
        member_count=member_count,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def team_quota_from_record(
    record: TeamQuotaRecord | None,
    *,
    tenant_id: str,
    team_id: str,
    usage: QuotaUsage,
    team_name: str | None = None,
    is_default_team: bool = False,
) -> TeamQuota:
    """The quota row (or its absence) plus the window usage, as one DTO.

    A missing row and a paused row read differently on purpose: ``configured``
    says whether a ceiling exists at all, while ``usage.status`` says whether it
    is *in force right now*. Collapsing the two would let a paused quota look
    like a passed check.
    """

    period = (
        TeamQuotaPeriod(record.period) if record is not None else TeamQuotaPeriod.CALENDAR_MONTH
    )
    enforcement = (
        TeamQuotaEnforcement(record.enforcement)
        if record is not None
        else TeamQuotaEnforcement.HARD
    )
    limit_cost = (
        float(record.limit_cost)
        if record is not None and record.limit_cost is not None
        else None
    )
    limit_runs = (
        int(record.limit_runs)
        if record is not None and record.limit_runs is not None
        else None
    )
    limit_tokens = (
        int(record.limit_tokens)
        if record is not None and record.limit_tokens is not None
        else None
    )
    verdict = evaluate_quota(
        period=period,
        usage=usage,
        limit_cost=limit_cost,
        limit_runs=limit_runs,
        limit_tokens=limit_tokens,
        enforcement=enforcement,
    )
    paused = record is not None and record.status != "active"
    return TeamQuota(
        quota_id=record.id if record is not None else None,
        tenant_id=tenant_id,
        team_id=team_id,
        team_name=team_name,
        is_default_team=is_default_team,
        period=period,
        limit_cost=limit_cost,
        limit_runs=limit_runs,
        limit_tokens=limit_tokens,
        enforcement=enforcement,
        status=record.status if record is not None else "active",
        configured=verdict.configured and not paused,
        created_by=record.created_by if record is not None else None,
        note=record.note if record is not None else None,
        usage=TeamQuotaUsage(
            period=period,
            window_start=usage.window.start,
            window_end=usage.window.end,
            estimated_cost=usage.estimated_cost,
            actual_cost=usage.actual_cost,
            spent_cost=usage.spent_cost,
            run_count=usage.run_count,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cost_remaining=(
                round(limit_cost - usage.spent_cost, 6) if limit_cost is not None else None
            ),
            runs_remaining=(
                max(0, limit_runs - usage.run_count) if limit_runs is not None else None
            ),
            tokens_remaining=(
                max(0, limit_tokens - usage.total_tokens)
                if limit_tokens is not None
                else None
            ),
            status=STATUS_PAUSED if paused else verdict.status,
            exceeded_dimensions=[] if paused else list(verdict.exceeded),
        ),
        created_at=record.created_at if record is not None else None,
        updated_at=record.updated_at if record is not None else None,
    )


def team_membership_from_record(
    record: TeamMembershipRecord,
    *,
    user: UserRecord | None = None,
) -> TeamMembership:
    team_role = coerce_team_role(record.member_role)
    return TeamMembership(
        membership_id=record.id,
        team_id=record.team_id,
        user_id=record.user_id,
        role=record.member_role,
        status=record.status,
        user_name=user.name if user else None,
        user_email=user.email if user else None,
        invited_by=record.invited_by,
        note=record.note,
        permissions=sorted(
            permission.value for permission in effective_permissions(team_role)
        ),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def project_membership_from_record(
    record: ProjectMembershipRecord,
    *,
    user: UserRecord | None = None,
) -> ProjectMembership:
    return ProjectMembership(
        membership_id=record.id,
        project_id=record.project_id,
        user_id=record.user_id,
        role=record.member_role,
        status=record.status,
        user_name=user.name if user else None,
        user_email=user.email if user else None,
        invited_by=record.invited_by,
        note=record.note,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def audit_log_from_record(record: AuditLogRecord) -> AuditLogEntry:
    return AuditLogEntry(
        audit_id=record.id,
        tenant_id=record.tenant_id,
        actor_id=record.actor_id,
        action=record.action,
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        project_id=record.project_id,
        summary=record.summary,
        payload=record.payload or {},
        created_at=record.created_at,
    )


class TeamRepository:
    """Teams, team memberships and per-project memberships.

    Both membership tables use ``member_role`` on disk (``role`` is reserved on
    MySQL 8) and expose it as ``role`` through the API, matching the asset
    library's ``usage_role`` precedent.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- teams ---------------------------------------------------------------

    def ensure_team(
        self,
        *,
        team_id: str | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
        name: str | None = None,
        slug: str | None = None,
        is_default: bool = False,
        created_by: str | None = None,
    ) -> TeamRecord:
        if team_id is not None:
            existing = self.session.get(TeamRecord, team_id)
            if existing is not None:
                return existing
        project_repository = ProjectRepository(self.session)
        project_repository.ensure_tenant(tenant_id)
        resolved_slug = slug or slugify(name or (slug or "team"))
        record = TeamRecord(
            id=team_id or f"team_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=(name or resolved_slug)[:160],
            slug=self._unique_slug(tenant_id, resolved_slug),
            description=None,
            created_by=created_by,
            is_default=is_default,
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        return record

    def ensure_default_team(self, actor_id: str | None = None) -> TeamRecord:
        """The implicit team of the default tenant; created on first use."""
        tenant_id = DEFAULT_TENANT_ID
        existing = self.session.scalar(
            select(TeamRecord).where(
                TeamRecord.tenant_id == tenant_id,
                TeamRecord.is_default.is_(True),
            )
        )
        if existing is not None:
            return existing
        return self.ensure_team(
            tenant_id=tenant_id,
            name=settings.default_team_name,
            slug=settings.default_team_slug,
            is_default=True,
            created_by=actor_id,
        )

    def default_team_id(self, tenant_id: str = DEFAULT_TENANT_ID) -> str | None:
        """The default team's id, or ``None`` before anyone has been seated.

        Read-only, unlike :meth:`ensure_default_team`: attributing a project or
        listing quotas must not be what first materialises a team, or merely
        asking a question would change the answer.
        """
        return self.session.scalar(
            select(TeamRecord.id).where(
                TeamRecord.tenant_id == tenant_id,
                TeamRecord.is_default.is_(True),
            )
        )

    def _unique_slug(self, tenant_id: str, base: str, *, exclude_id: str | None = None) -> str:
        candidate = base
        suffix = 2
        while True:
            statement = select(TeamRecord.id).where(
                TeamRecord.tenant_id == tenant_id,
                TeamRecord.slug == candidate,
            )
            if exclude_id is not None:
                statement = statement.where(TeamRecord.id != exclude_id)
            if self.session.scalar(statement) is None:
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1

    def get_team(self, team_id: str) -> Team | None:
        record = self.session.get(TeamRecord, team_id)
        if record is None:
            return None
        return team_from_record(record, member_count=self._member_count(team_id))

    def list_teams(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        include_archived: bool = False,
    ) -> list[Team]:
        statement = select(TeamRecord).where(TeamRecord.tenant_id == tenant_id)
        if not include_archived:
            statement = statement.where(TeamRecord.status == "active")
        statement = statement.order_by(TeamRecord.is_default.desc(), TeamRecord.created_at)
        records = list(self.session.scalars(statement))
        counts = self._member_counts([record.id for record in records])
        return [
            team_from_record(record, member_count=counts.get(record.id, 0))
            for record in records
        ]

    def create_team(
        self,
        payload: TeamCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        created_by: str | None = None,
    ) -> Team:
        ProjectRepository(self.session).ensure_tenant(tenant_id)
        if payload.slug:
            slug = slugify(payload.slug)
            existing = self.session.scalar(
                select(TeamRecord.id).where(
                    TeamRecord.tenant_id == tenant_id,
                    TeamRecord.slug == slug,
                )
            )
            if existing is not None:
                raise ValueError(f"team slug '{slug}' already exists in this tenant")
        else:
            slug = self._unique_slug(tenant_id, slugify(payload.name))
        record = TeamRecord(
            id=f"team_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=payload.name,
            slug=slug,
            description=payload.description,
            created_by=created_by,
            is_default=False,
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        return team_from_record(record, member_count=0)

    def update_team(self, team_id: str, payload: TeamUpdate) -> Team | None:
        record = self.session.get(TeamRecord, team_id)
        if record is None:
            return None
        if payload.name is not None:
            record.name = payload.name
        if payload.description is not None:
            record.description = payload.description
        if payload.status is not None:
            record.status = payload.status
        self.session.flush()
        return team_from_record(record, member_count=self._member_count(team_id))

    def _member_count(self, team_id: str) -> int:
        return self._member_counts([team_id]).get(team_id, 0)

    def _member_counts(self, team_ids: list[str]) -> dict[str, int]:
        if not team_ids:
            return {}
        statement = (
            select(TeamMembershipRecord.team_id, func.count())
            .where(
                TeamMembershipRecord.team_id.in_(team_ids),
                TeamMembershipRecord.status == "active",
            )
            .group_by(TeamMembershipRecord.team_id)
        )
        return {team_id: count for team_id, count in self.session.execute(statement)}

    # --- team memberships ----------------------------------------------------

    def list_members(
        self,
        team_id: str,
        *,
        include_removed: bool = False,
    ) -> list[TeamMembership]:
        statement = select(TeamMembershipRecord).where(TeamMembershipRecord.team_id == team_id)
        if not include_removed:
            statement = statement.where(TeamMembershipRecord.status == "active")
        statement = statement.order_by(TeamMembershipRecord.created_at, TeamMembershipRecord.id)
        records = list(self.session.scalars(statement))
        users = self._users_by_id([record.user_id for record in records])
        return [team_membership_from_record(record, user=users.get(record.user_id)) for record in records]

    def get_membership(self, team_id: str, user_id: str) -> TeamMembershipRecord | None:
        return self.session.scalar(
            select(TeamMembershipRecord).where(
                TeamMembershipRecord.team_id == team_id,
                TeamMembershipRecord.user_id == user_id,
            )
        )

    def find_membership(self, membership_id: str) -> TeamMembershipRecord | None:
        return self.session.get(TeamMembershipRecord, membership_id)

    def add_member(
        self,
        team_id: str,
        payload: TeamMemberAdd,
        *,
        invited_by: str | None = None,
    ) -> tuple[TeamMembershipOutcome, TeamMembership | None]:
        team = self.session.get(TeamRecord, team_id)
        if team is None:
            return TeamMembershipOutcome.TEAM_NOT_FOUND, None
        if coerce_team_role(payload.role) is None:
            return TeamMembershipOutcome.INVALID_ROLE, None
        UserRepository(self.session).ensure_user(
            payload.user_id,
            tenant_id=team.tenant_id,
            display_name=payload.name,
            email=payload.email,
            role=payload.role,
        )
        existing = self.get_membership(team_id, payload.user_id)
        if existing is not None:
            # Idempotent: re-adding refreshes the role/note instead of duplicating.
            existing.member_role = payload.role
            existing.status = "active"
            if payload.note is not None:
                existing.note = payload.note
            self.session.flush()
            user = self.session.get(UserRecord, existing.user_id)
            return TeamMembershipOutcome.UPDATED, team_membership_from_record(existing, user=user)
        record = TeamMembershipRecord(
            id=f"tmem_{uuid4().hex[:12]}",
            tenant_id=team.tenant_id,
            team_id=team_id,
            user_id=payload.user_id,
            member_role=payload.role,
            invited_by=invited_by,
            note=payload.note,
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return TeamMembershipOutcome.CREATED, team_membership_from_record(record, user=user)

    def update_member(
        self,
        membership_id: str,
        payload: TeamMemberUpdate,
    ) -> TeamMembership | None:
        record = self.session.get(TeamMembershipRecord, membership_id)
        if record is None:
            return None
        if payload.role is not None:
            if coerce_team_role(payload.role) is None:
                return None
            record.member_role = payload.role
        if payload.status is not None:
            record.status = payload.status
        if payload.note is not None:
            record.note = payload.note
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return team_membership_from_record(record, user=user)

    def remove_member(self, membership_id: str) -> TeamMembership | None:
        record = self.session.get(TeamMembershipRecord, membership_id)
        if record is None:
            return None
        record.status = "removed"
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return team_membership_from_record(record, user=user)

    def team_role_for_user(
        self,
        user_id: str | None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[str, TeamRole] | None:
        if not user_id:
            return None
        statement = (
            select(TeamMembershipRecord)
            .join(TeamRecord, TeamRecord.id == TeamMembershipRecord.team_id)
            .where(
                TeamMembershipRecord.user_id == user_id,
                TeamMembershipRecord.status == "active",
                TeamRecord.tenant_id == tenant_id,
                TeamRecord.status == "active",
            )
        )
        best: tuple[str, TeamRole] | None = None
        for record in self.session.scalars(statement):
            role = coerce_team_role(record.member_role)
            if role is None:
                continue
            if best is None or _TEAM_ROLE_RANK[role] > _TEAM_ROLE_RANK[best[1]]:
                best = (record.team_id, role)
        return best

    # --- project memberships -------------------------------------------------

    def list_project_members(
        self,
        project_id: str,
        *,
        include_removed: bool = False,
    ) -> list[ProjectMembership]:
        statement = select(ProjectMembershipRecord).where(
            ProjectMembershipRecord.project_id == project_id
        )
        if not include_removed:
            statement = statement.where(ProjectMembershipRecord.status == "active")
        statement = statement.order_by(
            ProjectMembershipRecord.created_at,
            ProjectMembershipRecord.id,
        )
        records = list(self.session.scalars(statement))
        users = self._users_by_id([record.user_id for record in records])
        return [
            project_membership_from_record(record, user=users.get(record.user_id))
            for record in records
        ]

    def find_project_membership(self, membership_id: str) -> ProjectMembershipRecord | None:
        return self.session.get(ProjectMembershipRecord, membership_id)

    def get_project_membership(
        self,
        project_id: str,
        user_id: str,
    ) -> ProjectMembershipRecord | None:
        return self.session.scalar(
            select(ProjectMembershipRecord).where(
                ProjectMembershipRecord.project_id == project_id,
                ProjectMembershipRecord.user_id == user_id,
            )
        )

    def add_project_member(
        self,
        project_id: str,
        payload: ProjectMemberAdd,
        *,
        invited_by: str | None = None,
    ) -> tuple[ProjectMembershipOutcome, ProjectMembership | None]:
        project = self.session.get(ProjectRecord, project_id)
        if project is None:
            return ProjectMembershipOutcome.PROJECT_NOT_FOUND, None
        if coerce_project_role(payload.role) is None:
            return ProjectMembershipOutcome.INVALID_ROLE, None
        UserRepository(self.session).ensure_user(
            payload.user_id,
            tenant_id=project.tenant_id,
            display_name=payload.name,
            email=payload.email,
            role=payload.role,
        )
        existing = self.get_project_membership(project_id, payload.user_id)
        if existing is not None:
            existing.member_role = payload.role
            existing.status = "active"
            if payload.note is not None:
                existing.note = payload.note
            self.session.flush()
            user = self.session.get(UserRecord, existing.user_id)
            return ProjectMembershipOutcome.UPDATED, project_membership_from_record(
                existing, user=user
            )
        record = ProjectMembershipRecord(
            id=f"pmem_{uuid4().hex[:12]}",
            tenant_id=project.tenant_id,
            project_id=project_id,
            user_id=payload.user_id,
            member_role=payload.role,
            invited_by=invited_by,
            note=payload.note,
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return ProjectMembershipOutcome.CREATED, project_membership_from_record(
            record, user=user
        )

    def update_project_member(
        self,
        membership_id: str,
        payload: ProjectMemberUpdate,
    ) -> ProjectMembership | None:
        record = self.session.get(ProjectMembershipRecord, membership_id)
        if record is None:
            return None
        if payload.role is not None:
            if coerce_project_role(payload.role) is None:
                return None
            record.member_role = payload.role
        if payload.status is not None:
            record.status = payload.status
        if payload.note is not None:
            record.note = payload.note
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return project_membership_from_record(record, user=user)

    def remove_project_member(self, membership_id: str) -> ProjectMembership | None:
        record = self.session.get(ProjectMembershipRecord, membership_id)
        if record is None:
            return None
        record.status = "removed"
        self.session.flush()
        user = self.session.get(UserRecord, record.user_id)
        return project_membership_from_record(record, user=user)

    def project_role_for_user(self, project_id: str, user_id: str | None) -> ProjectRole | None:
        if not user_id:
            return None
        record = self.get_project_membership(project_id, user_id)
        if record is None or record.status != "active":
            return None
        return coerce_project_role(record.member_role)

    def _users_by_id(self, user_ids: list[str]) -> dict[str, UserRecord]:
        unique = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
        if not unique:
            return {}
        statement = select(UserRecord).where(UserRecord.id.in_(unique))
        return {record.id: record for record in self.session.scalars(statement)}


class TeamQuotaRepository:
    """Team spend ceilings and the attribution that makes them answerable (V8.3).

    The attribution edge is ``usage_ledger_entries.run_id -> production_runs
    .project_id -> projects.team_id``. A ``NULL`` project team means *the default
    team*, so a team that happens to be default also owns the rows created before
    anyone was assigned — without a backfill and without a second meaning for
    ``NULL`` than the one the actor layer already gives it.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- the quota row -------------------------------------------------------

    def get(self, team_id: str) -> TeamQuotaRecord | None:
        return self.session.scalar(
            select(TeamQuotaRecord).where(TeamQuotaRecord.team_id == team_id)
        )

    def list_for_tenant(self, tenant_id: str) -> list[TeamQuotaRecord]:
        statement = (
            select(TeamQuotaRecord)
            .where(TeamQuotaRecord.tenant_id == tenant_id)
            .order_by(TeamQuotaRecord.created_at, TeamQuotaRecord.id)
        )
        return list(self.session.scalars(statement))

    def upsert(
        self,
        team_id: str,
        *,
        tenant_id: str,
        payload: TeamQuotaUpdate,
        created_by: str | None = None,
    ) -> TeamQuotaRecord:
        record = self.get(team_id)
        created = record is None
        if record is None:
            record = TeamQuotaRecord(
                id=f"quota_{uuid4().hex[:12]}",
                tenant_id=tenant_id,
                team_id=team_id,
                created_by=created_by,
            )
            self.session.add(record)
        record.period = TeamQuotaPeriod(payload.period).value
        record.limit_cost = payload.limit_cost
        record.limit_runs = payload.limit_runs
        record.limit_tokens = payload.limit_tokens
        record.enforcement = TeamQuotaEnforcement(payload.enforcement).value
        record.status = payload.status
        if payload.note is not None or created:
            record.note = payload.note
        self.session.flush()
        return record

    def delete(self, team_id: str) -> bool:
        record = self.get(team_id)
        if record is None:
            return False
        self.session.delete(record)
        self.session.flush()
        return True

    # --- attribution ---------------------------------------------------------

    def project_team_id(self, project_id: str) -> str | None:
        return self.session.scalar(
            select(ProjectRecord.team_id).where(ProjectRecord.id == project_id)
        )

    def list_project_ids_for_team(
        self,
        tenant_id: str,
        team_id: str,
        *,
        default_team_id: str | None = None,
    ) -> list[str]:
        statement = select(ProjectRecord.id).where(
            ProjectRecord.tenant_id == tenant_id,
            *self._team_filter(team_id, default_team_id=default_team_id),
        )
        return list(self.session.scalars(statement))

    def usage_for_team(
        self,
        tenant_id: str,
        team_id: str,
        *,
        window: QuotaWindow,
        default_team_id: str | None = None,
    ) -> QuotaUsage:
        """Sum the team's ledger rows and runs inside ``window``.

        ``spent_cost`` follows the V8.2 panel rule per row: a reported
        ``actual_cost`` wins, otherwise the ``estimated_cost`` stands in. Summing
        only actuals would report zero for every in-flight call and let a team
        spend far past a ceiling that looked untouched.
        """
        team_filter = self._team_filter(team_id, default_team_id=default_team_id)
        spent = func.sum(
            case(
                (
                    UsageLedgerRecord.actual_cost.is_not(None),
                    func.coalesce(UsageLedgerRecord.actual_cost, 0),
                ),
                else_=func.coalesce(UsageLedgerRecord.estimated_cost, 0),
            )
        )
        ledger_statement = (
            select(
                spent,
                func.coalesce(func.sum(UsageLedgerRecord.estimated_cost), 0),
                func.coalesce(func.sum(UsageLedgerRecord.actual_cost), 0),
                func.coalesce(func.sum(UsageLedgerRecord.input_tokens), 0),
                func.coalesce(func.sum(UsageLedgerRecord.output_tokens), 0),
            )
            .join(ProductionRunRecord, UsageLedgerRecord.run_id == ProductionRunRecord.id)
            .join(ProjectRecord, ProductionRunRecord.project_id == ProjectRecord.id)
            .where(ProjectRecord.tenant_id == tenant_id, *team_filter)
        )
        ledger_statement = self._within_window(
            ledger_statement, UsageLedgerRecord.created_at, window
        )
        spent_cost, estimated_cost, actual_cost, input_tokens, output_tokens = (
            self.session.execute(ledger_statement).one()
        )

        run_statement = (
            select(func.count())
            .select_from(ProductionRunRecord)
            .join(ProjectRecord, ProductionRunRecord.project_id == ProjectRecord.id)
            .where(ProjectRecord.tenant_id == tenant_id, *team_filter)
        )
        run_statement = self._within_window(
            run_statement, ProductionRunRecord.created_at, window
        )
        run_count = self.session.scalar(run_statement) or 0

        return QuotaUsage(
            window=window,
            estimated_cost=round(float(estimated_cost or 0.0), 6),
            actual_cost=round(float(actual_cost or 0.0), 6),
            spent_cost=round(float(spent_cost or 0.0), 6),
            run_count=int(run_count),
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
        )

    @staticmethod
    def _team_filter(team_id: str, *, default_team_id: str | None) -> list[Any]:
        if default_team_id is not None and team_id == default_team_id:
            return [
                or_(ProjectRecord.team_id == team_id, ProjectRecord.team_id.is_(None))
            ]
        return [ProjectRecord.team_id == team_id]

    @staticmethod
    def _within_window(statement: Any, column: Any, window: QuotaWindow) -> Any:
        if window.start is not None:
            statement = statement.where(column >= window.start)
        return statement.where(column < window.end)


class AuditLogRepository:
    """Append-only audit trail for privileged, tenant-scoped operations."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        actor_id: str | None = None,
        entity_id: str | None = None,
        project_id: str | None = None,
        summary: str | None = None,
        payload: dict | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> AuditLogEntry:
        record = AuditLogRecord(
            id=f"audit_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action[:120],
            entity_type=entity_type[:64],
            entity_id=entity_id,
            project_id=project_id,
            summary=summary,
            payload=payload or {},
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return audit_log_from_record(record)

    def _filtered(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
    ):
        statement = select(AuditLogRecord).where(AuditLogRecord.tenant_id == tenant_id)
        if actor_id is not None:
            statement = statement.where(AuditLogRecord.actor_id == actor_id)
        if action is not None:
            statement = statement.where(AuditLogRecord.action == action)
        if entity_type is not None:
            statement = statement.where(AuditLogRecord.entity_type == entity_type)
        if project_id is not None:
            statement = statement.where(AuditLogRecord.project_id == project_id)
        if entity_id is not None:
            statement = statement.where(AuditLogRecord.entity_id == entity_id)
        return statement

    def list_logs(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> list[AuditLogEntry]:
        statement = self._filtered(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            project_id=project_id,
            entity_id=entity_id,
        )
        statement = _sorted(
            statement,
            AuditLogRecord,
            sort,
            (AuditLogRecord.created_at.desc(), AuditLogRecord.id.desc()),
        )
        statement = statement.limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [audit_log_from_record(record) for record in self.session.scalars(statement)]

    def count_logs(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        project_id: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Total rows matching the same filters, for the ``X-Total-Count`` header."""
        statement = self._filtered(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            project_id=project_id,
            entity_id=entity_id,
        )
        return int(self.session.scalar(select(func.count()).select_from(statement.subquery())) or 0)


class IdempotencyOutcome(StrEnum):
    CLAIMED = "claimed"
    REPLAYED = "replayed"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    IN_PROGRESS = "in_progress"


@dataclass
class IdempotencyClaim:
    """What the store decided about an incoming ``Idempotency-Key``."""

    outcome: IdempotencyOutcome
    record_id: str | None = None
    status_code: int | None = None
    response_body: Any = None


def idempotency_key_from_record(record: IdempotencyKeyRecord) -> IdempotencyKeyEntry:
    return IdempotencyKeyEntry(
        idempotency_key=record.idempotency_key,
        actor_id=record.actor_id,
        method=record.method,
        path=record.path,
        state=record.state,
        status_code=record.status_code,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


class IdempotencyRepository:
    """Claim / replay bookkeeping for the ``Idempotency-Key`` header.

    The row is written *before* the request runs, so a concurrent duplicate sees
    an ``in_progress`` claim rather than starting the work a second time. A claim
    that is never completed (the process died, the request 4xx'd) is released or,
    once past its TTL, taken over by the next caller.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> IdempotencyKeyRecord | None:
        statement = select(IdempotencyKeyRecord).where(
            IdempotencyKeyRecord.tenant_id == tenant_id,
            IdempotencyKeyRecord.actor_id == actor_id,
            IdempotencyKeyRecord.idempotency_key == idempotency_key,
        )
        return self.session.scalar(statement)

    def claim(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        idempotency_key: str,
        method: str,
        path: str,
        fingerprint: str,
        ttl_seconds: int,
    ) -> IdempotencyClaim:
        now = _utcnow()
        expires_at = now + timedelta(seconds=max(60, ttl_seconds))
        record = self.find(
            tenant_id=tenant_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        if record is not None:
            # Expiry is checked *before* the fingerprint. An expired row is dead
            # bookkeeping, and a key must never be poisoned forever by whatever
            # it was first used for — so a stale row is taken over no matter what
            # body arrives with it. Only a row that is still live can conflict.
            if record.expires_at is not None and as_utc(record.expires_at) <= now:
                record.method = method
                record.path = path[:512]
                record.request_fingerprint = fingerprint
                record.state = "in_progress"
                record.status_code = None
                record.response_body = None
                record.expires_at = expires_at
                self.session.flush()
                return IdempotencyClaim(IdempotencyOutcome.CLAIMED, record_id=record.id)
            if record.request_fingerprint != fingerprint:
                return IdempotencyClaim(IdempotencyOutcome.FINGERPRINT_MISMATCH)
            if record.state == "completed":
                return IdempotencyClaim(
                    IdempotencyOutcome.REPLAYED,
                    record_id=record.id,
                    status_code=record.status_code,
                    response_body=record.response_body,
                )
            # Live and unfinished: a concurrent duplicate is still running.
            return IdempotencyClaim(IdempotencyOutcome.IN_PROGRESS)

        record = IdempotencyKeyRecord(
            id=f"idem_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key[:255],
            method=method,
            path=path[:512],
            request_fingerprint=fingerprint,
            state="in_progress",
            status_code=None,
            response_body=None,
            expires_at=expires_at,
        )
        self.session.add(record)
        self.session.flush()
        return IdempotencyClaim(IdempotencyOutcome.CLAIMED, record_id=record.id)

    def complete(
        self,
        record_id: str,
        *,
        status_code: int,
        response_body: Any,
    ) -> None:
        record = self.session.get(IdempotencyKeyRecord, record_id)
        if record is None:
            return
        record.state = "completed"
        record.status_code = status_code
        record.response_body = response_body if isinstance(response_body, dict) else None
        self.session.flush()

    def release(self, record_id: str) -> None:
        """Drop a claim whose request did not produce a replayable response."""
        record = self.session.get(IdempotencyKeyRecord, record_id)
        if record is None:
            return
        if record.state == "in_progress":
            self.session.delete(record)
            self.session.flush()

    def purge_expired(self, *, now: datetime | None = None) -> int:
        moment = now or _utcnow()
        statement = select(IdempotencyKeyRecord).where(
            IdempotencyKeyRecord.expires_at.is_not(None),
            IdempotencyKeyRecord.expires_at < moment,
        )
        records = list(self.session.scalars(statement))
        for record in records:
            self.session.delete(record)
        self.session.flush()
        return len(records)

    def list_entries(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        state: str | None = None,
        actor_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[IdempotencyKeyEntry], int]:
        filters = [IdempotencyKeyRecord.tenant_id == tenant_id]
        if state is not None:
            filters.append(IdempotencyKeyRecord.state == state)
        if actor_id is not None:
            filters.append(IdempotencyKeyRecord.actor_id == actor_id)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(
                    select(IdempotencyKeyRecord).where(*filters).subquery()
                )
            )
            or 0
        )
        statement = (
            select(IdempotencyKeyRecord)
            .where(*filters)
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        statement = _sorted(
            statement,
            IdempotencyKeyRecord,
            sort,
            (IdempotencyKeyRecord.created_at.desc(), IdempotencyKeyRecord.id.desc()),
        )
        entries = [idempotency_key_from_record(record) for record in self.session.scalars(statement)]
        return entries, total


class ApiKeyOutcome(StrEnum):
    CREATED = "created"
    INVALID_SCOPE = "invalid_scope"
    SCOPE_EXCEEDS_OWNER = "scope_exceeds_owner"
    NOT_FOUND = "not_found"


@dataclass
class ApiKeyIssued:
    """The result of issuing a key: the record plus the one-time plaintext."""

    outcome: ApiKeyOutcome
    api_key: ApiKey | None = None
    plaintext: str | None = None
    detail: str | None = None


@dataclass
class ApiKeyRevocation:
    """The result of revoking a key.

    ``changed`` distinguishes the revocation that did the work from a repeat of
    it. Revoking is idempotent either way, but the audit trail should record the
    act once — otherwise "who revoked what" becomes a count of retries.
    """

    api_key: ApiKey | None = None
    changed: bool = False


#: ``s2v_<12 hex>_<48 hex>``. Hex on purpose: the urlsafe base64 alphabet contains
#: ``_``, which would make the credential ambiguous to split into its three parts.
API_KEY_SCHEME = "s2v"
API_KEY_PREFIX_BYTES = 6
API_KEY_SECRET_BYTES = 24


def generate_api_key() -> tuple[str, str]:
    """Return ``(key_prefix, plaintext)`` for a fresh credential."""
    prefix = f"{API_KEY_SCHEME}_{token_hex(API_KEY_PREFIX_BYTES)}"
    plaintext = f"{prefix}_{token_hex(API_KEY_SECRET_BYTES)}"
    return prefix, plaintext


def api_key_prefix_of(plaintext: str) -> str | None:
    """Recover the lookup handle from a presented credential.

    Returns ``None`` for anything that is not shaped like a key this service
    issues, so a malformed ``Authorization`` header is rejected without a query.
    """
    parts = plaintext.strip().split("_")
    if len(parts) != 3 or parts[0] != API_KEY_SCHEME or not parts[1] or not parts[2]:
        return None
    return f"{API_KEY_SCHEME}_{parts[1]}"


def hash_api_key(plaintext: str) -> str:
    """Salted SHA-256 of the credential; only this value is persisted."""
    pepper = settings.api_key_pepper or ""
    return sha256(f"{pepper}{plaintext}".encode()).hexdigest()


def api_key_from_record(record: ApiKeyRecord) -> ApiKey:
    return ApiKey(
        api_key_id=record.id,
        tenant_id=record.tenant_id,
        actor_id=record.actor_id,
        name=record.name,
        key_prefix=record.key_prefix,
        scopes=list(record.scopes) if record.scopes is not None else None,
        status=record.status,
        created_by=record.created_by,
        last_used_at=record.last_used_at,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class ApiKeyRepository:
    """Issue, list, revoke and verify programmatic credentials (V7.2).

    Verification is a point read on ``key_prefix`` followed by a constant-time
    digest comparison. The plaintext never leaves this module: it is returned
    once from :meth:`create` and only its hash is stored.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        payload: ApiKeyCreate,
        *,
        owner_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        created_by: str | None = None,
        owner_permissions: frozenset[str] | None = None,
        now: datetime | None = None,
    ) -> ApiKeyIssued:
        requested = payload.scopes
        if requested is not None:
            known = {permission.value for permission in Permission}
            unknown = sorted(set(requested) - known)
            if unknown:
                return ApiKeyIssued(
                    ApiKeyOutcome.INVALID_SCOPE,
                    detail=f"unknown permission(s): {', '.join(unknown)}",
                )
            if owner_permissions is not None:
                # A key may only ever narrow its owner. Asking for more is a
                # caller bug, and silently trimming it would hand back a
                # credential that behaves unlike the request that created it.
                exceeding = sorted(set(requested) - set(owner_permissions))
                if exceeding:
                    return ApiKeyIssued(
                        ApiKeyOutcome.SCOPE_EXCEEDS_OWNER,
                        detail=(
                            "the owner does not hold: " + ", ".join(exceeding)
                        ),
                    )
            scopes: list[str] | None = sorted(set(requested))
        else:
            scopes = None

        moment = now or _utcnow()
        expires_at = (
            moment + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days
            else None
        )
        prefix, plaintext = generate_api_key()
        record = ApiKeyRecord(
            id=f"key_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            actor_id=owner_id,
            name=payload.name[:160],
            key_prefix=prefix,
            key_hash=hash_api_key(plaintext),
            scopes=scopes,
            status="active",
            created_by=created_by,
            expires_at=expires_at,
        )
        self.session.add(record)
        self.session.flush()
        return ApiKeyIssued(
            ApiKeyOutcome.CREATED,
            api_key=api_key_from_record(record),
            plaintext=plaintext,
        )

    def get(self, api_key_id: str) -> ApiKey | None:
        record = self.session.get(ApiKeyRecord, api_key_id)
        return api_key_from_record(record) if record else None

    def find_by_prefix(self, key_prefix: str) -> ApiKeyRecord | None:
        statement = select(ApiKeyRecord).where(ApiKeyRecord.key_prefix == key_prefix)
        return self.session.scalar(statement)

    def verify(self, plaintext: str, *, now: datetime | None = None) -> ApiKeyRecord | None:
        """Return the record behind a live credential, or ``None``.

        Revoked and expired keys are rejected here rather than by the caller, so
        there is exactly one place that decides a credential is still usable.
        """
        prefix = api_key_prefix_of(plaintext)
        if prefix is None:
            return None
        record = self.find_by_prefix(prefix)
        if record is None:
            return None
        # ``compare_digest`` keeps the comparison time-independent of how many
        # leading characters happen to match.
        if not compare_digest(record.key_hash, hash_api_key(plaintext.strip())):
            return None
        if record.status != "active":
            return None
        moment = now or _utcnow()
        if record.expires_at is not None and as_utc(record.expires_at) <= moment:
            return None
        return record

    def touch(self, api_key_id: str, *, now: datetime | None = None) -> None:
        record = self.session.get(ApiKeyRecord, api_key_id)
        if record is None:
            return
        record.last_used_at = now or _utcnow()
        self.session.flush()

    def revoke(
        self,
        api_key_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ApiKeyRevocation:
        record = self.session.get(ApiKeyRecord, api_key_id)
        if record is None or record.tenant_id != tenant_id:
            return ApiKeyRevocation()
        if record.status == "revoked":
            return ApiKeyRevocation(api_key=api_key_from_record(record), changed=False)
        record.status = "revoked"
        self.session.flush()
        return ApiKeyRevocation(api_key=api_key_from_record(record), changed=True)

    def list_keys(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[ApiKey], int]:
        filters = [ApiKeyRecord.tenant_id == tenant_id]
        if actor_id is not None:
            filters.append(ApiKeyRecord.actor_id == actor_id)
        if status is not None:
            filters.append(ApiKeyRecord.status == status)
        total = int(
            self.session.scalar(
                select(func.count()).select_from(select(ApiKeyRecord).where(*filters).subquery())
            )
            or 0
        )
        statement = (
            select(ApiKeyRecord)
            .where(*filters)
            .limit(max(1, min(limit, 200)))
            .offset(max(0, offset))
        )
        statement = _sorted(
            statement,
            ApiKeyRecord,
            sort,
            (ApiKeyRecord.created_at.desc(), ApiKeyRecord.id.desc()),
        )
        keys = [api_key_from_record(record) for record in self.session.scalars(statement)]
        return keys, total


class BrandStyleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_brand_kit(
        self,
        project_id: str,
        payload: BrandKitCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> BrandKit | None:
        if self.session.get(ProjectRecord, project_id) is None:
            return None
        record = BrandKitRecord(
            id=f"brand_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=project_id,
            name=payload.name,
            description=payload.description,
            logo_asset_id=payload.logo_asset_id,
            color_palette=payload.color_palette,
            typography=payload.typography,
            safe_area=payload.safe_area,
            brand_voice=payload.brand_voice,
            banned_terms=payload.banned_terms,
            brand_metadata=payload.brand_metadata,
            status=payload.status,
        )
        self.session.add(record)
        self.session.flush()
        return brand_kit_from_record(record)

    def list_brand_kits(self, project_id: str) -> list[BrandKit] | None:
        if self.session.get(ProjectRecord, project_id) is None:
            return None
        statement = (
            select(BrandKitRecord)
            .where(BrandKitRecord.project_id == project_id)
            .order_by(BrandKitRecord.created_at, BrandKitRecord.id)
        )
        return [brand_kit_from_record(record) for record in self.session.scalars(statement)]

    def get_brand_kit(self, brand_kit_id: str) -> BrandKit | None:
        record = self.session.get(BrandKitRecord, brand_kit_id)
        return brand_kit_from_record(record) if record else None

    def update_brand_kit(
        self,
        brand_kit_id: str,
        payload: BrandKitUpdate,
    ) -> BrandKit | None:
        record = self.session.get(BrandKitRecord, brand_kit_id)
        if record is None:
            return None
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, key, value)
        self.session.flush()
        return brand_kit_from_record(record)

    def create_style_preset(
        self,
        project_id: str,
        payload: StylePresetCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> StylePreset | None:
        project = self.session.get(ProjectRecord, project_id)
        if project is None:
            return None
        if payload.brand_kit_id and not self._brand_kit_belongs_to_project(
            payload.brand_kit_id,
            project_id,
        ):
            raise ValueError("brand_kit_id does not belong to this project")
        record = StylePresetRecord(
            id=f"style_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=project_id,
            brand_kit_id=payload.brand_kit_id,
            name=payload.name,
            description=payload.description,
            style_key=payload.style_key,
            is_default=payload.is_default,
            aspect_ratio=payload.aspect_ratio,
            subtitle_style=payload.subtitle_style,
            visual_style=payload.visual_style,
            tone=payload.tone,
            prompt_guidelines=payload.prompt_guidelines,
            render_defaults=payload.render_defaults,
            provider_preferences=payload.provider_preferences,
            guardrails=payload.guardrails,
            preset_metadata=payload.preset_metadata,
            status=payload.status,
        )
        self.session.add(record)
        self.session.flush()
        if payload.is_default:
            self._set_default_style_preset_record(project, record)
        self.session.flush()
        return style_preset_from_record(record)

    def list_style_presets(self, project_id: str) -> list[StylePreset] | None:
        if self.session.get(ProjectRecord, project_id) is None:
            return None
        statement = (
            select(StylePresetRecord)
            .where(StylePresetRecord.project_id == project_id)
            .order_by(
                StylePresetRecord.is_default.desc(),
                StylePresetRecord.created_at,
                StylePresetRecord.id,
            )
        )
        return [style_preset_from_record(record) for record in self.session.scalars(statement)]

    def get_style_preset(self, style_preset_id: str) -> StylePreset | None:
        record = self.session.get(StylePresetRecord, style_preset_id)
        return style_preset_from_record(record) if record else None

    def update_style_preset(
        self,
        style_preset_id: str,
        payload: StylePresetUpdate,
    ) -> StylePreset | None:
        record = self.session.get(StylePresetRecord, style_preset_id)
        if record is None:
            return None
        update_data = payload.model_dump(exclude_unset=True)
        if (
            "brand_kit_id" in update_data
            and update_data["brand_kit_id"]
            and not self._brand_kit_belongs_to_project(update_data["brand_kit_id"], record.project_id)
        ):
            raise ValueError("brand_kit_id does not belong to this project")
        for key, value in update_data.items():
            setattr(record, key, value)
        project = self.session.get(ProjectRecord, record.project_id)
        if project is not None and update_data.get("is_default") is True:
            self._set_default_style_preset_record(project, record)
        elif (
            project is not None
            and update_data.get("is_default") is False
            and project.default_style_preset_id == record.id
        ):
            project.default_style_preset_id = None
        self.session.flush()
        return style_preset_from_record(record)

    def set_project_default_style_preset(
        self,
        project_id: str,
        style_preset_id: str,
    ) -> StylePreset | None:
        project = self.session.get(ProjectRecord, project_id)
        record = self.session.get(StylePresetRecord, style_preset_id)
        if project is None or record is None or record.project_id != project_id:
            return None
        self._set_default_style_preset_record(project, record)
        self.session.flush()
        return style_preset_from_record(record)

    def get_project_default_style_preset(self, project_id: str) -> StylePreset | None:
        project = self.session.get(ProjectRecord, project_id)
        if project is None or not project.default_style_preset_id:
            return None
        record = self.session.get(StylePresetRecord, project.default_style_preset_id)
        if record is None or record.project_id != project_id:
            return None
        return style_preset_from_record(record)

    def style_preset_snapshot(self, style_preset_id: str) -> dict | None:
        style_record = self.session.get(StylePresetRecord, style_preset_id)
        if style_record is None:
            return None
        brand_record = (
            self.session.get(BrandKitRecord, style_record.brand_kit_id)
            if style_record.brand_kit_id
            else None
        )
        return {
            "style_preset": style_preset_from_record(style_record).model_dump(mode="json"),
            "brand_kit": brand_kit_from_record(brand_record).model_dump(mode="json")
            if brand_record
            else None,
        }

    def default_style_preset_snapshot_for_project(self, project_id: str) -> dict | None:
        project = self.session.get(ProjectRecord, project_id)
        if project is None or not project.default_style_preset_id:
            return None
        return self.style_preset_snapshot(project.default_style_preset_id)

    def _brand_kit_belongs_to_project(self, brand_kit_id: str, project_id: str) -> bool:
        record = self.session.get(BrandKitRecord, brand_kit_id)
        return record is not None and record.project_id == project_id

    def _set_default_style_preset_record(
        self,
        project: ProjectRecord,
        record: StylePresetRecord,
    ) -> None:
        statement = select(StylePresetRecord).where(
            StylePresetRecord.project_id == project.id,
            StylePresetRecord.is_default.is_(True),
        )
        for existing in self.session.scalars(statement):
            existing.is_default = False
        record.is_default = True
        project.default_style_preset_id = record.id


class ProductionRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        *,
        project_id: str,
        payload: RunCreate,
        pipeline: PipelineDefinition,
        tenant_id: str = DEFAULT_TENANT_ID,
        style_preset_snapshot: dict | None = None,
    ) -> ProductionRun:
        pipeline_version = self._ensure_pipeline_version(pipeline)
        pipeline_snapshot = pipeline_version.definition_snapshot
        input_payload = payload.model_dump()
        input_payload["pipeline_key"] = pipeline.pipeline_key
        if style_preset_snapshot is not None:
            input_payload["style_preset_snapshot"] = style_preset_snapshot
            style_preset = style_preset_snapshot.get("style_preset") or {}
            if not input_payload.get("style_key"):
                input_payload["style_key"] = style_preset.get("style_key")
        stages = [
            StageRunRecord(
                id=f"stage_{uuid4().hex[:12]}",
                run_id="",
                stage_key=stage.stage_key,
                stage_order=stage.order,
                status=StageStatus.PENDING.value,
                attempt=1,
                requires_human_review=stage.requires_human_review,
                output_artifact_types=stage.output_artifact_types,
            )
            for stage in pipeline.stages
        ]
        run = ProductionRunRecord(
            id=f"run_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=project_id,
            pipeline_key=pipeline.pipeline_key,
            pipeline_version_id=pipeline_version.id,
            pipeline_version=pipeline.version,
            pipeline_dsl_version=pipeline.dsl_version,
            pipeline_definition_snapshot=pipeline_snapshot,
            input_payload=input_payload,
            current_stage_key=stages[0].stage_key if stages else None,
            status=RunStatus.DRAFT.value,
            target_duration_seconds=payload.target_duration_seconds,
            target_platform=payload.target_platform,
            style_key=input_payload.get("style_key"),
            stages=stages,
        )
        for stage in stages:
            stage.run_id = run.id
        self.session.add(run)
        self.session.flush()
        return production_run_from_record(run)

    def _ensure_pipeline_version(self, pipeline: PipelineDefinition) -> PipelineVersionRecord:
        snapshot = pipeline.snapshot()
        content_hash = pipeline.content_hash or _stable_json_hash(snapshot)
        statement = select(PipelineVersionRecord).where(
            PipelineVersionRecord.pipeline_key == pipeline.pipeline_key,
            PipelineVersionRecord.version == pipeline.version,
            PipelineVersionRecord.content_hash == content_hash,
        )
        record = self.session.scalar(statement)
        if record is not None:
            return record
        for existing in self.session.scalars(
            select(PipelineVersionRecord).where(
                PipelineVersionRecord.pipeline_key == pipeline.pipeline_key,
                PipelineVersionRecord.is_current.is_(True),
            )
        ):
            existing.is_current = False
        record = PipelineVersionRecord(
            id=f"pipever_{uuid4().hex[:12]}",
            pipeline_key=pipeline.pipeline_key,
            version=pipeline.version,
            dsl_version=pipeline.dsl_version,
            status=pipeline.status,
            content_hash=content_hash,
            source_path=None,
            definition_snapshot=snapshot,
            is_current=True,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_run(self, run_id: str) -> ProductionRun | None:
        record = self._get_run_record(run_id)
        return production_run_from_record(record) if record else None

    def update_run_state(
        self,
        run_id: str,
        *,
        status: RunStatus,
        current_stage_key: str | None,
    ) -> ProductionRun | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None
        record.status = status.value
        record.current_stage_key = current_stage_key
        self.session.flush()
        return production_run_from_record(record)

    def update_stage_status(
        self,
        run_id: str,
        stage_key: str,
        status: StageStatus,
    ) -> ProductionRun | None:
        record = self._get_run_record(run_id)
        if record is None:
            return None
        for stage in record.stages:
            if stage.stage_key == stage_key:
                stage.status = status.value
                self.session.flush()
                return production_run_from_record(record)
        return None

    def _get_run_record(self, run_id: str) -> ProductionRunRecord | None:
        statement = (
            select(ProductionRunRecord)
            .where(ProductionRunRecord.id == run_id)
            .options(selectinload(ProductionRunRecord.stages))
        )
        return self.session.scalar(statement)


class ArtifactRepository:
    def __init__(
        self,
        session: Session,
        *,
        schema_registry: ArtifactSchemaRegistry | None = None,
    ) -> None:
        self.session = session
        self.schema_registry = schema_registry or ArtifactSchemaRegistry()

    def append_artifact(
        self,
        *,
        run_id: str,
        stage_key: str,
        artifact_type: str,
        payload: dict,
        stage_run_id: str | None = None,
        schema_version: str = "1.0",
        schema_key: str | None = None,
        producer: str | None = None,
        lineage: dict | None = None,
        derived_from: list[dict] | None = None,
    ) -> Artifact:
        schema = self.schema_registry.validate(
            artifact_type=artifact_type,
            payload=payload,
            schema_version=schema_version,
        )
        resolved_schema_key = schema_key or (schema.schema_key if schema else None)
        version = self.next_version(run_id=run_id, artifact_type=artifact_type)
        current_statement = select(ArtifactRecord).where(
            ArtifactRecord.run_id == run_id,
            ArtifactRecord.artifact_type == artifact_type,
            ArtifactRecord.is_current.is_(True),
        )
        for current_record in self.session.scalars(current_statement):
            current_record.is_current = False

        record = ArtifactRecord(
            id=f"artifact_{uuid4().hex[:12]}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            artifact_type=artifact_type,
            version=version,
            payload=payload,
            schema_version=schema_version,
            schema_key=resolved_schema_key,
            producer=producer,
            lineage=lineage,
            derived_from=derived_from,
            is_current=True,
        )
        self.session.add(record)
        self.session.flush()
        return artifact_from_record(record)

    def list_artifacts(self, run_id: str, *, current_only: bool = False) -> list[Artifact]:
        statement = (
            select(ArtifactRecord)
            .outerjoin(StageRunRecord, ArtifactRecord.stage_run_id == StageRunRecord.id)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(
                StageRunRecord.stage_order,
                ArtifactRecord.created_at,
                ArtifactRecord.version,
                ArtifactRecord.id,
            )
        )
        if current_only:
            statement = statement.where(ArtifactRecord.is_current.is_(True))
        return [artifact_from_record(record) for record in self.session.scalars(statement)]

    def current_payloads(self, run_id: str) -> dict:
        payloads = {}
        for artifact in self.list_artifacts(run_id, current_only=True):
            payloads[artifact.artifact_type] = artifact.payload
        return payloads

    def next_version(self, *, run_id: str, artifact_type: str) -> int:
        statement = select(ArtifactRecord.version).where(
            ArtifactRecord.run_id == run_id,
            ArtifactRecord.artifact_type == artifact_type,
        )
        versions = list(self.session.scalars(statement))
        return max(versions) + 1 if versions else 1


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session
        # Every event in the platform passes through ``append_event``, which is
        # exactly why the webhook outbox hangs off here: fan-out cannot be
        # forgotten by a new code path that emits an event, because there is no
        # other way to emit one.
        self.webhooks = WebhookRepository(session)

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        source_type: EventSourceType,
        tenant_id: str = DEFAULT_TENANT_ID,
        source_id: str | None = None,
        stage_key: str | None = None,
        stage_run_id: str | None = None,
        tool_invocation_id: str | None = None,
        severity: EventSeverity = EventSeverity.INFO,
        message: str | None = None,
        payload: dict | None = None,
    ) -> EventLog:
        record = EventLogRecord(
            id=f"event_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            run_id=run_id,
            sequence=self.next_sequence(run_id),
            event_type=event_type,
            source_type=source_type.value,
            source_id=source_id,
            stage_key=stage_key,
            stage_run_id=stage_run_id,
            tool_invocation_id=tool_invocation_id,
            severity=severity.value,
            message=message,
            payload=payload or {},
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        event = event_from_record(record)
        # Transactional outbox (V7.3). Queued before the caller commits, so an
        # event and the deliveries announcing it either both survive or neither
        # does. A no-op for everything outside the published event catalog.
        self.webhooks.enqueue_for_event(event=event, tenant_id=tenant_id)
        return event

    def list_events(
        self,
        run_id: str,
        *,
        source_type: EventSourceType | None = None,
        stage_key: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[EventLog]:
        statement = select(EventLogRecord).where(EventLogRecord.run_id == run_id)
        if source_type is not None:
            statement = statement.where(EventLogRecord.source_type == source_type.value)
        if stage_key is not None:
            statement = statement.where(EventLogRecord.stage_key == stage_key)
        if event_type is not None:
            statement = statement.where(EventLogRecord.event_type == event_type)
        statement = statement.order_by(EventLogRecord.sequence).limit(limit)
        return [event_from_record(record) for record in self.session.scalars(statement)]

    def next_sequence(self, run_id: str) -> int:
        statement = select(EventLogRecord.sequence).where(EventLogRecord.run_id == run_id)
        sequences = list(self.session.scalars(statement))
        return max(sequences) + 1 if sequences else 1


class ReviewRepository:
    """Persistence for the review-gate records.

    Decisions are append-only: rows are never updated or deleted, and the current
    value of a review request is derived from the highest ``sequence`` row.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def register_reviewer(
        self,
        user_id: str | None,
        *,
        display_name: str | None = None,
    ) -> str | None:
        """Ensure the ``users`` row behind an externally supplied id exists.

        Every reviewer id that lands in ``review_requests.assignee_user_id``,
        ``review_decisions.actor_user_id`` or ``review_comments.author_user_id``
        is foreign-keyed to ``users.id``, so the id has to be registered before
        the write. See :class:`UserRepository`.
        """
        return UserRepository(self.session).ensure_user(
            user_id, display_name=display_name
        )

    def create_request(
        self,
        *,
        run_id: str,
        project_id: str,
        stage_run_id: str,
        stage_key: str,
        pipeline_key: str,
        iteration: int,
        gate_type: str,
        gates_stage_key: str | None,
        artifact_scope: list[dict],
        review_focus: list[str],
        success_criteria: list[str],
        tenant_id: str = DEFAULT_TENANT_ID,
        assignee_user_id: str | None = None,
        due_at: datetime | None = None,
    ) -> ReviewRequest:
        record = ReviewRequestRecord(
            id=f"reviewreq_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key=stage_key,
            pipeline_key=pipeline_key,
            iteration=iteration,
            status=ReviewRequestStatus.OPEN.value,
            current_decision=None,
            gate_type=gate_type,
            gates_stage_key=gates_stage_key,
            artifact_scope=list(artifact_scope),
            review_focus=list(review_focus),
            success_criteria=list(success_criteria),
            assignee_user_id=self.register_reviewer(assignee_user_id),
            due_at=store_utc(due_at),
            opened_at=_utcnow(),
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return review_request_from_record(record)

    def get_request_record(self, review_request_id: str) -> ReviewRequestRecord | None:
        return self.session.get(ReviewRequestRecord, review_request_id)

    def get_request(self, review_request_id: str) -> ReviewRequest | None:
        record = self.get_request_record(review_request_id)
        return review_request_from_record(record) if record is not None else None

    def get_request_detail(
        self,
        review_request_id: str,
        *,
        workspace_url: str = "",
        run_board_url: str = "",
        attachments: list[ReviewAttachment] | None = None,
    ) -> ReviewRequestDetail | None:
        record = self.get_request_record(review_request_id)
        if record is None:
            return None
        base = review_request_from_record(record)
        # Attachments are composed one level up (``DatabaseStore``), where the
        # asset snapshot can be resolved through the asset repository; this
        # method only assembles what it can reach from its own tables.
        return ReviewRequestDetail(
            **base.model_dump(),
            decisions=self.list_decisions(review_request_id),
            comments=self.list_comments(review_request_id),
            action_items=self.list_action_items(review_request_id),
            attachments=attachments or [],
            workspace_url=workspace_url,
            run_board_url=run_board_url,
        )

    def list_requests(
        self,
        run_id: str,
        *,
        status: ReviewRequestStatus | None = None,
        stage_key: str | None = None,
    ) -> list[ReviewRequest]:
        statement = select(ReviewRequestRecord).where(ReviewRequestRecord.run_id == run_id)
        if status is not None:
            statement = statement.where(ReviewRequestRecord.status == status.value)
        if stage_key is not None:
            statement = statement.where(ReviewRequestRecord.stage_key == stage_key)
        statement = statement.order_by(
            ReviewRequestRecord.iteration,
            ReviewRequestRecord.created_at,
            ReviewRequestRecord.id,
        )
        return [review_request_from_record(row) for row in self.session.scalars(statement)]

    def list_project_requests(
        self,
        project_id: str,
        *,
        status: ReviewRequestStatus | None = None,
        assignee_user_id: str | None = None,
        stage_key: str | None = None,
        limit: int = 200,
    ) -> list[ReviewRequest]:
        statement = select(ReviewRequestRecord).where(
            ReviewRequestRecord.project_id == project_id
        )
        if status is not None:
            statement = statement.where(ReviewRequestRecord.status == status.value)
        if assignee_user_id is not None:
            statement = statement.where(
                ReviewRequestRecord.assignee_user_id == assignee_user_id
            )
        if stage_key is not None:
            statement = statement.where(ReviewRequestRecord.stage_key == stage_key)
        statement = statement.order_by(
            ReviewRequestRecord.opened_at,
            ReviewRequestRecord.id,
        ).limit(max(1, limit))
        return [review_request_from_record(row) for row in self.session.scalars(statement)]

    def find_open_request(self, *, stage_run_id: str) -> ReviewRequest | None:
        statement = (
            select(ReviewRequestRecord)
            .where(ReviewRequestRecord.stage_run_id == stage_run_id)
            .where(ReviewRequestRecord.status == ReviewRequestStatus.OPEN.value)
            .order_by(ReviewRequestRecord.iteration.desc(), ReviewRequestRecord.id.desc())
        )
        record = self.session.scalars(statement).first()
        return review_request_from_record(record) if record is not None else None

    def list_open_requests_for_run(self, run_id: str) -> list[ReviewRequest]:
        statement = (
            select(ReviewRequestRecord)
            .where(ReviewRequestRecord.run_id == run_id)
            .where(ReviewRequestRecord.status == ReviewRequestStatus.OPEN.value)
        )
        return [review_request_from_record(row) for row in self.session.scalars(statement)]

    def next_iteration(self, *, run_id: str, stage_key: str) -> int:
        statement = select(ReviewRequestRecord.iteration).where(
            ReviewRequestRecord.run_id == run_id,
            ReviewRequestRecord.stage_key == stage_key,
        )
        iterations = [value for value in self.session.scalars(statement) if value is not None]
        return max(iterations) + 1 if iterations else 1

    def set_request_status(
        self,
        review_request_id: str,
        *,
        status: ReviewRequestStatus,
        current_decision: ReviewDecisionType | None = None,
        closed: bool = False,
    ) -> ReviewRequest | None:
        record = self.get_request_record(review_request_id)
        if record is None:
            return None
        record.status = status.value
        if current_decision is not None:
            record.current_decision = current_decision.value
        record.closed_at = _utcnow() if closed else None
        self.session.flush()
        self.session.refresh(record)
        return review_request_from_record(record)

    def assign_request(
        self,
        review_request_id: str,
        *,
        assignee_user_id: str | None,
        due_at: datetime | None,
    ) -> ReviewRequest | None:
        record = self.get_request_record(review_request_id)
        if record is None:
            return None
        record.assignee_user_id = self.register_reviewer(assignee_user_id)
        record.due_at = store_utc(due_at)
        self.session.flush()
        self.session.refresh(record)
        return review_request_from_record(record)

    def reopen_request(
        self,
        review_request_id: str,
        *,
        iteration: int,
        artifact_scope: list[dict],
    ) -> ReviewRequest | None:
        """Pull a closed request back to ``open`` with a fresh artifact scope.

        Only the request row is mutated; the decision log stays append-only.
        """
        record = self.get_request_record(review_request_id)
        if record is None:
            return None
        record.iteration = iteration
        record.artifact_scope = list(artifact_scope)
        record.status = ReviewRequestStatus.OPEN.value
        record.current_decision = ReviewDecisionType.REOPEN.value
        record.closed_at = None
        self.session.flush()
        self.session.refresh(record)
        return review_request_from_record(record)

    def supersede_requests_for_stage_run(
        self,
        *,
        stage_run_ids: list[str],
    ) -> list[str]:
        if not stage_run_ids:
            return []
        statement = (
            select(ReviewRequestRecord)
            .where(ReviewRequestRecord.stage_run_id.in_(stage_run_ids))
            .where(ReviewRequestRecord.status == ReviewRequestStatus.OPEN.value)
        )
        superseded: list[str] = []
        for record in self.session.scalars(statement):
            record.status = ReviewRequestStatus.SUPERSEDED.value
            record.closed_at = _utcnow()
            superseded.append(record.id)
        self.session.flush()
        return superseded

    def append_decision(
        self,
        *,
        review_request_id: str,
        run_id: str,
        round_number: int,
        decision: ReviewDecisionType,
        tenant_id: str = DEFAULT_TENANT_ID,
        actor_user_id: str | None = None,
        actor_display_name: str | None = None,
        comment: str | None = None,
        reason_code: str | None = None,
        artifact_refs: list[dict] | None = None,
    ) -> ReviewDecision:
        record = ReviewDecisionRecord(
            id=f"reviewdecision_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            review_request_id=review_request_id,
            run_id=run_id,
            sequence=self.next_decision_sequence(review_request_id),
            round=round_number,
            decision=decision.value,
            actor_user_id=self.register_reviewer(
                actor_user_id, display_name=actor_display_name
            ),
            actor_display_name=actor_display_name,
            comment=comment,
            reason_code=reason_code,
            artifact_refs=list(artifact_refs or []),
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return review_decision_from_record(record)

    def next_decision_sequence(self, review_request_id: str) -> int:
        statement = select(ReviewDecisionRecord.sequence).where(
            ReviewDecisionRecord.review_request_id == review_request_id
        )
        sequences = [value for value in self.session.scalars(statement) if value is not None]
        return max(sequences) + 1 if sequences else 1

    def list_decisions(self, review_request_id: str) -> list[ReviewDecision]:
        statement = (
            select(ReviewDecisionRecord)
            .where(ReviewDecisionRecord.review_request_id == review_request_id)
            .order_by(ReviewDecisionRecord.sequence, ReviewDecisionRecord.id)
        )
        return [review_decision_from_record(row) for row in self.session.scalars(statement)]

    def list_run_decisions(self, run_id: str, *, limit: int = 300) -> list[ReviewDecision]:
        statement = (
            select(ReviewDecisionRecord)
            .where(ReviewDecisionRecord.run_id == run_id)
            .order_by(ReviewDecisionRecord.created_at, ReviewDecisionRecord.id)
            .limit(max(1, limit))
        )
        return [review_decision_from_record(row) for row in self.session.scalars(statement)]

    def create_comment(
        self,
        *,
        review_request_id: str,
        run_id: str,
        stage_key: str,
        body: str,
        severity: ReviewCommentSeverity = ReviewCommentSeverity.SUGGESTION,
        tenant_id: str = DEFAULT_TENANT_ID,
        artifact_id: str | None = None,
        artifact_version: int | None = None,
        author_user_id: str | None = None,
        author_display_name: str | None = None,
    ) -> ReviewComment:
        record = ReviewCommentRecord(
            id=f"reviewcomment_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            review_request_id=review_request_id,
            run_id=run_id,
            stage_key=stage_key,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            author_user_id=self.register_reviewer(
                author_user_id, display_name=author_display_name
            ),
            author_display_name=author_display_name,
            severity=severity.value,
            body=body,
            status=ReviewCommentStatus.OPEN.value,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return review_comment_from_record(record)

    def get_comment_record(self, comment_id: str) -> ReviewCommentRecord | None:
        return self.session.get(ReviewCommentRecord, comment_id)

    def list_comments(
        self,
        review_request_id: str,
        *,
        status: ReviewCommentStatus | None = None,
    ) -> list[ReviewComment]:
        statement = select(ReviewCommentRecord).where(
            ReviewCommentRecord.review_request_id == review_request_id
        )
        if status is not None:
            statement = statement.where(ReviewCommentRecord.status == status.value)
        statement = statement.order_by(ReviewCommentRecord.created_at, ReviewCommentRecord.id)
        return [review_comment_from_record(row) for row in self.session.scalars(statement)]

    def list_run_comments(self, run_id: str) -> list[ReviewComment]:
        statement = (
            select(ReviewCommentRecord)
            .where(ReviewCommentRecord.run_id == run_id)
            .order_by(ReviewCommentRecord.created_at, ReviewCommentRecord.id)
        )
        return [review_comment_from_record(row) for row in self.session.scalars(statement)]

    def count_open_comments(self, review_request_id: str) -> int:
        statement = (
            select(ReviewCommentRecord.id)
            .where(ReviewCommentRecord.review_request_id == review_request_id)
            .where(ReviewCommentRecord.status == ReviewCommentStatus.OPEN.value)
        )
        return len(list(self.session.scalars(statement)))

    def count_open_comments_by_severity(
        self,
        review_request_id: str,
        *,
        severity: ReviewCommentSeverity,
    ) -> int:
        statement = (
            select(ReviewCommentRecord.id)
            .where(ReviewCommentRecord.review_request_id == review_request_id)
            .where(ReviewCommentRecord.status == ReviewCommentStatus.OPEN.value)
            .where(ReviewCommentRecord.severity == severity.value)
        )
        return len(list(self.session.scalars(statement)))

    def set_comment_status(
        self,
        comment_id: str,
        *,
        status: ReviewCommentStatus,
        resolved_by_decision_id: str | None = None,
    ) -> ReviewComment | None:
        record = self.get_comment_record(comment_id)
        if record is None:
            return None
        record.status = status.value
        if resolved_by_decision_id is not None:
            record.resolved_by_decision_id = resolved_by_decision_id
        self.session.flush()
        self.session.refresh(record)
        return review_comment_from_record(record)

    def resolve_comments(
        self,
        comment_ids: list[str],
        *,
        resolved_by_decision_id: str | None = None,
    ) -> list[str]:
        resolved: list[str] = []
        for comment_id in comment_ids:
            record = self.get_comment_record(comment_id)
            if record is None:
                continue
            record.status = ReviewCommentStatus.RESOLVED.value
            if resolved_by_decision_id is not None:
                record.resolved_by_decision_id = resolved_by_decision_id
            resolved.append(record.id)
        self.session.flush()
        return resolved

    def create_action_item(
        self,
        *,
        review_request_id: str,
        decision_id: str,
        run_id: str,
        kind: ReviewActionKind,
        instruction: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        stage_key: str | None = None,
        artifact_type: str | None = None,
        stage_run_id: str | None = None,
        linked_comment_ids: list[str] | None = None,
        result_action: str | None = None,
        status: ReviewActionStatus = ReviewActionStatus.OPEN,
    ) -> ReviewActionItem:
        record = ReviewActionItemRecord(
            id=f"reviewaction_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            review_request_id=review_request_id,
            decision_id=decision_id,
            run_id=run_id,
            kind=kind.value,
            stage_key=stage_key,
            artifact_type=artifact_type,
            stage_run_id=stage_run_id,
            instruction=instruction,
            linked_comment_ids=list(linked_comment_ids or []),
            result_action=result_action,
            status=status.value,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return review_action_item_from_record(record)

    def list_action_items(
        self,
        review_request_id: str,
        *,
        status: ReviewActionStatus | None = None,
    ) -> list[ReviewActionItem]:
        statement = select(ReviewActionItemRecord).where(
            ReviewActionItemRecord.review_request_id == review_request_id
        )
        if status is not None:
            statement = statement.where(ReviewActionItemRecord.status == status.value)
        statement = statement.order_by(
            ReviewActionItemRecord.created_at,
            ReviewActionItemRecord.id,
        )
        return [review_action_item_from_record(row) for row in self.session.scalars(statement)]

    # --- attachments -------------------------------------------------------

    def get_attachment_record(self, attachment_id: str) -> ReviewAttachmentRecord | None:
        return self.session.get(ReviewAttachmentRecord, attachment_id)

    def list_attachments(self, review_request_id: str) -> list[ReviewAttachmentRecord]:
        statement = (
            select(ReviewAttachmentRecord)
            .where(ReviewAttachmentRecord.review_request_id == review_request_id)
            .order_by(ReviewAttachmentRecord.created_at, ReviewAttachmentRecord.id)
        )
        return list(self.session.scalars(statement))

    def mount_attachment(
        self,
        *,
        review_request_id: str,
        run_id: str,
        asset_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        note: str | None = None,
        added_by_user_id: str | None = None,
        added_by_display_name: str | None = None,
    ) -> tuple[ReviewAttachmentRecord, bool]:
        """Mount an asset on a review request.

        Returns ``(record, created)``; re-mounting the same asset is idempotent
        and returns the existing row. The unique pair in the schema is the
        backstop — the pre-check only avoids relying on the IntegrityError path.
        """
        existing = (
            self.session.query(ReviewAttachmentRecord)
            .filter(
                ReviewAttachmentRecord.review_request_id == review_request_id,
                ReviewAttachmentRecord.asset_id == asset_id,
            )
            .one_or_none()
        )
        if existing is not None:
            return existing, False
        record = ReviewAttachmentRecord(
            id=f"rvatt_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            review_request_id=review_request_id,
            run_id=run_id,
            asset_id=asset_id,
            note=note,
            added_by_user_id=self.register_reviewer(
                added_by_user_id, display_name=added_by_display_name
            ),
            added_by_display_name=added_by_display_name,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return record, True

    def remove_attachment(self, attachment_id: str) -> ReviewAttachmentRecord | None:
        record = self.get_attachment_record(attachment_id)
        if record is None:
            return None
        self.session.delete(record)
        self.session.flush()
        return record

    def list_run_action_items(
        self,
        run_id: str,
        *,
        status: ReviewActionStatus | None = None,
    ) -> list[ReviewActionItem]:
        statement = select(ReviewActionItemRecord).where(
            ReviewActionItemRecord.run_id == run_id
        )
        if status is not None:
            statement = statement.where(ReviewActionItemRecord.status == status.value)
        statement = statement.order_by(
            ReviewActionItemRecord.created_at,
            ReviewActionItemRecord.id,
        )
        return [review_action_item_from_record(row) for row in self.session.scalars(statement)]

    def count_open_action_items(self, review_request_id: str) -> int:
        statement = (
            select(ReviewActionItemRecord.id)
            .where(ReviewActionItemRecord.review_request_id == review_request_id)
            .where(ReviewActionItemRecord.status == ReviewActionStatus.OPEN.value)
        )
        return len(list(self.session.scalars(statement)))

    def set_action_item_status(
        self,
        action_item_id: str,
        *,
        status: ReviewActionStatus,
        produced_artifact_id: str | None = None,
        result_action: str | None = None,
    ) -> ReviewActionItem | None:
        record = self.session.get(ReviewActionItemRecord, action_item_id)
        if record is None:
            return None
        record.status = status.value
        if produced_artifact_id is not None:
            record.produced_artifact_id = produced_artifact_id
        if result_action is not None:
            record.result_action = result_action
        record.closed_at = (
            _utcnow()
            if status in {ReviewActionStatus.DONE, ReviewActionStatus.DISMISSED}
            else None
        )
        self.session.flush()
        self.session.refresh(record)
        return review_action_item_from_record(record)

    def close_action_items_for_stage(
        self,
        *,
        run_id: str,
        stage_key: str,
        produced_artifact_id: str | None = None,
        result_action: str | None = None,
    ) -> list[str]:
        statement = (
            select(ReviewActionItemRecord)
            .where(ReviewActionItemRecord.run_id == run_id)
            .where(ReviewActionItemRecord.stage_key == stage_key)
            .where(
                ReviewActionItemRecord.status.in_(
                    [ReviewActionStatus.OPEN.value, ReviewActionStatus.IN_PROGRESS.value]
                )
            )
        )
        closed: list[str] = []
        for record in self.session.scalars(statement):
            record.status = ReviewActionStatus.DONE.value
            record.closed_at = _utcnow()
            if produced_artifact_id is not None:
                record.produced_artifact_id = produced_artifact_id
            if result_action is not None:
                record.result_action = result_action
            closed.append(record.id)
        self.session.flush()
        return closed


class AssetRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_asset(
        self,
        *,
        asset_id: str,
        run_id: str,
        asset_type: str,
        storage_url: str,
        stage_run_id: str | None = None,
        provider: str | None = None,
        tool_key: str | None = None,
        mime_type: str | None = None,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        file_size: int | None = None,
        checksum: str | None = None,
        asset_metadata: dict | None = None,
    ) -> Asset:
        record = AssetRecord(
            id=asset_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            asset_type=asset_type,
            provider=provider,
            tool_key=tool_key,
            storage_url=storage_url,
            mime_type=mime_type,
            duration_ms=duration_ms,
            width=width,
            height=height,
            file_size=file_size,
            checksum=checksum,
            asset_metadata=asset_metadata or {},
        )
        self.session.add(record)
        self.session.flush()
        return asset_from_record(record)

    def get_asset(self, asset_id: str) -> Asset | None:
        record = self.session.get(AssetRecord, asset_id)
        return asset_from_record(record) if record else None

    def list_assets(self, run_id: str) -> list[Asset]:
        statement = (
            select(AssetRecord)
            .where(AssetRecord.run_id == run_id)
            .order_by(AssetRecord.created_at, AssetRecord.id)
        )
        return [asset_from_record(record) for record in self.session.scalars(statement)]


class CollectionAddOutcome(StrEnum):
    """Why an ``add_item`` call ended the way it did."""

    CREATED = "created"
    EXISTING = "existing"
    COLLECTION_NOT_FOUND = "collection_not_found"
    ASSET_NOT_FOUND = "asset_not_found"


class ReviewAttachmentOutcome(StrEnum):
    """Why a ``mount_review_attachment`` call ended the way it did."""

    CREATED = "created"
    EXISTING = "existing"
    REQUEST_NOT_FOUND = "request_not_found"
    ASSET_NOT_FOUND = "asset_not_found"


class CollectionImportOutcome(StrEnum):
    IMPORTED = "imported"
    COLLECTION_NOT_FOUND = "collection_not_found"
    RUN_NOT_FOUND = "run_not_found"


class AssetReuseOutcome(StrEnum):
    REUSED = "reused"
    ITEM_NOT_FOUND = "item_not_found"
    RUN_NOT_FOUND = "run_not_found"


class AssetCollectionRepository:
    """Project-scoped asset library: collections of reusable, rights-tagged entries.

    Entries reference run assets rather than owning bytes. That keeps one physical
    object per generated take while still letting the same take be reused by later
    runs, each copy carrying its own provenance note.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # collections
    # ------------------------------------------------------------------ #

    def create_collection(
        self,
        project_id: str,
        payload: AssetCollectionCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> AssetCollection | None:
        if self.session.get(ProjectRecord, project_id) is None:
            return None
        record = AssetCollectionRecord(
            id=f"acol_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            project_id=project_id,
            kind=payload.kind,
            name=payload.name,
            description=payload.description,
            source_run_id=payload.source_run_id,
            is_default=payload.is_default,
            status="active",
            collection_metadata=payload.collection_metadata,
        )
        self.session.add(record)
        self.session.flush()
        return asset_collection_from_record(record, item_count=0)

    def list_collections(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        include_archived: bool = False,
    ) -> list[AssetCollection] | None:
        if self.session.get(ProjectRecord, project_id) is None:
            return None
        statement = select(AssetCollectionRecord).where(
            AssetCollectionRecord.project_id == project_id
        )
        if kind is not None:
            statement = statement.where(AssetCollectionRecord.kind == kind)
        if not include_archived:
            statement = statement.where(AssetCollectionRecord.status == "active")
        statement = statement.order_by(AssetCollectionRecord.created_at, AssetCollectionRecord.id)
        records = list(self.session.scalars(statement))
        counts = self._item_counts([record.id for record in records])
        return [
            asset_collection_from_record(record, item_count=counts.get(record.id, 0))
            for record in records
        ]

    def get_collection(
        self,
        collection_id: str,
        *,
        with_items: bool = False,
    ) -> AssetCollection | None:
        record = self.session.get(AssetCollectionRecord, collection_id)
        if record is None:
            return None
        items = self.list_items(collection_id) if with_items else []
        item_count = (
            len(items) if with_items else self._item_counts([collection_id]).get(collection_id, 0)
        )
        return asset_collection_from_record(record, item_count=item_count, items=items)

    def update_collection(
        self,
        collection_id: str,
        payload: AssetCollectionUpdate,
    ) -> AssetCollection | None:
        record = self.session.get(AssetCollectionRecord, collection_id)
        if record is None:
            return None
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, key, value)
        self.session.flush()
        return asset_collection_from_record(
            record,
            item_count=self._item_counts([collection_id]).get(collection_id, 0),
        )

    def project_library(self, project_id: str) -> ProjectAssetLibrary | None:
        collections = self.list_collections(project_id)
        if collections is None:
            return None
        detailed = [
            self.get_collection(collection.collection_id, with_items=True) or collection
            for collection in collections
        ]
        by_kind: dict[str, int] = {}
        for collection in detailed:
            by_kind[collection.kind] = by_kind.get(collection.kind, 0) + 1
        return ProjectAssetLibrary(
            project_id=project_id,
            collections=detailed,
            totals={
                "collection_count": len(detailed),
                "item_count": sum(collection.item_count for collection in detailed),
                "by_kind": by_kind,
            },
        )

    # ------------------------------------------------------------------ #
    # items
    # ------------------------------------------------------------------ #

    def list_items(
        self,
        collection_id: str,
        *,
        include_removed: bool = False,
        role: str | None = None,
    ) -> list[AssetCollectionItem]:
        statement = select(AssetCollectionItemRecord).where(
            AssetCollectionItemRecord.collection_id == collection_id
        )
        if not include_removed:
            statement = statement.where(AssetCollectionItemRecord.status == "active")
        if role is not None:
            statement = statement.where(AssetCollectionItemRecord.usage_role == role)
        statement = statement.order_by(
            AssetCollectionItemRecord.created_at,
            AssetCollectionItemRecord.id,
        )
        records = list(self.session.scalars(statement))
        assets = self._assets_for([record.asset_id for record in records])
        return [
            asset_collection_item_from_record(record, asset=assets.get(record.asset_id))
            for record in records
        ]

    def get_item(self, item_id: str) -> AssetCollectionItem | None:
        record = self.session.get(AssetCollectionItemRecord, item_id)
        if record is None:
            return None
        asset = self.session.get(AssetRecord, record.asset_id)
        return asset_collection_item_from_record(
            record,
            asset=asset_from_record(asset) if asset else None,
        )

    def add_item(
        self,
        collection_id: str,
        payload: AssetCollectionItemAdd,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[CollectionAddOutcome, AssetCollectionItem | None]:
        collection = self.session.get(AssetCollectionRecord, collection_id)
        if collection is None:
            return CollectionAddOutcome.COLLECTION_NOT_FOUND, None
        asset_record = self.session.get(AssetRecord, payload.asset_id)
        if asset_record is None:
            return CollectionAddOutcome.ASSET_NOT_FOUND, None
        existing = self.session.scalars(
            select(AssetCollectionItemRecord).where(
                AssetCollectionItemRecord.collection_id == collection_id,
                AssetCollectionItemRecord.asset_id == payload.asset_id,
            )
        ).first()
        if existing is not None:
            # Adding twice is a no-op so retries and bulk imports stay idempotent.
            return CollectionAddOutcome.EXISTING, asset_collection_item_from_record(
                existing,
                asset=asset_from_record(asset_record),
            )
        record = AssetCollectionItemRecord(
            id=f"aitem_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            collection_id=collection_id,
            asset_id=payload.asset_id,
            usage_role=payload.role,
            note=payload.note,
            provenance=_provenance_payload(payload.provenance),
            license_terms=_license_payload(payload.license),
            status="active",
        )
        self.session.add(record)
        self.session.flush()
        return CollectionAddOutcome.CREATED, asset_collection_item_from_record(
            record,
            asset=asset_from_record(asset_record),
        )

    def update_item(
        self,
        item_id: str,
        payload: AssetCollectionItemUpdate,
    ) -> AssetCollectionItem | None:
        record = self.session.get(AssetCollectionItemRecord, item_id)
        if record is None:
            return None
        updates = payload.model_dump(exclude_unset=True)
        if "role" in updates:
            record.usage_role = updates["role"]
        if "note" in updates:
            record.note = updates["note"]
        if "status" in updates:
            record.status = updates["status"]
        if "license" in updates:
            license_terms = payload.license or AssetLicense()
            record.license_terms = _license_payload(license_terms)
        self.session.flush()
        asset = self.session.get(AssetRecord, record.asset_id)
        return asset_collection_item_from_record(
            record,
            asset=asset_from_record(asset) if asset else None,
        )

    def remove_item(self, item_id: str) -> AssetCollectionItem | None:
        """Archive an entry instead of deleting it: provenance must survive."""
        record = self.session.get(AssetCollectionItemRecord, item_id)
        if record is None:
            return None
        record.status = "removed"
        self.session.flush()
        asset = self.session.get(AssetRecord, record.asset_id)
        return asset_collection_item_from_record(
            record,
            asset=asset_from_record(asset) if asset else None,
        )

    def import_from_run(
        self,
        collection_id: str,
        payload: AssetCollectionImportRequest,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> tuple[CollectionImportOutcome, AssetCollectionImportResult | None]:
        """Promote a run's assets into the collection.

        Every source asset is attributed back to its run, so an operator can always
        tell which take a reusable entry came from.
        """
        if self.session.get(AssetCollectionRecord, collection_id) is None:
            return CollectionImportOutcome.COLLECTION_NOT_FOUND, None
        run_record = self.session.get(ProductionRunRecord, payload.run_id)
        if run_record is None:
            return CollectionImportOutcome.RUN_NOT_FOUND, None

        statement = select(AssetRecord).where(AssetRecord.run_id == payload.run_id)
        if payload.asset_ids:
            statement = statement.where(AssetRecord.id.in_(payload.asset_ids))
        if payload.asset_types:
            statement = statement.where(AssetRecord.asset_type.in_(payload.asset_types))
        statement = statement.order_by(AssetRecord.created_at, AssetRecord.id)

        added_item_ids: list[str] = []
        skipped_asset_ids: list[str] = []
        license_terms = _license_payload(payload.license)
        for asset_record in self.session.scalars(statement):
            existing = self.session.scalars(
                select(AssetCollectionItemRecord).where(
                    AssetCollectionItemRecord.collection_id == collection_id,
                    AssetCollectionItemRecord.asset_id == asset_record.id,
                )
            ).first()
            if existing is not None:
                skipped_asset_ids.append(asset_record.id)
                continue
            record = AssetCollectionItemRecord(
                id=f"aitem_{uuid4().hex[:12]}",
                tenant_id=tenant_id,
                collection_id=collection_id,
                asset_id=asset_record.id,
                usage_role=payload.role,
                note=payload.note,
                provenance=_provenance_payload(
                    AssetProvenance(
                        source="generated" if asset_record.provider else "uploaded",
                        provider=asset_record.provider,
                        tool_key=asset_record.tool_key,
                        source_run_id=payload.run_id,
                        source_asset_id=asset_record.id,
                        origin_note="imported from run",
                    )
                ),
                license_terms=license_terms,
                status="active",
            )
            self.session.add(record)
            added_item_ids.append(record.id)
        self.session.flush()
        return CollectionImportOutcome.IMPORTED, AssetCollectionImportResult(
            collection_id=collection_id,
            run_id=payload.run_id,
            imported_count=len(added_item_ids),
            added_item_ids=added_item_ids,
            skipped_asset_ids=skipped_asset_ids,
        )

    # ------------------------------------------------------------------ #
    # reuse
    # ------------------------------------------------------------------ #

    def reuse_into_run(
        self,
        run_id: str,
        *,
        item_id: str,
        asset_type: str | None = None,
        stage_run_id: str | None = None,
        note: str | None = None,
    ) -> tuple[AssetReuseOutcome, Asset | None, AssetCollectionItem | None]:
        """Copy a library entry into a run as a run-owned asset row.

        The bytes are shared (the copy keeps the original ``storage_url``), so reuse
        costs nothing but still gives the target run a first-class asset it can feed
        into composition without reaching across runs.
        """
        item = self.session.get(AssetCollectionItemRecord, item_id)
        if item is None:
            return AssetReuseOutcome.ITEM_NOT_FOUND, None, None
        if self.session.get(ProductionRunRecord, run_id) is None:
            return AssetReuseOutcome.RUN_NOT_FOUND, None, None
        source = self.session.get(AssetRecord, item.asset_id)
        if source is None:
            return AssetReuseOutcome.ITEM_NOT_FOUND, None, None

        provenance = AssetProvenance(
            source="imported",
            provider=source.provider,
            tool_key=source.tool_key,
            source_run_id=source.run_id,
            source_asset_id=source.id,
            origin_note=note or f"reused from collection {item.collection_id}",
        )
        copied = AssetRecord(
            id=f"asset_{uuid4().hex[:12]}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            asset_type=asset_type or source.asset_type,
            provider=source.provider,
            tool_key=source.tool_key,
            storage_url=source.storage_url,
            mime_type=source.mime_type,
            duration_ms=source.duration_ms,
            width=source.width,
            height=source.height,
            file_size=source.file_size,
            checksum=source.checksum,
            asset_metadata={
                **(source.asset_metadata or {}),
                "reused_from": {
                    "collection_id": item.collection_id,
                    "item_id": item.id,
                    "asset_id": source.id,
                    "run_id": source.run_id,
                },
                # Rights travel with the copy: whoever later renders this run can
                # still tell where the material came from and what it may be used for.
                "library_provenance": provenance.model_dump(mode="json"),
                "library_license": dict(item.license_terms or {}),
            },
        )
        self.session.add(copied)
        self.session.flush()
        return (
            AssetReuseOutcome.REUSED,
            asset_from_record(copied),
            asset_collection_item_from_record(item, asset=asset_from_record(source)),
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _item_counts(self, collection_ids: list[str]) -> dict[str, int]:
        if not collection_ids:
            return {}
        statement = (
            select(
                AssetCollectionItemRecord.collection_id,
                func.count(AssetCollectionItemRecord.id),
            )
            .where(
                AssetCollectionItemRecord.collection_id.in_(collection_ids),
                AssetCollectionItemRecord.status == "active",
            )
            .group_by(AssetCollectionItemRecord.collection_id)
        )
        return {row[0]: int(row[1]) for row in self.session.execute(statement)}

    def _assets_for(self, asset_ids: list[str]) -> dict[str, Asset]:
        unique_ids = sorted({asset_id for asset_id in asset_ids if asset_id})
        if not unique_ids:
            return {}
        statement = select(AssetRecord).where(AssetRecord.id.in_(unique_ids))
        return {record.id: asset_from_record(record) for record in self.session.scalars(statement)}


class ToolInvocationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_invocation(
        self,
        *,
        run_id: str,
        tool_key: str,
        provider: str,
        request_payload: dict,
        status: ToolInvocationStatus = ToolInvocationStatus.QUEUED,
        stage_run_id: str | None = None,
        response_payload: dict | None = None,
        external_task_id: str | None = None,
        cost: float | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_category: str | None = None,
        error_taxonomy_code: str | None = None,
        error_retryable: bool | None = None,
        error_user_message: str | None = None,
        error_remediation: str | None = None,
    ) -> ToolInvocation:
        record = ToolInvocationRecord(
            id=f"toolinv_{uuid4().hex[:12]}",
            run_id=run_id,
            stage_run_id=stage_run_id,
            tool_key=tool_key,
            provider=provider,
            request_payload=request_payload,
            response_payload=response_payload or {},
            status=status.value,
            external_task_id=external_task_id,
            cost=cost,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
            error_category=error_category,
            error_taxonomy_code=error_taxonomy_code,
            error_retryable=error_retryable,
            error_user_message=error_user_message,
            error_remediation=error_remediation,
        )
        self.session.add(record)
        self.session.flush()
        return tool_invocation_from_record(record)

    def get_invocation(self, invocation_id: str) -> ToolInvocation | None:
        record = self.session.get(ToolInvocationRecord, invocation_id)
        return tool_invocation_from_record(record) if record else None

    def get_invocation_by_external_task_id(
        self,
        *,
        provider: str,
        external_task_id: str,
    ) -> ToolInvocation | None:
        statement = (
            select(ToolInvocationRecord)
            .where(ToolInvocationRecord.provider == provider)
            .where(ToolInvocationRecord.external_task_id == external_task_id)
            .order_by(ToolInvocationRecord.created_at.desc(), ToolInvocationRecord.id.desc())
        )
        record = self.session.scalar(statement)
        return tool_invocation_from_record(record) if record else None

    def list_invocations(self, run_id: str) -> list[ToolInvocation]:
        statement = (
            select(ToolInvocationRecord)
            .where(ToolInvocationRecord.run_id == run_id)
            .order_by(ToolInvocationRecord.created_at, ToolInvocationRecord.id)
        )
        return [tool_invocation_from_record(record) for record in self.session.scalars(statement)]

    def update_invocation(
        self,
        invocation_id: str,
        *,
        status: ToolInvocationStatus | None = None,
        response_payload: dict | None = None,
        external_task_id: str | None = None,
        cost: float | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_category: str | None = None,
        error_taxonomy_code: str | None = None,
        error_retryable: bool | None = None,
        error_user_message: str | None = None,
        error_remediation: str | None = None,
    ) -> ToolInvocation | None:
        record = self.session.get(ToolInvocationRecord, invocation_id)
        if record is None:
            return None
        if status is not None:
            record.status = status.value
        if response_payload is not None:
            record.response_payload = response_payload
        if external_task_id is not None:
            record.external_task_id = external_task_id
        if cost is not None:
            record.cost = cost
        if duration_ms is not None:
            record.duration_ms = duration_ms
        if error_code is not None:
            record.error_code = error_code
        if error_message is not None:
            record.error_message = error_message
        if error_category is not None:
            record.error_category = error_category
        if error_taxonomy_code is not None:
            record.error_taxonomy_code = error_taxonomy_code
        if error_retryable is not None:
            record.error_retryable = error_retryable
        if error_user_message is not None:
            record.error_user_message = error_user_message
        if error_remediation is not None:
            record.error_remediation = error_remediation
        self.session.flush()
        return tool_invocation_from_record(record)


class BackgroundJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(
        self,
        *,
        run_id: str,
        payload: BackgroundJobCreate,
        tenant_id: str = DEFAULT_TENANT_ID,
        queue: str = "media",
    ) -> BackgroundJob:
        record = BackgroundJobRecord(
            id=f"job_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            run_id=run_id,
            stage_run_id=payload.stage_run_id,
            job_type=payload.job_type,
            status=BackgroundJobStatus.QUEUED.value,
            progress=0,
            request_payload=payload.request_payload,
            result_payload={},
            attempt=1,
            max_attempts=payload.max_attempts,
            queue=queue,
            priority=payload.priority,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return background_job_from_record(record)

    def get_job(self, job_id: str) -> BackgroundJob | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        return background_job_from_record(record) if record else None

    def list_jobs(self, run_id: str) -> list[BackgroundJob]:
        statement = (
            select(BackgroundJobRecord)
            .where(BackgroundJobRecord.run_id == run_id)
            .order_by(BackgroundJobRecord.created_at, BackgroundJobRecord.id)
        )
        return [background_job_from_record(record) for record in self.session.scalars(statement)]

    def list_processable_jobs(self, *, limit: int = 10) -> list[BackgroundJob]:
        statuses = [
            BackgroundJobStatus.QUEUED.value,
            BackgroundJobStatus.RUNNING.value,
            BackgroundJobStatus.WAITING_PROVIDER.value,
        ]
        statement = (
            select(BackgroundJobRecord)
            .where(BackgroundJobRecord.status.in_(statuses))
            .order_by(BackgroundJobRecord.created_at)
            .limit(max(1, min(limit, 100)))
        )
        return [background_job_from_record(record) for record in self.session.scalars(statement)]

    def claim_processable_jobs(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
        queues: list[str] | None = None,
    ) -> list[BackgroundJob]:
        statuses = [
            BackgroundJobStatus.QUEUED.value,
            BackgroundJobStatus.RUNNING.value,
            BackgroundJobStatus.WAITING_PROVIDER.value,
        ]
        now = datetime.now(UTC).replace(tzinfo=None)
        statement = (
            select(BackgroundJobRecord)
            .where(BackgroundJobRecord.status.in_(statuses))
            # A real lease: rows whose lease has not expired (and that belong to
            # another worker) are untouchable. NULL lease rows are pre-migration
            # or unleased — treat them as expired so they stay claimable.
            .where(
                or_(
                    BackgroundJobRecord.locked_by.is_(None),
                    BackgroundJobRecord.locked_by == worker_id,
                    BackgroundJobRecord.lease_expires_at.is_(None),
                    BackgroundJobRecord.lease_expires_at < now,
                )
            )
            # Delayed retries stay invisible until their backoff elapses.
            .where(
                or_(
                    BackgroundJobRecord.next_run_at.is_(None),
                    BackgroundJobRecord.next_run_at <= now,
                )
            )
            .order_by(BackgroundJobRecord.priority, BackgroundJobRecord.created_at)
            .limit(max(1, min(limit, 100)))
            .with_for_update(skip_locked=True)
        )
        if queues is not None:
            statement = statement.where(BackgroundJobRecord.queue.in_(queues))
        records = list(self.session.scalars(statement))
        for record in records:
            record.locked_by = worker_id
            record.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
        self.session.flush()
        return [background_job_from_record(record) for record in records]

    def update_job(
        self,
        job_id: str,
        *,
        status: BackgroundJobStatus | None = None,
        progress: int | None = None,
        result_payload: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        error_category: str | None = None,
        error_taxonomy_code: str | None = None,
        error_retryable: bool | None = None,
        error_user_message: str | None = None,
        error_remediation: str | None = None,
        attempt: int | None = None,
        locked_by: str | None = None,
        lease_expires_at: datetime | None = None,
        started_at: datetime | None = None,
        next_run_at: datetime | None = None,
        clear_next_run_at: bool = False,
    ) -> BackgroundJob | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        if record is None:
            return None
        if status is not None:
            record.status = status.value
        if progress is not None:
            record.progress = max(0, min(progress, 100))
        if result_payload is not None:
            record.result_payload = result_payload
        if error_code is not None:
            record.error_code = error_code
        if error_message is not None:
            record.error_message = error_message
        if error_category is not None:
            record.error_category = error_category
        if error_taxonomy_code is not None:
            record.error_taxonomy_code = error_taxonomy_code
        if error_retryable is not None:
            record.error_retryable = error_retryable
        if error_user_message is not None:
            record.error_user_message = error_user_message
        if error_remediation is not None:
            record.error_remediation = error_remediation
        if attempt is not None:
            record.attempt = attempt
        if locked_by is not None:
            record.locked_by = locked_by
        if lease_expires_at is not None:
            record.lease_expires_at = lease_expires_at
        if started_at is not None:
            record.started_at = started_at
        if next_run_at is not None:
            record.next_run_at = next_run_at
        if clear_next_run_at:
            record.next_run_at = None
        self.session.flush()
        self.session.refresh(record)
        return background_job_from_record(record)

    def retry_job(self, job_id: str) -> BackgroundJob | None:
        record = self.session.get(BackgroundJobRecord, job_id)
        if record is None:
            return None
        if record.attempt >= record.max_attempts:
            return background_job_from_record(record)
        record.status = BackgroundJobStatus.QUEUED.value
        record.progress = 0
        record.error_code = None
        record.error_message = None
        record.error_category = None
        record.error_taxonomy_code = None
        record.error_retryable = None
        record.error_user_message = None
        record.error_remediation = None
        record.locked_by = None
        record.lease_expires_at = None
        record.started_at = None
        record.next_run_at = None
        record.attempt += 1
        self.session.flush()
        self.session.refresh(record)
        return background_job_from_record(record)

    def requeue_dead_job(self, job_id: str) -> BackgroundJob | None:
        """Put a dead-lettered job back in the queue with a fresh attempt count.

        Mirrors the webhook requeue semantics: the operator has (hopefully)
        fixed whatever exhausted the retries, so the job starts over instead of
        inheriting an attempt count that would dead-letter again immediately.
        """
        record = self.session.get(BackgroundJobRecord, job_id)
        if record is None:
            return None
        if record.status != BackgroundJobStatus.DEAD.value:
            return background_job_from_record(record)
        record.status = BackgroundJobStatus.QUEUED.value
        record.progress = 0
        record.error_code = None
        record.error_message = None
        record.error_category = None
        record.error_taxonomy_code = None
        record.error_retryable = None
        record.error_user_message = None
        record.error_remediation = None
        record.locked_by = None
        record.lease_expires_at = None
        record.started_at = None
        record.next_run_at = None
        record.attempt = 1
        self.session.flush()
        self.session.refresh(record)
        return background_job_from_record(record)


class UsageLedgerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def append_entry(
        self,
        *,
        run_id: str,
        source_type: str,
        source_id: str,
        operation: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        stage_run_id: str | None = None,
        provider: str | None = None,
        tool_key: str | None = None,
        estimated_cost: float | None = None,
        actual_cost: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
        asset_bytes: int | None = None,
        usage_unit: str | None = None,
        usage_quantity: float | None = None,
        entry_metadata: dict | None = None,
    ) -> UsageLedgerEntry:
        record = UsageLedgerRecord(
            id=f"usage_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            run_id=run_id,
            stage_run_id=stage_run_id,
            source_type=source_type,
            source_id=source_id,
            provider=provider,
            tool_key=tool_key,
            operation=operation,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            asset_bytes=asset_bytes,
            usage_unit=usage_unit,
            usage_quantity=usage_quantity,
            entry_metadata=entry_metadata or {},
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return usage_ledger_entry_from_record(record)

    def list_entries(self, run_id: str) -> list[UsageLedgerEntry]:
        statement = (
            select(UsageLedgerRecord)
            .where(UsageLedgerRecord.run_id == run_id)
            .order_by(UsageLedgerRecord.created_at, UsageLedgerRecord.id)
        )
        return [
            usage_ledger_entry_from_record(record)
            for record in self.session.scalars(statement)
        ]


def project_from_record(record: ProjectRecord) -> Project:
    return Project(
        project_id=record.id,
        title=record.title,
        description=record.description,
        default_language=record.default_language,
        default_aspect_ratio=record.default_aspect_ratio,
        default_style_preset_id=record.default_style_preset_id,
        team_id=record.team_id,
    )


def brand_kit_from_record(record: BrandKitRecord) -> BrandKit:
    return BrandKit(
        brand_kit_id=record.id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        name=record.name,
        description=record.description,
        logo_asset_id=record.logo_asset_id,
        color_palette=record.color_palette or {},
        typography=record.typography or {},
        safe_area=record.safe_area or {},
        brand_voice=record.brand_voice or {},
        banned_terms=record.banned_terms or [],
        brand_metadata=record.brand_metadata or {},
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def style_preset_from_record(record: StylePresetRecord) -> StylePreset:
    return StylePreset(
        style_preset_id=record.id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        brand_kit_id=record.brand_kit_id,
        name=record.name,
        description=record.description,
        style_key=record.style_key,
        is_default=record.is_default,
        aspect_ratio=record.aspect_ratio,
        subtitle_style=record.subtitle_style or {},
        visual_style=record.visual_style or {},
        tone=record.tone or {},
        prompt_guidelines=record.prompt_guidelines or {},
        render_defaults=record.render_defaults or {},
        provider_preferences=record.provider_preferences or {},
        guardrails=record.guardrails or {},
        preset_metadata=record.preset_metadata or {},
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def provider_credential_from_record(
    record: ProviderCredentialRecord,
    *,
    resolved: dict[str, str] | None = None,
    source: ProviderSecretSource | None = None,
) -> ProviderCredential:
    """Project a credential row for the API.

    ``resolved`` is the readable plaintext keyed by name, supplied by the
    repository that holds the vault. It defaults to ``None``, which means "the
    stored values *are* the plaintext" — the pre-V8.4 arrangement, and still the
    correct reading when encryption is disabled. A name absent from ``resolved``
    while present on the row is a reference the current key cannot open; its
    preview is blank rather than showing the reference itself, because a preview
    is supposed to be a glimpse of the secret and ``sec_ab12`` is not one.
    """
    stored = record.secret_values or {}
    if resolved is None:
        resolved = {
            key: value
            for key, value in stored.items()
            if isinstance(value, str) and value
        }
    return ProviderCredential(
        credential_id=record.id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        provider=record.provider,
        label=record.label,
        credential_type=record.credential_type,
        status=ProviderCredentialStatus(record.status),
        secret_names=sorted(stored.keys()),
        secret_preview={
            key: _secret_preview(resolved[key])
            for key in sorted(resolved)
        },
        secret_source=source or ProviderSecretSource.NONE,
        encrypted=source == ProviderSecretSource.VAULT,
        quota=record.quota or {},
        credential_metadata=record.credential_metadata or {},
        last_check_status=record.last_check_status,
        last_check_message=record.last_check_message,
        last_checked_at=record.last_checked_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _encrypted_secret_summary(record: EncryptedSecretRecord) -> EncryptedSecretSummary:
    return EncryptedSecretSummary(
        secret_id=record.id,
        name=record.name,
        algorithm=record.algorithm,
        key_version=record.key_version,
        rotated_at=record.rotated_at,
    )


def _secret_preview(value: object) -> str:
    secret = str(value or "")
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:3]}...{secret[-4:]}"


def production_run_from_record(record: ProductionRunRecord) -> ProductionRun:
    stages = sorted(record.stages, key=lambda stage: stage.stage_order)
    return ProductionRun(
        run_id=record.id,
        project_id=record.project_id,
        pipeline_key=record.pipeline_key,
        pipeline_version_id=record.pipeline_version_id,
        pipeline_version=record.pipeline_version,
        pipeline_dsl_version=record.pipeline_dsl_version,
        pipeline_definition_snapshot=record.pipeline_definition_snapshot,
        status=RunStatus(record.status),
        current_stage_key=record.current_stage_key,
        input_payload=record.input_payload,
        estimated_cost=float(record.estimated_cost) if record.estimated_cost is not None else None,
        actual_cost=float(record.actual_cost) if record.actual_cost is not None else None,
        stages=[
            StageRun(
                stage_run_id=stage.id,
                stage_key=stage.stage_key,
                order=stage.stage_order,
                status=StageStatus(stage.status),
                requires_human_review=stage.requires_human_review,
                output_artifact_types=stage.output_artifact_types,
                invalidation=stage_invalidation_from_record(stage),
            )
            for stage in stages
        ],
    )


def _stable_json_hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def artifact_from_record(record: ArtifactRecord) -> Artifact:
    return Artifact(
        artifact_id=record.id,
        run_id=record.run_id,
        stage_key=record.stage_key,
        artifact_type=record.artifact_type,
        version=record.version,
        schema_version=record.schema_version,
        schema_key=record.schema_key,
        producer=record.producer,
        lineage=record.lineage,
        derived_from=record.derived_from,
        is_current=record.is_current,
        payload=record.payload,
    )


def event_from_record(record: EventLogRecord) -> EventLog:
    return EventLog(
        event_id=record.id,
        run_id=record.run_id,
        sequence=record.sequence,
        event_type=record.event_type,
        source_type=EventSourceType(record.source_type),
        source_id=record.source_id,
        stage_key=record.stage_key,
        stage_run_id=record.stage_run_id,
        tool_invocation_id=record.tool_invocation_id,
        severity=EventSeverity(record.severity),
        message=record.message,
        payload=record.payload,
        created_at=record.created_at,
    )


def asset_from_record(record: AssetRecord) -> Asset:
    return Asset(
        asset_id=record.id,
        run_id=record.run_id,
        stage_run_id=record.stage_run_id,
        asset_type=record.asset_type,
        provider=record.provider,
        tool_key=record.tool_key,
        storage_url=record.storage_url,
        mime_type=record.mime_type,
        duration_ms=record.duration_ms,
        width=record.width,
        height=record.height,
        file_size=record.file_size,
        checksum=record.checksum,
        asset_metadata=record.asset_metadata,
    )


def _provenance_payload(provenance: AssetProvenance) -> dict:
    return provenance.model_dump()


def _license_payload(license_terms: AssetLicense) -> dict:
    payload = license_terms.model_dump()
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, datetime):
        # JSON columns hold naive UTC, same as every other timestamp we persist.
        normalized = store_utc(expires_at)
        payload["expires_at"] = normalized.isoformat() if normalized is not None else None
    return payload


def asset_collection_from_record(
    record: AssetCollectionRecord,
    *,
    item_count: int = 0,
    items: list[AssetCollectionItem] | None = None,
) -> AssetCollection:
    return AssetCollection(
        collection_id=record.id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        kind=record.kind,
        name=record.name,
        description=record.description,
        source_run_id=record.source_run_id,
        is_default=record.is_default,
        status=record.status,
        collection_metadata=record.collection_metadata or {},
        item_count=item_count,
        items=items or [],
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def asset_collection_item_from_record(
    record: AssetCollectionItemRecord,
    *,
    asset: Asset | None = None,
) -> AssetCollectionItem:
    return AssetCollectionItem(
        item_id=record.id,
        collection_id=record.collection_id,
        asset_id=record.asset_id,
        role=record.usage_role,
        note=record.note,
        provenance=AssetProvenance(**(record.provenance or {})),
        license=AssetLicense(**(record.license_terms or {})),
        status=record.status,
        asset=asset,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def tool_invocation_from_record(record: ToolInvocationRecord) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=record.id,
        run_id=record.run_id,
        stage_run_id=record.stage_run_id,
        tool_key=record.tool_key,
        provider=record.provider,
        request_payload=record.request_payload,
        response_payload=record.response_payload,
        status=ToolInvocationStatus(record.status),
        cost=float(record.cost) if record.cost is not None else None,
        duration_ms=record.duration_ms,
        external_task_id=record.external_task_id,
        error_code=record.error_code,
        error_message=record.error_message,
        error_category=record.error_category,
        error_taxonomy_code=record.error_taxonomy_code,
        error_retryable=record.error_retryable,
        error_user_message=record.error_user_message,
        error_remediation=record.error_remediation,
    )


def background_job_from_record(record: BackgroundJobRecord) -> BackgroundJob:
    return BackgroundJob(
        job_id=record.id,
        run_id=record.run_id,
        stage_run_id=record.stage_run_id,
        job_type=record.job_type,
        status=BackgroundJobStatus(record.status),
        progress=record.progress,
        request_payload=record.request_payload,
        result_payload=record.result_payload,
        error_code=record.error_code,
        error_message=record.error_message,
        error_category=record.error_category,
        error_taxonomy_code=record.error_taxonomy_code,
        error_retryable=record.error_retryable,
        error_user_message=record.error_user_message,
        error_remediation=record.error_remediation,
        attempt=record.attempt,
        max_attempts=record.max_attempts,
        locked_by=record.locked_by,
        queue=record.queue,
        priority=record.priority,
        started_at=record.started_at,
        lease_expires_at=record.lease_expires_at,
        next_run_at=record.next_run_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def usage_ledger_entry_from_record(record: UsageLedgerRecord) -> UsageLedgerEntry:
    return UsageLedgerEntry(
        ledger_entry_id=record.id,
        run_id=record.run_id,
        stage_run_id=record.stage_run_id,
        source_type=record.source_type,
        source_id=record.source_id,
        provider=record.provider,
        tool_key=record.tool_key,
        operation=record.operation,
        estimated_cost=(
            float(record.estimated_cost) if record.estimated_cost is not None else None
        ),
        actual_cost=float(record.actual_cost) if record.actual_cost is not None else None,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        duration_ms=record.duration_ms,
        asset_bytes=record.asset_bytes,
        usage_unit=record.usage_unit,
        usage_quantity=(
            float(record.usage_quantity) if record.usage_quantity is not None else None
        ),
        entry_metadata=record.entry_metadata,
        created_at=record.created_at,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a stored timestamp so comparisons work on both MySQL and SQLite."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_overdue(due_at: datetime | None, *, now: datetime | None = None) -> bool:
    normalized = as_utc(due_at)
    if normalized is None:
        return False
    return normalized < (now or _utcnow())


def store_utc(value: datetime | None) -> datetime | None:
    """Convert an inbound timestamp to the naive UTC form the columns hold.

    Neither MySQL ``DATETIME`` nor SQLite keeps an offset, and the drivers write
    the wall-clock fields verbatim — so an idempotency-safe ``+08:00`` input
    would otherwise be read back as if it were UTC and be eight hours off. The
    read side (``as_utc``) already assumes UTC, so normalizing on write keeps the
    instant intact.
    """
    normalized = as_utc(value)
    if normalized is None:
        return None
    return normalized.replace(tzinfo=None)


def review_request_from_record(record: ReviewRequestRecord) -> ReviewRequest:
    return ReviewRequest(
        review_request_id=record.id,
        project_id=record.project_id,
        run_id=record.run_id,
        stage_run_id=record.stage_run_id,
        stage_key=record.stage_key,
        pipeline_key=record.pipeline_key,
        iteration=record.iteration,
        status=ReviewRequestStatus(record.status),
        current_decision=(
            ReviewDecisionType(record.current_decision) if record.current_decision else None
        ),
        gate_type=record.gate_type,
        gates_stage_key=record.gates_stage_key,
        artifact_scope=[ReviewArtifactRef(**item) for item in (record.artifact_scope or [])],
        review_focus=list(record.review_focus or []),
        success_criteria=list(record.success_criteria or []),
        assignee_user_id=record.assignee_user_id,
        due_at=record.due_at,
        overdue=(
            record.status == ReviewRequestStatus.OPEN.value
            and is_overdue(record.due_at)
        ),
        opened_at=record.opened_at,
        closed_at=record.closed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def review_decision_from_record(record: ReviewDecisionRecord) -> ReviewDecision:
    return ReviewDecision(
        decision_id=record.id,
        review_request_id=record.review_request_id,
        run_id=record.run_id,
        sequence=record.sequence,
        round=record.round,
        decision=ReviewDecisionType(record.decision),
        actor_user_id=record.actor_user_id,
        actor_display_name=record.actor_display_name,
        comment=record.comment,
        reason_code=record.reason_code,
        artifact_refs=[ReviewArtifactRef(**item) for item in (record.artifact_refs or [])],
        created_at=record.created_at,
    )


def review_attachment_from_record(
    record: ReviewAttachmentRecord,
    *,
    asset: Asset | None = None,
) -> ReviewAttachment:
    return ReviewAttachment(
        attachment_id=record.id,
        review_request_id=record.review_request_id,
        run_id=record.run_id,
        asset_id=record.asset_id,
        note=record.note,
        added_by_user_id=record.added_by_user_id,
        added_by_display_name=record.added_by_display_name,
        asset=asset,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def review_comment_from_record(record: ReviewCommentRecord) -> ReviewComment:
    return ReviewComment(
        comment_id=record.id,
        review_request_id=record.review_request_id,
        run_id=record.run_id,
        stage_key=record.stage_key,
        artifact_id=record.artifact_id,
        artifact_version=record.artifact_version,
        author_user_id=record.author_user_id,
        author_display_name=record.author_display_name,
        severity=ReviewCommentSeverity(record.severity),
        body=record.body,
        status=ReviewCommentStatus(record.status),
        resolved_by_decision_id=record.resolved_by_decision_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def review_action_item_from_record(record: ReviewActionItemRecord) -> ReviewActionItem:
    return ReviewActionItem(
        action_item_id=record.id,
        review_request_id=record.review_request_id,
        decision_id=record.decision_id,
        run_id=record.run_id,
        kind=ReviewActionKind(record.kind),
        stage_key=record.stage_key,
        artifact_type=record.artifact_type,
        stage_run_id=record.stage_run_id,
        instruction=record.instruction,
        linked_comment_ids=list(record.linked_comment_ids or []),
        result_action=record.result_action,
        status=ReviewActionStatus(record.status),
        produced_artifact_id=record.produced_artifact_id,
        closed_at=record.closed_at,
        created_at=record.created_at,
    )


def stage_invalidation_from_record(record: StageRunRecord) -> dict | None:
    if record.invalidated_at is None:
        return None
    return {
        "invalidated_at": record.invalidated_at,
        "invalidated_by_stage_key": record.invalidated_by_stage_key,
        "invalidation_reason": record.invalidation_reason,
    }


# --- V7.3 outbound webhooks -------------------------------------------------


def webhook_subscription_from_record(record: WebhookSubscriptionRecord) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id=record.id,
        tenant_id=record.tenant_id,
        name=record.name,
        url=record.url,
        secret=record.secret,
        event_types=list(record.event_types or []),
        status=record.status,
        description=record.description,
        created_by=record.created_by,
        last_success_at=record.last_success_at,
        last_failure_at=record.last_failure_at,
        consecutive_failures=record.consecutive_failures or 0,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def webhook_delivery_from_record(record: WebhookDeliveryRecord) -> WebhookDelivery:
    return WebhookDelivery(
        delivery_id=record.id,
        tenant_id=record.tenant_id,
        subscription_id=record.subscription_id,
        event_id=record.event_id,
        event_type=record.event_type,
        run_id=record.run_id,
        status=record.status,
        attempt=record.attempt or 0,
        max_attempts=record.max_attempts or 1,
        payload=dict(record.payload or {}),
        signature=record.signature,
        response_status=record.response_status,
        response_body=record.response_body,
        error_message=record.error_message,
        next_attempt_at=record.next_attempt_at,
        delivered_at=record.delivered_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class WebhookSubscriptionOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    NOT_FOUND = "not_found"
    UNKNOWN_EVENT = "unknown_event"
    INVALID_URL = "invalid_url"


@dataclass
class WebhookSubscriptionResult:
    outcome: WebhookSubscriptionOutcome
    subscription: WebhookSubscription | None = None
    detail: str | None = None


@dataclass
class WebhookDeliveryAttempt:
    """The bookkeeping result of one delivery attempt."""

    delivery: WebhookDelivery | None = None
    subscription_disabled: bool = False
    remaining_attempts: int = 0

    @property
    def succeeded(self) -> bool:
        return self.delivery is not None and self.delivery.status == (
            WebhookDeliveryStatus.SUCCEEDED.value
        )

    @property
    def abandoned(self) -> bool:
        return self.delivery is not None and self.delivery.status == (
            WebhookDeliveryStatus.ABANDONED.value
        )


class WebhookRepository:
    """Tenant endpoints, their outbox, and the retry ledger (V7.3).

    The outbox is what makes delivery trustworthy: :meth:`enqueue_for_event`
    writes the delivery rows *inside* the transaction that appends the event, so
    an event that is recorded but not queued (or queued but not recorded) is not
    a state the database can reach. Everything after that is at-least-once.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- subscriptions ------------------------------------------------------

    def create_subscription(
        self,
        payload: WebhookSubscriptionCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        created_by: str | None = None,
    ) -> WebhookSubscriptionResult:
        event_types, error = self._resolve_event_types(payload.event_types)
        if error is not None:
            return WebhookSubscriptionResult(
                WebhookSubscriptionOutcome.UNKNOWN_EVENT,
                detail=error,
            )
        url_error = validate_webhook_url(normalize_webhook_url(payload.url))
        if url_error is not None:
            return WebhookSubscriptionResult(
                WebhookSubscriptionOutcome.INVALID_URL,
                detail=url_error,
            )
        record = WebhookSubscriptionRecord(
            id=f"whsub_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=payload.name,
            url=normalize_webhook_url(payload.url),
            secret=generate_webhook_secret(),
            event_types=event_types,
            status=payload.status,
            description=payload.description,
            created_by=created_by,
            consecutive_failures=0,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return WebhookSubscriptionResult(
            WebhookSubscriptionOutcome.CREATED,
            subscription=webhook_subscription_from_record(record),
        )

    def get_subscription(self, subscription_id: str) -> WebhookSubscription | None:
        record = self.session.get(WebhookSubscriptionRecord, subscription_id)
        return webhook_subscription_from_record(record) if record else None

    def list_subscriptions(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[WebhookSubscription], int]:
        bound = max(1, min(limit, 200))
        conditions = [WebhookSubscriptionRecord.tenant_id == tenant_id]
        if status is not None:
            conditions.append(WebhookSubscriptionRecord.status == status)
        statement = select(WebhookSubscriptionRecord).where(*conditions).limit(bound).offset(
            max(0, offset)
        )
        statement = _sorted(
            statement,
            WebhookSubscriptionRecord,
            sort,
            (WebhookSubscriptionRecord.created_at, WebhookSubscriptionRecord.id),
        )
        count_statement = select(func.count()).select_from(WebhookSubscriptionRecord).where(
            *conditions
        )
        records = list(self.session.scalars(statement))
        total = int(self.session.scalar(count_statement) or 0)
        return [webhook_subscription_from_record(record) for record in records], total

    def update_subscription(
        self,
        subscription_id: str,
        payload: WebhookSubscriptionUpdate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> WebhookSubscriptionResult:
        record = self._owned_record(subscription_id, tenant_id=tenant_id)
        if record is None:
            return WebhookSubscriptionResult(WebhookSubscriptionOutcome.NOT_FOUND)
        update_data = payload.model_dump(exclude_unset=True)
        if payload.event_types is not None:
            event_types, error = self._resolve_event_types(payload.event_types)
            if error is not None:
                return WebhookSubscriptionResult(
                    WebhookSubscriptionOutcome.UNKNOWN_EVENT,
                    detail=error,
                )
            record.event_types = event_types
        if payload.url is not None:
            url_error = validate_webhook_url(normalize_webhook_url(payload.url))
            if url_error is not None:
                return WebhookSubscriptionResult(
                    WebhookSubscriptionOutcome.INVALID_URL,
                    detail=url_error,
                )
            record.url = normalize_webhook_url(payload.url)
        if payload.name is not None:
            record.name = payload.name
        if "description" in update_data:
            record.description = payload.description
        if payload.status is not None:
            record.status = payload.status
            # Re-activating by hand clears the count that auto-pause accumulated,
            # otherwise the very next failure would disable it again immediately.
            if payload.status == WebhookSubscriptionStatus.ACTIVE.value:
                record.consecutive_failures = 0
        self.session.flush()
        self.session.refresh(record)
        return WebhookSubscriptionResult(
            WebhookSubscriptionOutcome.UPDATED,
            subscription=webhook_subscription_from_record(record),
        )

    def delete_subscription(
        self,
        subscription_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> WebhookSubscription | None:
        record = self._owned_record(subscription_id, tenant_id=tenant_id)
        if record is None:
            return None
        subscription = webhook_subscription_from_record(record)
        # Deliveries are deleted with the subscription: the foreign key means
        # they cannot outlive it, and a queue that will never be drained is not
        # worth keeping. The event log still holds the source events.
        for delivery in self.session.scalars(
            select(WebhookDeliveryRecord).where(
                WebhookDeliveryRecord.subscription_id == subscription_id
            )
        ):
            self.session.delete(delivery)
        self.session.delete(record)
        self.session.flush()
        return subscription

    def rotate_secret(
        self,
        subscription_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> WebhookSubscription | None:
        record = self._owned_record(subscription_id, tenant_id=tenant_id)
        if record is None:
            return None
        record.secret = generate_webhook_secret()
        self.session.flush()
        self.session.refresh(record)
        return webhook_subscription_from_record(record)

    def _resolve_event_types(self, values: list[str]) -> tuple[list[str], str | None]:
        """Validate against the published catalog, preserving the caller's order."""
        resolved: list[str] = []
        for value in values:
            try:
                event = WebhookEvent(value)
            except ValueError:
                return [], f"unknown webhook event type: {value}"
            if event.value not in resolved:
                resolved.append(event.value)
        return resolved, None

    def _owned_record(
        self,
        subscription_id: str,
        *,
        tenant_id: str,
    ) -> WebhookSubscriptionRecord | None:
        record = self.session.get(WebhookSubscriptionRecord, subscription_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    # --- outbox -------------------------------------------------------------

    def enqueue_for_event(
        self,
        *,
        event: EventLog,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[WebhookDelivery]:
        """Fan one internal event out to every subscribed endpoint.

        Called from ``EventRepository.append_event`` while the event row is still
        uncommitted, so the caller's own transaction decides whether both land.

        The event type filter is applied in Python rather than SQL: ``event_types``
        is a JSON array, and neither SQLite nor MySQL offers a containment
        predicate that is portable and indexable. Subscriptions per tenant are a
        handful of rows, so the scan is not the cost that matters here.
        """
        public_event = webhook_event_for_internal(event.event_type)
        if public_event is None:
            return []
        subscriptions = self.session.scalars(
            select(WebhookSubscriptionRecord).where(
                WebhookSubscriptionRecord.tenant_id == tenant_id,
                WebhookSubscriptionRecord.status == WebhookSubscriptionStatus.ACTIVE.value,
            )
        )
        matching = [
            subscription
            for subscription in subscriptions
            if public_event.value in (subscription.event_types or [])
        ]
        if not matching:
            return []
        deliveries: list[WebhookDelivery] = []
        for subscription in matching:
            delivery_id = f"whdel_{uuid4().hex[:12]}"
            record = WebhookDeliveryRecord(
                id=delivery_id,
                tenant_id=tenant_id,
                subscription_id=subscription.id,
                event_id=event.event_id,
                event_type=public_event.value,
                run_id=event.run_id,
                status=WebhookDeliveryStatus.PENDING.value,
                attempt=0,
                max_attempts=WEBHOOK_DEFAULT_MAX_ATTEMPTS,
                payload=build_webhook_payload(
                    delivery_id=delivery_id,
                    event_id=event.event_id,
                    event_type=public_event.value,
                    run_id=event.run_id,
                    project_id=self._project_id_for_run(event.run_id),
                    sequence=event.sequence,
                    occurred_at=event.created_at or datetime.now(UTC),
                    severity=event.severity.value,
                    message=event.message,
                    stage_key=event.stage_key,
                    data=event.payload,
                ),
                next_attempt_at=None,
            )
            self.session.add(record)
            deliveries.append(record)
        self.session.flush()
        for record in deliveries:
            self.session.refresh(record)
        return [webhook_delivery_from_record(record) for record in deliveries]

    def _project_id_for_run(self, run_id: str) -> str | None:
        statement = select(ProductionRunRecord.project_id).where(
            ProductionRunRecord.id == run_id
        )
        return self.session.scalar(statement)

    # --- delivery queue -----------------------------------------------------

    def get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        record = self.session.get(WebhookDeliveryRecord, delivery_id)
        return webhook_delivery_from_record(record) if record else None

    def list_deliveries(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        subscription_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[WebhookDelivery], int]:
        bound = max(1, min(limit, 200))
        conditions = [WebhookDeliveryRecord.tenant_id == tenant_id]
        if subscription_id is not None:
            conditions.append(WebhookDeliveryRecord.subscription_id == subscription_id)
        if status is not None:
            conditions.append(WebhookDeliveryRecord.status == status)
        statement = select(WebhookDeliveryRecord).where(*conditions).limit(bound).offset(
            max(0, offset)
        )
        statement = _sorted(
            statement,
            WebhookDeliveryRecord,
            sort,
            (WebhookDeliveryRecord.created_at.desc(), WebhookDeliveryRecord.id.desc()),
        )
        count_statement = (
            select(func.count()).select_from(WebhookDeliveryRecord).where(*conditions)
        )
        records = list(self.session.scalars(statement))
        total = int(self.session.scalar(count_statement) or 0)
        return [webhook_delivery_from_record(record) for record in records], total

    def claim_pending_deliveries(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[WebhookDelivery]:
        """Take ownership of due deliveries.

        ``delivering`` is reclaimable once its lease expires — that is how a
        delivery in flight during a worker crash comes back rather than sitting
        in a state nothing will ever leave.

        The join on an ``active`` subscription is what makes pausing mean
        something: a paused endpoint's queued events stay in the outbox,
        untouched and visible, and start flowing again the moment it is resumed.
        Deleting a subscription is different — the foreign key takes its
        deliveries with it.
        """
        moment = store_utc(now or datetime.now(UTC)) or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=max(1, lease_seconds))
        claimable = [
            WebhookDeliveryStatus.PENDING.value,
            WebhookDeliveryStatus.FAILED.value,
            WebhookDeliveryStatus.DELIVERING.value,
        ]
        statement = (
            select(WebhookDeliveryRecord)
            .join(
                WebhookSubscriptionRecord,
                WebhookSubscriptionRecord.id == WebhookDeliveryRecord.subscription_id,
            )
            .where(
                WebhookSubscriptionRecord.status == WebhookSubscriptionStatus.ACTIVE.value
            )
            .where(WebhookDeliveryRecord.status.in_(claimable))
            .where(
                or_(
                    WebhookDeliveryRecord.next_attempt_at.is_(None),
                    WebhookDeliveryRecord.next_attempt_at <= moment,
                )
            )
            .where(
                or_(
                    WebhookDeliveryRecord.locked_by.is_(None),
                    WebhookDeliveryRecord.locked_by == worker_id,
                    WebhookDeliveryRecord.status != WebhookDeliveryStatus.DELIVERING.value,
                    WebhookDeliveryRecord.updated_at < cutoff,
                )
            )
            .order_by(WebhookDeliveryRecord.created_at)
            .limit(max(1, min(limit, 100)))
            .with_for_update(skip_locked=True)
        )
        records = list(self.session.scalars(statement))
        for record in records:
            record.locked_by = worker_id
            record.status = WebhookDeliveryStatus.DELIVERING.value
        self.session.flush()
        return [webhook_delivery_from_record(record) for record in records]

    def mark_delivery_succeeded(
        self,
        delivery_id: str,
        *,
        attempt: int,
        signature: str,
        response_status: int | None,
        response_body: str | None,
        now: datetime | None = None,
    ) -> WebhookDelivery | None:
        record = self.session.get(WebhookDeliveryRecord, delivery_id)
        if record is None:
            return None
        record.status = WebhookDeliveryStatus.SUCCEEDED.value
        record.attempt = attempt
        record.signature = signature
        record.response_status = response_status
        record.response_body = response_body
        record.error_message = None
        record.next_attempt_at = None
        record.delivered_at = store_utc(now or datetime.now(UTC))
        record.locked_by = None
        self.session.flush()
        self.session.refresh(record)
        return webhook_delivery_from_record(record)

    def mark_delivery_failed(
        self,
        delivery_id: str,
        *,
        attempt: int,
        signature: str,
        response_status: int | None,
        response_body: str | None,
        error_message: str,
        next_attempt_at: datetime | None,
        now: datetime | None = None,
    ) -> WebhookDelivery | None:
        """Record a failed attempt, leaving it either retryable or abandoned.

        ``next_attempt_at is None`` is what the caller passes once attempts are
        exhausted; the status then becomes ``abandoned`` rather than ``failed``,
        so the row reads as "we gave up" instead of "waiting its turn".
        """
        record = self.session.get(WebhookDeliveryRecord, delivery_id)
        if record is None:
            return None
        record.attempt = attempt
        record.signature = signature
        record.response_status = response_status
        record.response_body = response_body
        record.error_message = error_message
        record.locked_by = None
        if next_attempt_at is None:
            record.status = WebhookDeliveryStatus.ABANDONED.value
            record.next_attempt_at = None
        else:
            record.status = WebhookDeliveryStatus.FAILED.value
            record.next_attempt_at = store_utc(next_attempt_at)
        record.delivered_at = store_utc(now or datetime.now(UTC))
        self.session.flush()
        self.session.refresh(record)
        return webhook_delivery_from_record(record)

    def requeue_delivery(
        self,
        delivery_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> WebhookDelivery | None:
        """Put a delivery back in the queue by hand, resetting its attempt count.

        A manual replay is a statement that the endpoint is fixed now, so
        carrying over the old attempt count would only make it give up sooner.
        """
        record = self.session.get(WebhookDeliveryRecord, delivery_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        record.status = WebhookDeliveryStatus.PENDING.value
        record.attempt = 0
        record.next_attempt_at = None
        record.locked_by = None
        record.error_message = None
        record.response_status = None
        record.response_body = None
        self.session.flush()
        self.session.refresh(record)
        return webhook_delivery_from_record(record)

    def note_subscription_success(
        self,
        subscription_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        record = self.session.get(WebhookSubscriptionRecord, subscription_id)
        if record is None:
            return
        record.last_success_at = store_utc(now or datetime.now(UTC))
        record.consecutive_failures = 0
        self.session.flush()

    def note_subscription_failure(
        self,
        subscription_id: str,
        *,
        disable_after: int,
        now: datetime | None = None,
    ) -> bool:
        """Count a failure and disable the subscription once it crosses the line.

        Returns whether the subscription was disabled by *this* call, so the
        caller audits a state change and not merely a failure.
        """
        record = self.session.get(WebhookSubscriptionRecord, subscription_id)
        if record is None:
            return False
        record.last_failure_at = store_utc(now or datetime.now(UTC))
        record.consecutive_failures = (record.consecutive_failures or 0) + 1
        disabled = False
        if (
            disable_after > 0
            and record.consecutive_failures >= disable_after
            and record.status == WebhookSubscriptionStatus.ACTIVE.value
        ):
            record.status = WebhookSubscriptionStatus.DISABLED.value
            disabled = True
        self.session.flush()
        return disabled


# --- V7.4 embeddable workspace ---------------------------------------------


class ExternalIntegrationOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    SLUG_CONFLICT = "slug_conflict"
    INVALID_ORIGINS = "invalid_origins"


@dataclass
class ExternalIntegrationResult:
    outcome: ExternalIntegrationOutcome
    integration: ExternalIntegration | None = None
    detail: str | None = None


def embed_capabilities() -> ExternalIntegrationCapabilities:
    """The embed contract as data, so a host can self-check before minting.

    Publishing the allowlist is deliberate: a partner that asks for ``run:write``
    should find out from a GET rather than from a 400 after wiring everything up.
    """
    return ExternalIntegrationCapabilities(
        pages=sorted(EMBED_PAGES),
        default_scopes=sorted(EMBED_DEFAULT_SCOPES),
        allowed_scopes=sorted(EMBED_ALLOWED_SCOPES),
        default_ttl_seconds=embed_default_ttl_seconds(),
        max_ttl_seconds=embed_max_ttl_seconds(),
        embed_path=EMBED_PATH,
    )


def external_integration_from_record(record: ExternalIntegrationRecord) -> ExternalIntegration:
    return ExternalIntegration(
        integration_id=record.id,
        tenant_id=record.tenant_id,
        name=record.name,
        slug=record.slug,
        description=record.description,
        allowed_origins=[str(origin) for origin in (record.allowed_origins or [])],
        status=record.status,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
        capabilities=embed_capabilities(),
    )


class ExternalIntegrationRepository:
    """Registered host platforms and the origins they may embed from (V7.4).

    The row is a registration only — embed sessions are stateless signed tokens —
    so this repository has no session lifecycle to manage: what it owns is the
    allowlist and the on/off switch. ``disabled`` is the revocation lever, and it
    works retroactively because verification re-reads the row.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_integration(
        self,
        payload: ExternalIntegrationCreate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        created_by: str | None = None,
    ) -> ExternalIntegrationResult:
        origins, origin_error = validate_origins(payload.allowed_origins)
        if origin_error is not None:
            return ExternalIntegrationResult(
                ExternalIntegrationOutcome.INVALID_ORIGINS,
                detail=origin_error,
            )
        # An explicit slug is a claim on a name and collides loudly; a derived one
        # is a convenience, so it dedupes instead of failing.
        if payload.slug:
            if self._slug_taken(payload.slug, tenant_id=tenant_id):
                return ExternalIntegrationResult(
                    ExternalIntegrationOutcome.SLUG_CONFLICT,
                    detail=f"slug is already registered: {payload.slug}",
                )
            slug = payload.slug
        else:
            slug = self._unique_slug(payload.name, tenant_id=tenant_id)
        record = ExternalIntegrationRecord(
            id=f"extint_{uuid4().hex[:12]}",
            tenant_id=tenant_id,
            name=payload.name,
            slug=slug,
            description=payload.description,
            allowed_origins=origins,
            status=str(payload.status),
            created_by=created_by,
        )
        self.session.add(record)
        self.session.flush()
        self.session.refresh(record)
        return ExternalIntegrationResult(
            ExternalIntegrationOutcome.CREATED,
            integration=external_integration_from_record(record),
        )

    def get_integration(
        self,
        integration_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ExternalIntegration | None:
        record = self._owned_record(integration_id, tenant_id=tenant_id)
        return external_integration_from_record(record) if record is not None else None

    def find_integration(self, integration_id: str) -> ExternalIntegration | None:
        """Look a row up *without* a tenant guard — for the embed document route.

        That request carries no credential at all (the token stays in the URL
        fragment and never reaches the server), so it cannot name a tenant. All it
        needs is the framing allowlist behind one opaque id, which is why this
        stays a separate method rather than a ``tenant_id=None`` mode: every
        tenant-scoped caller must keep going through :meth:`get_integration`.
        """
        record = self.session.get(ExternalIntegrationRecord, integration_id)
        return external_integration_from_record(record) if record is not None else None

    def list_integrations(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[tuple[str, bool]] | None = None,
    ) -> tuple[list[ExternalIntegration], int]:
        bound = max(1, min(limit, 200))
        conditions = [ExternalIntegrationRecord.tenant_id == tenant_id]
        if status is not None:
            conditions.append(ExternalIntegrationRecord.status == status)
        statement = select(ExternalIntegrationRecord).where(*conditions).limit(bound).offset(
            max(0, offset)
        )
        statement = _sorted(
            statement,
            ExternalIntegrationRecord,
            sort,
            (ExternalIntegrationRecord.created_at, ExternalIntegrationRecord.id),
        )
        count_statement = (
            select(func.count()).select_from(ExternalIntegrationRecord).where(*conditions)
        )
        records = list(self.session.scalars(statement))
        total = int(self.session.scalar(count_statement) or 0)
        return [external_integration_from_record(record) for record in records], total

    def update_integration(
        self,
        integration_id: str,
        payload: ExternalIntegrationUpdate,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ExternalIntegrationResult:
        record = self._owned_record(integration_id, tenant_id=tenant_id)
        if record is None:
            return ExternalIntegrationResult(ExternalIntegrationOutcome.NOT_FOUND)
        if payload.allowed_origins is not None:
            origins, origin_error = validate_origins(payload.allowed_origins)
            if origin_error is not None:
                return ExternalIntegrationResult(
                    ExternalIntegrationOutcome.INVALID_ORIGINS,
                    detail=origin_error,
                )
            record.allowed_origins = origins
        if payload.name is not None:
            record.name = payload.name
        if payload.description is not None:
            record.description = payload.description
        if payload.status is not None:
            record.status = payload.status
        self.session.flush()
        self.session.refresh(record)
        return ExternalIntegrationResult(
            ExternalIntegrationOutcome.UPDATED,
            integration=external_integration_from_record(record),
        )

    def delete_integration(
        self,
        integration_id: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ExternalIntegration | None:
        record = self._owned_record(integration_id, tenant_id=tenant_id)
        if record is None:
            return None
        integration = external_integration_from_record(record)
        # Nothing references an integration row (embed sessions are tokens), so
        # deleting is a hard delete with no cascade to think about. Links minted
        # before the delete stop verifying, because verification looks the row up.
        self.session.delete(record)
        self.session.flush()
        return integration

    def _owned_record(
        self,
        integration_id: str,
        *,
        tenant_id: str,
    ) -> ExternalIntegrationRecord | None:
        record = self.session.get(ExternalIntegrationRecord, integration_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    def _slug_taken(self, slug: str, *, tenant_id: str) -> bool:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(ExternalIntegrationRecord)
                .where(
                    ExternalIntegrationRecord.tenant_id == tenant_id,
                    ExternalIntegrationRecord.slug == slug,
                )
            )
            or 0
        ) > 0

    def _unique_slug(self, name: str, *, tenant_id: str) -> str:
        base = slugify(name)[:56] or "integration"
        if not self._slug_taken(base, tenant_id=tenant_id):
            return base
        for _ in range(5):
            candidate = f"{base[:48]}-{uuid4().hex[:6]}"
            if not self._slug_taken(candidate, tenant_id=tenant_id):
                return candidate
        return f"integration-{uuid4().hex[:8]}"


class DeploymentSettingsRepository:
    """The install's single settings row (V8.5).

    Not tenant-scoped, and that is the whole design: everything else here belongs
    to a tenant, while "is this install in maintenance" is a property of the
    process. Access control lives one layer up (``deployment:read`` /
    ``deployment:manage`` are tenant permissions), so the row's shape stays as
    simple as its meaning.

    ``get_or_create`` exists for the writes. Reads deliberately do not create:
    a GET that inserts has a side effect, and two replicas racing on the first
    request would be a real possibility. The migration seeds the row, and a
    database that somehow lacks it reads as defaults rather than failing.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self) -> DeploymentSettingRecord | None:
        return self.session.get(DeploymentSettingRecord, DEPLOYMENT_SETTINGS_ID)

    def get_or_create(self) -> DeploymentSettingRecord:
        """The row, created with the migration's own defaults if it is missing."""
        record = self.get()
        if record is None:
            record = DeploymentSettingRecord(
                id=DEPLOYMENT_SETTINGS_ID,
                maintenance_mode=False,
                backup_retention_days=settings.deployment_backup_retention_days,
            )
            self.session.add(record)
            self.session.flush()
        return record
