"""Role-based permissions for teams and projects.

Two role layers exist. A **team role** is the baseline a user gets from the team
that owns the tenant; a **project role** narrows or widens that baseline for one
project. The effective permission set is the union of both, so a project role can
only ever add access, never silently revoke the team baseline.

Everything here is deliberately declarative: the matrix is the single source of
truth for the API, the UI and the tests.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Permission(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"
    RUN_READ = "run:read"
    RUN_CREATE = "run:create"
    RUN_START = "run:start"
    RUN_WRITE = "run:write"
    RUN_REVIEW = "run:review"
    ASSET_READ = "asset:read"
    ASSET_WRITE = "asset:write"
    LIBRARY_READ = "library:read"
    LIBRARY_WRITE = "library:write"
    MEMBER_READ = "member:read"
    MEMBER_MANAGE = "member:manage"
    TEAM_MANAGE = "team:manage"
    APIKEY_MANAGE = "apikey:manage"
    WEBHOOK_MANAGE = "webhook:manage"
    INTEGRATION_MANAGE = "integration:manage"
    QUOTA_READ = "quota:read"
    QUOTA_MANAGE = "quota:manage"
    PROVIDER_MANAGE = "provider:manage"
    DEPLOYMENT_READ = "deployment:read"
    DEPLOYMENT_MANAGE = "deployment:manage"


class TeamRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)
READ_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
    permission for permission in Permission if permission.value.endswith(":read")
)

TEAM_ROLE_PERMISSIONS: dict[TeamRole, frozenset[Permission]] = {
    TeamRole.OWNER: ALL_PERMISSIONS,
    TeamRole.ADMIN: ALL_PERMISSIONS - {Permission.TEAM_MANAGE},
    TeamRole.MEMBER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.RUN_READ,
            Permission.RUN_CREATE,
            Permission.RUN_START,
            Permission.RUN_WRITE,
            Permission.RUN_REVIEW,
            Permission.ASSET_READ,
            Permission.ASSET_WRITE,
            Permission.LIBRARY_READ,
            Permission.LIBRARY_WRITE,
            Permission.MEMBER_READ,
            Permission.QUOTA_READ,
            Permission.DEPLOYMENT_READ,
        }
    ),
    TeamRole.VIEWER: READ_ONLY_PERMISSIONS,
}

# ``TEAM_MANAGE`` is intentionally absent: managing the team itself is a team-level
# concern, so a project role must never hand it out. ``APIKEY_MANAGE`` joins it —
# issuing a credential that can act as its owner across the whole tenant is not a
# project-scoped capability, however senior the project role. ``WEBHOOK_MANAGE``
# completes the set: a subscription's endpoint receives events from *every* run in
# the tenant, so scoping it to one project would be a lie about what it grants.
# ``INTEGRATION_MANAGE`` (V7.4) is the same argument once more: registering a host
# platform mints links into *any* project of the tenant, so it cannot be a
# project-scoped capability.
#
# ``QUOTA_MANAGE`` (V8.3) closes the set. Reading a team's quota is a read-only
# view and rides ``READ_ONLY_PERMISSIONS`` like every other ``:read`` key, but
# *setting* a ceiling — or exempting one run from a hard one — decides how much
# the whole tenant may spend. A project role must not be able to raise its own
# allowance, so it is tenant-only: only a team owner or admin, who is already
# trusted with ``api_key:manage`` and ``webhook:manage``, can move the number.
#
# ``PROVIDER_MANAGE`` (V8.4) is the last one, and note what is *absent*: there is
# no ``provider:read``. The read surface is not innocuous — a credential carries
# ``secret_preview`` (the first three and last four characters of the key) and the
# listing enumerates which paid providers the tenant holds — so "who may look" is
# the same question as "who may change". One key, and a project role never holds
# it: whoever can write a provider key decides which account the whole tenant's
# paid calls are billed to.
#
# ``DEPLOYMENT_MANAGE`` (V8.5) is tenant-only by the same argument as
# ``TEAM_MANAGE``, one level further out: it flips the maintenance switch and
# edits the settings of the *install*, not of a project. But its read twin
# ``DEPLOYMENT_READ`` is granted broadly on purpose, and the contrast with
# ``provider:manage`` above is the point: deployment posture holds nothing worth
# hiding (a version string, a revision id, a boolean, when the last backup was
# taken). It rides ``READ_ONLY_PERMISSIONS`` like every other ``:read`` key and is
# handed to team members explicitly, because being able to answer "is this box up
# to date, and is it in maintenance" is ordinary operational self-service — while
# being able to *change* either answer is not.
TENANT_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.TEAM_MANAGE,
        Permission.APIKEY_MANAGE,
        Permission.WEBHOOK_MANAGE,
        Permission.INTEGRATION_MANAGE,
        Permission.QUOTA_MANAGE,
        Permission.PROVIDER_MANAGE,
        Permission.DEPLOYMENT_MANAGE,
    }
)
PROJECT_ROLE_PERMISSIONS: dict[ProjectRole, frozenset[Permission]] = {
    ProjectRole.OWNER: ALL_PERMISSIONS - TENANT_ONLY_PERMISSIONS,
    ProjectRole.EDITOR: ALL_PERMISSIONS
    - TENANT_ONLY_PERMISSIONS
    - {Permission.PROJECT_DELETE, Permission.MEMBER_MANAGE},
    ProjectRole.REVIEWER: frozenset(
        {
            Permission.PROJECT_READ,
            Permission.RUN_READ,
            Permission.RUN_REVIEW,
            Permission.ASSET_READ,
            Permission.LIBRARY_READ,
        }
    ),
    ProjectRole.VIEWER: READ_ONLY_PERMISSIONS,
}


def coerce_team_role(value: str | TeamRole | None) -> TeamRole | None:
    if value is None:
        return None
    if isinstance(value, TeamRole):
        return value
    try:
        return TeamRole(value)
    except ValueError:
        return None


def coerce_project_role(value: str | ProjectRole | None) -> ProjectRole | None:
    if value is None:
        return None
    if isinstance(value, ProjectRole):
        return value
    try:
        return ProjectRole(value)
    except ValueError:
        return None


def effective_permissions(
    team_role: str | TeamRole | None,
    project_role: str | ProjectRole | None = None,
) -> frozenset[Permission]:
    """Union of the team baseline and the project-specific role."""
    permissions: set[Permission] = set()
    resolved_team = coerce_team_role(team_role)
    if resolved_team is not None:
        permissions |= TEAM_ROLE_PERMISSIONS[resolved_team]
    resolved_project = coerce_project_role(project_role)
    if resolved_project is not None:
        permissions |= PROJECT_ROLE_PERMISSIONS[resolved_project]
    return frozenset(permissions)


def has_permission(
    permission: Permission,
    *,
    team_role: str | TeamRole | None,
    project_role: str | ProjectRole | None = None,
) -> bool:
    return permission in effective_permissions(team_role, project_role)


def permission_matrix() -> dict[str, Any]:
    """Machine-readable matrix, used by the API and rendered by the team page."""
    return {
        "permissions": [permission.value for permission in Permission],
        "team_roles": [
            {
                "role": role.value,
                "permissions": sorted(
                    permission.value for permission in TEAM_ROLE_PERMISSIONS[role]
                ),
            }
            for role in TeamRole
        ],
        "project_roles": [
            {
                "role": role.value,
                "permissions": sorted(
                    permission.value for permission in PROJECT_ROLE_PERMISSIONS[role]
                ),
            }
            for role in ProjectRole
        ],
    }


class PermissionDenied(Exception):
    """Raised when the acting identity lacks the permission an endpoint needs.

    Mirrors ``RunGateError``: the payload is flat, so ``main.py``'s global handler
    can return ``{"detail": {code, message, ...}}`` and the Web layer can render a
    structured message instead of a bare 403.
    """

    def __init__(
        self,
        permission: Permission | str,
        *,
        message: str | None = None,
        status_code: int = 403,
        actor_id: str | None = None,
        actor_source: str | None = None,
        team_role: str | None = None,
        project_role: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.permission = str(permission)
        self.message = message or (
            f"actor is not allowed to perform '{self.permission}' on this resource"
        )
        self.status_code = status_code
        self.details: dict[str, Any] = {
            "permission": self.permission,
            "actor_id": actor_id,
            "actor_source": actor_source,
            "team_role": team_role,
            "project_role": project_role,
        }
        if details:
            self.details.update(details)
        super().__init__(self.message)

    @property
    def code(self) -> str:
        return "permission_denied"

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}
