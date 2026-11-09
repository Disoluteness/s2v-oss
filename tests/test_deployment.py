"""V8.5 deployment posture: the switch, the report, and the artifacts.

Four layers, cheapest first:

1. **Pure rules** (``deployment.py``) — which requests maintenance mode refuses,
   where the migration head comes from, and which capabilities a settings object
   claims. No database, no request.
2. **The store** — the singleton row, its lifecycle, and the audit entries that
   record transitions rather than requests.
3. **The request contract** — the maintenance guard as mounted, including the two
   writes it deliberately lets through and the permissions on each endpoint.
4. **The artifacts** — the compose file, the manifests and the shell scripts. These
   are checked because they are deliverables: a YAML typo in a manifest nobody can
   apply in CI is a deployment that fails on the day it is needed, and a syntax
   error in ``restore.sh`` is discovered while the database is already down.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from harness import build_client

from apps.api.app import db_models  # noqa: F401  (registers the tables)
from apps.api.app.actor import ActorContext
from apps.api.app.config import Settings
from apps.api.app.deployment import (
    DEFAULT_MAINTENANCE_MESSAGE,
    DEPLOYMENT_SETTINGS_ID,
    alembic_head,
    alembic_pending,
    deployment_capabilities,
    is_maintenance_exempt,
    is_safe_method,
)
from apps.api.app.errors import ErrorCode
from apps.api.app.models import (
    DeploymentBackupRequest,
    DeploymentSettingsUpdate,
)
from apps.api.app.permissions import (
    TENANT_ONLY_PERMISSIONS,
    Permission,
    ProjectRole,
    TeamRole,
    effective_permissions,
)
from apps.api.app.repositories import DEFAULT_TENANT_ID
from apps.api.app.version import APP_VERSION

OWNER = "system"
MEMBER = "user_member"
NOBODY = "user_never_seated"

REPO_ROOT = Path(__file__).resolve().parents[1]


def _default_actor(actor_id: str = OWNER) -> ActorContext:
    return ActorContext(
        actor_id=actor_id,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "deployment.db")
    # The default identity owns the default team only once seated (V6.5: a new
    # identity does not join a team by itself), and without that every permission
    # decision below would answer "no" for a reason that has nothing to do with
    # the feature under test.
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _seat_member(asgi, actor_id: str = MEMBER, role: str = TeamRole.MEMBER.value) -> str:
    from apps.api.app.models import TeamMemberAdd

    team_id = asgi.store.teams.default_team_id(DEFAULT_TENANT_ID)
    assert team_id is not None
    _outcome, _ = asgi.store.teams.add_member(
        team_id, TeamMemberAdd(user_id=actor_id, role=role), invited_by=OWNER
    )
    asgi.store.session.commit()
    return team_id


# --------------------------------------------------------------------------- #
# 1. pure rules
# --------------------------------------------------------------------------- #


def test_safe_methods_are_exactly_the_ones_that_cannot_change_anything() -> None:
    assert is_safe_method("get")
    assert is_safe_method("HEAD")
    assert is_safe_method("options")
    assert not is_safe_method("POST")
    assert not is_safe_method("PATCH")
    assert not is_safe_method("DELETE")


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_every_read_is_exempt_whatever_the_path(method: str) -> None:
    assert is_maintenance_exempt(method, "/api/projects")
    assert is_maintenance_exempt(method, "/api/runs/whatever/start")


def test_the_off_switch_stays_reachable() -> None:
    """Without this exemption the maintenance switch would be a one-way door."""
    assert is_maintenance_exempt("POST", "/api/deployment/maintenance")
    assert is_maintenance_exempt("PATCH", "/api/deployment")
    assert is_maintenance_exempt("POST", "/api/deployment/backups")


def test_provider_callbacks_are_exempt_because_they_carry_paid_results() -> None:
    assert is_maintenance_exempt("POST", "/api/provider-callbacks/dashscope")


def test_auth_endpoints_are_exempt_because_identity_is_not_a_tenant_write() -> None:
    """A maintenance window must not lock people out of proving who they are."""
    assert is_maintenance_exempt("POST", "/api/auth/login")
    assert is_maintenance_exempt("POST", "/api/auth/logout")
    # And the exemption is a prefix, not a substring match: a lookalike path
    # does not inherit it.
    assert not is_maintenance_exempt("POST", "/api/auth-evil")


def test_an_ordinary_write_is_refused() -> None:
    assert not is_maintenance_exempt("POST", "/api/projects")
    assert not is_maintenance_exempt("POST", "/api/runs/run_1/start")
    assert not is_maintenance_exempt("DELETE", "/api/webhooks/webhook_1")
    # A prefix match, not a substring one: a lookalike path must not inherit the
    # exemption of the path it resembles.
    assert not is_maintenance_exempt("POST", "/api/deployment-evil")


def test_the_head_is_the_newest_migration_on_disk() -> None:
    assert alembic_head() == "20261109_0023"


def test_an_unknown_revision_reports_nothing_pending() -> None:
    """``None`` means "cannot tell", and twenty-one alarms help nobody."""
    assert alembic_pending(None) == []


def test_being_at_the_head_reports_nothing_pending() -> None:
    assert alembic_pending("20261109_0023") == []


def test_pending_is_the_gap_in_application_order() -> None:
    assert alembic_pending("20261012_0021") == ["20261012_0022", "20261109_0023"]


def test_a_revision_behind_reports_the_whole_chain_in_order() -> None:
    pending = alembic_pending("20260915_0001")
    assert pending[-1] == "20261109_0023"
    assert pending[0] == "20260916_0002"
    # Ascending, which is the order they must run in — a reversed list would be a
    # plausible-looking answer that is simply wrong.
    assert pending == sorted(pending)


def test_a_revision_that_is_not_on_the_head_ancestry_reports_the_full_chain() -> None:
    """A divergent revision is not "nearly up to date"; it is "unknown"."""
    pending = alembic_pending("29990101_9999")
    assert pending
    assert pending[-1] == "20261109_0023"


def test_capabilities_describe_behaviour_not_environment_variables() -> None:
    off = Settings(
        secret_encryption_key=None,
        provider_callback_secret=None,
        log_format="text",
        app_env="local",
    )
    assert deployment_capabilities(off) == {
        "secret_encryption": False,
        "provider_callback_signature": False,
        "asset_link_secret_secure": True,  # a development environment accepts the default
        "structured_logs": False,
        "webhook_dispatch": True,
    }

    on = Settings(
        secret_encryption_key="k",
        provider_callback_secret="s",
        log_format="JSON",
        app_env="production",
        asset_link_secret="a-real-secret",
    )
    capabilities = deployment_capabilities(on)
    assert capabilities["secret_encryption"] is True
    assert capabilities["provider_callback_signature"] is True
    assert capabilities["structured_logs"] is True
    assert capabilities["asset_link_secret_secure"] is True


def test_a_production_environment_with_the_development_secret_reports_insecure() -> None:
    insecure = Settings(app_env="production", asset_link_secret="dev-only-change-me")
    assert deployment_capabilities(insecure)["asset_link_secret_secure"] is False


# --------------------------------------------------------------------------- #
# 2. the store
# --------------------------------------------------------------------------- #


def test_settings_read_as_defaults_when_the_row_is_absent(asgi) -> None:
    """The migration seeds the row; a hand-built database has none."""
    view = asgi.store.deployment_settings()
    assert view.configured is False
    assert view.settings_id == DEPLOYMENT_SETTINGS_ID
    assert view.maintenance_mode is False
    # Not a blank label, and not a crash: the configured environment is a better
    # answer than an empty string everyone has to interpret.
    assert view.environment


def test_maintenance_is_off_when_there_is_no_row(asgi) -> None:
    state = asgi.store.maintenance_state()
    assert state.enabled is False
    assert state.message is None


def test_enabling_maintenance_creates_the_singleton(asgi) -> None:
    view = asgi.store.set_maintenance_mode(
        enabled=True, message=None, actor=_default_actor()
    )
    assert view.configured is True
    assert view.settings_id == DEPLOYMENT_SETTINGS_ID
    assert view.maintenance_mode is True
    # A blank message is replaced rather than stored empty: the caller asked for
    # maintenance, and "why" is the useful half.
    assert view.maintenance_message == DEFAULT_MAINTENANCE_MESSAGE
    assert view.maintenance_updated_by == OWNER
    assert view.maintenance_updated_at is not None


def test_a_custom_message_survives_and_is_cleared_on_resume(asgi) -> None:
    asgi.store.set_maintenance_mode(
        enabled=True, message="  migrating users table  ", actor=_default_actor()
    )
    assert asgi.store.maintenance_state().message == "migrating users table"

    view = asgi.store.set_maintenance_mode(enabled=False, message=None, actor=_default_actor())
    assert view.maintenance_mode is False
    # A stale "back in five minutes" must not outlive the window it described.
    assert view.maintenance_message is None


def test_the_switch_is_audited_once_per_transition(asgi) -> None:
    asgi.store.set_maintenance_mode(enabled=True, message=None, actor=_default_actor())
    asgi.store.set_maintenance_mode(enabled=True, message="again", actor=_default_actor())
    asgi.store.set_maintenance_mode(enabled=False, message=None, actor=_default_actor())

    entries = asgi.store.list_audit_logs(action="deployment.maintenance_enabled")
    assert len(entries) == 1
    disabled = asgi.store.list_audit_logs(action="deployment.maintenance_disabled")
    assert len(disabled) == 1
    assert disabled[0].payload["enabled"] is False


def test_updating_settings_records_only_what_changed(asgi) -> None:
    view = asgi.store.update_deployment_settings(
        DeploymentSettingsUpdate(deployment_name="studio-eu", environment="staging"),
        _default_actor(),
    )
    assert view.deployment_name == "studio-eu"
    assert view.environment == "staging"

    entries = asgi.store.list_audit_logs(action="deployment.settings_updated")
    assert len(entries) == 1
    assert set(entries[0].payload) == {"deployment_name", "environment"}

    # A second, identical update is a no-op and must not add a second entry: an
    # audit trail that records requests cannot answer "who changed this".
    asgi.store.update_deployment_settings(
        DeploymentSettingsUpdate(deployment_name="studio-eu", environment="staging"),
        _default_actor(),
    )
    assert len(asgi.store.list_audit_logs(action="deployment.settings_updated")) == 1


def test_backup_bookkeeping_records_the_id_and_normalises_the_time(asgi) -> None:
    aware = datetime.now(UTC) - timedelta(hours=3)
    view = asgi.store.record_backup(
        DeploymentBackupRequest(backup_id="s2v-backup-test", taken_at=aware),
        _default_actor(),
    )
    assert view.last_backup_id == "s2v-backup-test"
    assert view.last_backup_at is not None
    # Stored naive-UTC, the shape every timestamp column in this schema has (see
    # ``store_utc``); a tz-aware value would compare differently on MySQL and
    # SQLite.
    assert view.last_backup_at.tzinfo is None
    assert asgi.store.list_audit_logs(action="deployment.backup_recorded")


def test_backup_without_a_time_uses_now(asgi) -> None:
    view = asgi.store.record_backup(
        DeploymentBackupRequest(backup_id="s2v-backup-now"), _default_actor()
    )
    assert view.last_backup_at is not None
    assert (datetime.now(UTC).replace(tzinfo=None) - view.last_backup_at).total_seconds() < 60


def test_the_current_revision_is_unknown_on_a_metadata_built_database(asgi) -> None:
    """The ORM-built test database has no alembic_version, and that is reportable."""
    assert asgi.store.alembic_current_revision() is None


def test_table_count_is_readable_and_plausible(asgi) -> None:
    # ``table_count`` counts what the schema actually has, *including* Alembic's
    # own ``alembic_version`` table, so a migrated database reports 35 while
    # preflight (which excludes it) reports 34. The exact number is pinned in the
    # docs, the release checklist and gate section 25 — not here, where a second
    # copy would only be one more place to forget.
    count = asgi.store.table_count()
    assert count is not None and count > 20


def test_the_overview_reports_cannot_tell_rather_than_claiming_health(asgi) -> None:
    overview = asgi.store.deployment_overview()
    assert overview.app_version == APP_VERSION
    assert overview.migration.head == "20261109_0023"
    assert overview.migration.current is None
    # The important assertion: an unknown revision is *not* vacuously "up to date".
    assert overview.migration.up_to_date is False
    assert overview.migration.pending == []
    assert set(overview.capabilities) == {
        "secret_encryption",
        "provider_callback_signature",
        "asset_link_secret_secure",
        "structured_logs",
        "webhook_dispatch",
    }
    assert overview.table_count is not None


# --------------------------------------------------------------------------- #
# 3. the request contract
# --------------------------------------------------------------------------- #


def test_posture_is_readable_and_shaped_as_documented(asgi) -> None:
    status, _, body = asgi.request("GET", "/api/deployment")
    assert status == 200, body
    assert set(body) == {
        "settings",
        "app_version",
        "environment",
        "migration",
        "capabilities",
        "table_count",
    }
    assert body["app_version"] == APP_VERSION
    assert body["settings"]["maintenance_mode"] is False


def test_editing_settings_applies_and_is_returned(asgi) -> None:
    status, _, body = asgi.request(
        "PATCH", "/api/deployment", json_body={"notes": "owned by platform team"}
    )
    assert status == 200, body
    assert body["notes"] == "owned by platform team"
    assert body["configured"] is True

    status, _, reread = asgi.request("GET", "/api/deployment")
    assert status == 200
    assert reread["settings"]["notes"] == "owned by platform team"


def test_an_unknown_settings_field_is_rejected_rather_than_ignored(asgi) -> None:
    """``extra="forbid"``: a typo must not silently do nothing."""
    status, _, body = asgi.request(
        "PATCH", "/api/deployment", json_body={"deployment_nmae": "typo"}
    )
    assert status == 422
    assert body["detail"]["code"] == ErrorCode.VALIDATION_ERROR


def test_maintenance_refuses_writes_and_allows_reads(asgi) -> None:
    status, _, body = asgi.request(
        "POST", "/api/deployment/maintenance", json_body={"enabled": True}
    )
    assert status == 200, body
    assert body["maintenance_mode"] is True

    status, _, body = asgi.request("POST", "/api/projects", json_body={"title": "blocked"})
    assert status == 503, body
    assert body["detail"]["code"] == ErrorCode.MAINTENANCE_MODE
    # The actionable half: the request was fine, retrying later is the remedy.
    assert body["detail"]["read_only"] is True

    # Reads are untouched, so the UI keeps rendering during a migration.
    status, _, body = asgi.request("GET", "/api/projects")
    assert status == 200
    assert isinstance(body, list)

    # And the escape hatch stays open, or the switch could never be turned off.
    status, _, body = asgi.request(
        "PATCH", "/api/deployment", json_body={"notes": "still reachable"}
    )
    assert status == 200, body
    assert body["notes"] == "still reachable"

    status, _, body = asgi.request(
        "POST", "/api/deployment/backups", json_body={"backup_id": "bk-during-maintenance"}
    )
    assert status == 200, body


def test_provider_callbacks_are_not_refused_by_maintenance(asgi) -> None:
    asgi.request("POST", "/api/deployment/maintenance", json_body={"enabled": True})
    status, _, body = asgi.request(
        "POST", "/api/provider-callbacks/dashscope", json_body={"task_id": "t1", "status": "done"}
    )
    # It may be rejected for what it *is* (an unknown task, a bad payload); what
    # matters is that it is not rejected for the deployment being in maintenance,
    # because that would discard a result the platform has already paid for.
    assert status != 503, body


def test_resuming_restores_writes(asgi) -> None:
    asgi.request("POST", "/api/deployment/maintenance", json_body={"enabled": True})
    status, _, _ = asgi.request("POST", "/api/projects", json_body={"title": "blocked"})
    assert status == 503

    status, _, body = asgi.request(
        "POST", "/api/deployment/maintenance", json_body={"enabled": False}
    )
    assert status == 200, body
    assert body["maintenance_mode"] is False

    status, _, body = asgi.request("POST", "/api/projects", json_body={"title": "allowed"})
    assert status == 200, body
    assert body["title"] == "allowed"


def test_a_team_member_may_read_posture_but_not_change_it(asgi) -> None:
    _seat_member(asgi)

    status, _, body = asgi.request("GET", "/api/deployment", actor_id=MEMBER)
    assert status == 200, body

    status, _, body = asgi.request(
        "PATCH", "/api/deployment", json_body={"notes": "nope"}, actor_id=MEMBER
    )
    assert status == 403, body
    assert body["detail"]["code"] == ErrorCode.PERMISSION_DENIED

    status, _, body = asgi.request(
        "POST", "/api/deployment/maintenance", json_body={"enabled": True}, actor_id=MEMBER
    )
    assert status == 403, body

    # And the member did not manage to leave the deployment in maintenance.
    assert asgi.store.maintenance_state().enabled is False


def test_an_unseated_identity_cannot_read_posture(asgi) -> None:
    status, _, body = asgi.request("GET", "/api/deployment", actor_id=NOBODY)
    assert status == 403
    assert body["detail"]["code"] == ErrorCode.PERMISSION_DENIED


def test_recording_a_backup_through_the_api(asgi) -> None:
    status, _, body = asgi.request(
        "POST",
        "/api/deployment/backups",
        json_body={"backup_id": "s2v-backup-20261012T031500Z", "note": "quarterly rehearsal"},
    )
    assert status == 200, body
    assert body["last_backup_id"] == "s2v-backup-20261012T031500Z"
    assert body["last_backup_at"] is not None

    status, _, body = asgi.request("POST", "/api/deployment/backups", json_body={})
    assert status == 422  # backup_id is required


def test_the_manage_key_is_tenant_only_and_the_read_key_is_not() -> None:
    assert Permission.DEPLOYMENT_MANAGE in TENANT_ONLY_PERMISSIONS
    assert Permission.DEPLOYMENT_READ not in TENANT_ONLY_PERMISSIONS
    # A read key rides the mechanical rule, which is what keeps "who can look" and
    # "who can change" from needing a second, ad-hoc matrix.
    assert Permission.DEPLOYMENT_READ in effective_permissions(TeamRole.VIEWER, None)
    assert Permission.DEPLOYMENT_READ in effective_permissions(TeamRole.MEMBER, None)
    # No project role can ever confer the write key, however senior.
    for role in ProjectRole:
        assert Permission.DEPLOYMENT_MANAGE not in effective_permissions(
            TeamRole.VIEWER, role
        )


def test_the_maintenance_error_is_retryable_and_carries_a_503() -> None:
    from apps.api.app.errors import error_catalog_table

    entry = next(
        row for row in error_catalog_table() if row["code"] == ErrorCode.MAINTENANCE_MODE
    )
    assert entry["status_code"] == 503
    assert entry["category"] == "server"
    # A maintenance window ends, so a client that gave up permanently on the first
    # refusal would have to be restarted by hand when it closed.
    assert entry["retryable"] is True


# --------------------------------------------------------------------------- #
# 4. the artifacts
# --------------------------------------------------------------------------- #


def _yaml_documents(path: Path) -> list[object]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _k8s_paths() -> list[Path]:
    return sorted((REPO_ROOT / "infra" / "deploy" / "k8s").glob("*.yaml"))


def test_the_k8s_manifests_are_present() -> None:
    names = {path.name for path in _k8s_paths()}
    assert {
        "00-namespace.yaml",
        "10-config.yaml",
        "20-api.yaml",
        "30-workers.yaml",
        "40-migrate-job.yaml",
        "50-ingress.yaml",
        "60-autoscaling.yaml",
    } <= names


@pytest.mark.parametrize("path", [*_k8s_paths(), REPO_ROOT / "docker-compose.prod.yml"])
def test_every_deployment_manifest_parses(path: Path) -> None:
    """Structural validity, which is the only part verifiable without a cluster.

    It is still worth pinning: these files are applied on the day something is
    already wrong, and a manifest that does not parse turns a bad afternoon into a
    bad week.
    """
    documents = _yaml_documents(path)
    assert documents, f"{path.name} parsed to nothing"
    for document in documents:
        assert isinstance(document, dict)
        assert document.get("apiVersion") or document.get("services"), (
            f"{path.name} has a document with neither apiVersion nor services"
        )


def test_every_k8s_document_is_namespaced_except_the_namespace_itself() -> None:
    for path in _k8s_paths():
        for document in _yaml_documents(path):
            if document.get("kind") == "Namespace":
                continue
            assert document["metadata"]["namespace"] == "s2v-studio", (
                f"{path.name} / {document.get('kind')} is not namespaced"
            )


def test_the_production_compose_runs_migrations_before_the_api() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "migrate" in services, "the one-shot migration service is the point of this file"
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]

    # The dependency edge is what makes the ordering a property of the file rather
    # than of the operator's memory.
    for name in ("api", "worker-provider", "worker-analysis", "worker-media", "worker-render"):
        depends = services[name]["depends_on"]
        assert depends["migrate"]["condition"] == "service_completed_successfully", name


def test_the_production_compose_splits_the_worker_by_role() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    roles = {
        name: service.get("environment", {}).get("WORKER_ROLE")
        for name, service in compose["services"].items()
        if name.startswith("worker-")
    }
    assert roles == {
        "worker-provider": "provider",
        "worker-analysis": "analysis",
        "worker-media": "media",
        "worker-render": "render",
    }


def test_the_production_compose_publishes_no_infrastructure_ports() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    for name in ("mysql", "redis", "minio"):
        assert "ports" not in compose["services"][name], (
            f"{name} must not be reachable from the host in the production file"
        )


def test_the_api_manifest_probes_the_application_not_just_the_port() -> None:
    documents = _yaml_documents(REPO_ROOT / "infra" / "deploy" / "k8s" / "20-api.yaml")
    deployment = next(doc for doc in documents if doc.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert container[probe]["httpGet"]["path"] == "/api/health", probe
    # Hardening that would be easy to drop in an edit and hard to notice.
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_the_ingress_does_not_expose_the_scrape_endpoint() -> None:
    documents = _yaml_documents(REPO_ROOT / "infra" / "deploy" / "k8s" / "50-ingress.yaml")
    ingress = next(doc for doc in documents if doc.get("kind") == "Ingress")
    paths = [
        rule_path["path"]
        for rule in ingress["spec"]["rules"]
        for rule_path in rule["http"]["paths"]
    ]
    # ``/metrics`` is unauthenticated by design (V8.2), so the place to exclude it
    # is the edge rather than the application.
    assert not any("metrics" in path for path in paths)


@pytest.mark.parametrize("name", ["backup.sh", "restore.sh"])
def test_the_operational_scripts_are_executable_and_parse(name: str) -> None:
    path = REPO_ROOT / "scripts" / name
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert path.stat().st_mode & 0o111, f"{name} is not executable"
    # ``bash -n`` is a syntax check only, and that is the honest limit: these
    # scripts need a live database to do anything real, so what is pinned here is
    # that an operator cannot discover a typo while the database is already down.
    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_restore_script_refuses_to_run_without_the_interlock() -> None:
    """The maintenance check is what stops a restore from racing the application."""
    text = (REPO_ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
    assert "--skip-maintenance-check" in text
    assert "maintenance_mode" in text
    # ``--yes`` and the archive are both mandatory, and the usage error is exit 2.
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "restore.sh"), "--archive", "/nonexistent.tar.gz"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--yes" in result.stderr


def test_the_backup_script_only_prunes_when_told_to() -> None:
    text = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    # Defaulting to reporting rather than deleting is the reason a retention typo
    # cannot destroy data.
    assert "would prune" in text
    assert "--prune" in text


def test_the_preflight_reports_a_health_row_the_api_cannot(asgi) -> None:
    """It reads the database and configuration directly, which is the point.

    An HTTP probe cannot report "the schema is behind the code" when the code is
    what refuses to start.
    """
    from scripts import deployment_preflight

    checks = deployment_preflight.run_preflight(
        config=Settings(app_env="production", asset_link_secret="a-real-secret"),
        engine=asgi.engine,
        database_url="sqlite:///ignored.db",
    )
    by_name = {check.name: check for check in checks}
    assert by_name["database"].status == deployment_preflight.OK
    assert by_name["asset_link_secret"].status == deployment_preflight.OK
    # The ORM-built database has no alembic_version, so this is the honest answer.
    assert by_name["schema_revision"].status == deployment_preflight.FAIL
    assert by_name["maintenance_mode"].status == deployment_preflight.OK
    assert by_name["last_backup"].status == deployment_preflight.WARN


def test_the_preflight_fails_a_production_deployment_using_the_development_secret() -> None:
    from scripts import deployment_preflight

    checks = deployment_preflight._config_checks(
        Settings(app_env="production", asset_link_secret="dev-only-change-me")
    )
    failure = next(check for check in checks if check.name == "asset_link_secret")
    assert failure.status == deployment_preflight.FAIL
    assert "ASSET_LINK_SECRET" in failure.detail


def test_the_preflight_flags_secret_trust_mode_without_a_secret() -> None:
    from scripts import deployment_preflight

    checks = deployment_preflight._config_checks(
        Settings(actor_trust_mode="secret", actor_header_secret=None)
    )
    failure = next(check for check in checks if check.name == "actor_trust_mode")
    # ``secret`` mode with no secret trusts no header at all, which locks out every
    # identity rather than merely being a poor idea.
    assert failure.status == deployment_preflight.FAIL


def test_the_preflight_json_shape_is_stable() -> None:
    from scripts import deployment_preflight

    checks = [
        deployment_preflight._check("database", deployment_preflight.OK, "connected"),
        deployment_preflight._check("schema_revision", deployment_preflight.WARN, "behind"),
    ]
    rendered = deployment_preflight._render(checks, as_json=True)
    payload = __import__("json").loads(rendered)
    assert payload["app_version"] == APP_VERSION
    assert payload["failed"] == 0
    assert payload["warned"] == 1
    assert [row["name"] for row in payload["checks"]] == ["database", "schema_revision"]


def test_the_runbook_exists_and_names_the_documented_entry_points() -> None:
    runbook = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for anchor in (
        "alembic upgrade head",
        "scripts/backup.sh",
        "scripts/restore.sh",
        "scripts/deployment_preflight.py",
        "/api/deployment/maintenance",
        "migration.up_to_date",
    ):
        assert anchor in runbook, anchor


def test_the_optional_settings_are_absent_from_the_required_path() -> None:
    """Maintenance mode must not be configurable from the environment.

    If it could be, the switch would require exactly the restart it exists to
    avoid, and two sources of truth would disagree about whether writes are on.
    """
    fields = set(Settings.model_fields)
    assert "deployment_name" in fields
    assert "deployment_backup_retention_days" in fields
    assert not any("maintenance" in field for field in fields)


def test_the_schema_has_exactly_one_deployment_settings_table() -> None:
    from apps.api.app.db_models import Base

    assert "deployment_settings" in Base.metadata.tables
    table = Base.metadata.tables["deployment_settings"]
    # Install-scoped, so no tenant column — and that is worth asserting, because
    # adding one later would silently break the "one row describes the install"
    # invariant that both the repository and the permission story rest on.
    assert "tenant_id" not in table.columns
    assert next(iter(table.primary_key.columns)).name == "id"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
