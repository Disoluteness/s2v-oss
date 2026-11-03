"""V8.4 security hardening: the vault, the takeover, the signatures, the doors.

Four layers again, cheapest first:

1. **The box** (``secrets.py``) — AES-256-GCM sealing with the secret's own
   identity as associated data, so a blob moved to another row stops opening.
   Pure, no session.
2. **The vault** — ``ProviderCredentialRepository`` routing writes through the
   box when one is configured, and otherwise leaving values inline exactly as
   they were before this version.
3. **The takeover** — the five provider-credential endpoints V6.5 parked in
   ``OPEN_ENDPOINTS`` now answer to ``provider:manage``, a tenant-only key; the
   callback that *cannot* answer to a permission answers to a signature instead.
4. **The doors** — upload admission (size, type, magic bytes) and the signed
   asset link's TTL ceiling and default-secret refusal.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from harness import build_client

from apps.api.app.actor import ActorContext
from apps.api.app.errors import ErrorCode
from apps.api.app.models import (
    ProjectCreate,
    ProjectMemberAdd,
    ProviderCredentialCreate,
    ProviderCredentialUpdate,
    ProviderSecretSource,
    TeamMemberAdd,
)
from apps.api.app.permissions import (
    TENANT_ONLY_PERMISSIONS,
    Permission,
    ProjectRole,
    TeamRole,
    effective_permissions,
)
from apps.api.app.repositories import DEFAULT_TENANT_ID
from apps.api.app.secrets import (
    ALGORITHM,
    DEFAULT_ASSET_LINK_SECRET,
    SecretBox,
    SecretEncryptionError,
    asset_link_secret_is_secure,
    is_managed_secret_ref,
)
from apps.api.app.webhooks import sign_webhook

OWNER = "system"
PROJECT_OWNER = "user_project_owner"


def _default_actor() -> ActorContext:
    return ActorContext(
        actor_id=OWNER,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


@pytest.fixture()
def asgi(tmp_path):
    """Encryption off — the default, and the state this version must not change."""
    client = build_client(tmp_path / "security.db")
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture()
def secure_asgi(tmp_path, monkeypatch):
    """Encryption on, configured the way an operator would: before the store exists."""
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "secret_encryption_key", "unit-test-master-key")
    client = build_client(tmp_path / "secure.db")
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _box(key: str = "unit-test-master-key") -> SecretBox:
    return SecretBox(key=key)


def _project_and_run(client) -> tuple[str, str]:
    status, _, project = client.request(
        "POST", "/api/projects", json_body={"title": "安全项目"}, actor_id=OWNER
    )
    assert status == 200, project
    status, _, run = client.request(
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        json_body={"raw_script": "秋天第一杯奶茶"},
        actor_id=OWNER,
    )
    assert status == 200, run
    return project["project_id"], run["run_id"]


# --------------------------------------------------------------------------- #
# 1. the box: sealing, binding, tamper detection
# --------------------------------------------------------------------------- #


def test_a_sealed_value_opens_under_the_same_identity():
    box = _box()
    sealed = box.seal("sk-live-abcdef", tenant_id="t1", name="DASHSCOPE_API_KEY")

    assert sealed.algorithm == ALGORITHM
    assert sealed.key_version == box.key_version
    assert "sk-live-abcdef" not in sealed.ciphertext  # the point of the exercise
    assert (
        box.open(
            sealed.ciphertext,
            tenant_id="t1",
            name="DASHSCOPE_API_KEY",
            key_version=sealed.key_version,
        )
        == "sk-live-abcdef"
    )


def test_the_same_plaintext_seals_to_different_ciphertexts():
    """A fresh nonce per seal; two identical credentials must not look identical."""
    box = _box()
    first = box.seal("same-value", tenant_id="t1", name="K")
    second = box.seal("same-value", tenant_id="t1", name="K")

    assert first.ciphertext != second.ciphertext


@pytest.mark.parametrize(
    ("tenant_id", "name"),
    [("t2", "DASHSCOPE_API_KEY"), ("t1", "ELEVENLABS_API_KEY")],
)
def test_a_ciphertext_is_bound_to_its_tenant_and_name(tenant_id, name):
    """The AAD makes a row's ciphertext non-portable: moving it must fail closed."""
    box = _box()
    sealed = box.seal("sk-live-abcdef", tenant_id="t1", name="DASHSCOPE_API_KEY")

    with pytest.raises(SecretEncryptionError):
        box.open(sealed.ciphertext, tenant_id=tenant_id, name=name, key_version=1)


def test_a_tampered_ciphertext_is_rejected():
    box = _box()
    sealed = box.seal("sk-live-abcdef", tenant_id="t1", name="K")
    raw = bytearray(base64.b64decode(sealed.ciphertext))
    raw[-1] ^= 0x01  # flip one bit of the tag

    with pytest.raises(SecretEncryptionError):
        box.open(
            base64.b64encode(bytes(raw)).decode(),
            tenant_id="t1",
            name="K",
            key_version=1,
        )


def test_a_mismatched_key_version_is_named_in_the_error():
    box = _box()
    sealed = box.seal("v", tenant_id="t1", name="K")

    with pytest.raises(SecretEncryptionError) as excinfo:
        box.open(sealed.ciphertext, tenant_id="t1", name="K", key_version=sealed.key_version + 1)

    assert "key version" in str(excinfo.value)


def test_an_unconfigured_box_is_disabled_rather_than_broken():
    box = SecretBox(key=None)

    assert box.enabled is False
    with pytest.raises(SecretEncryptionError):
        box.seal("v", tenant_id="t1", name="K")


def test_a_huge_secret_round_trips():
    """No length assumption: an RSA key pasted into a credential is still a value."""
    box = _box()
    plaintext = "x" * 20000
    sealed = box.seal(plaintext, tenant_id="t1", name="K")

    assert (
        box.open(sealed.ciphertext, tenant_id="t1", name="K", key_version=1) == plaintext
    )


def test_only_the_managed_prefix_counts_as_a_reference():
    assert is_managed_secret_ref("sec_abc123")
    assert not is_managed_secret_ref("sk-live-abc")
    assert not is_managed_secret_ref(None)
    assert not is_managed_secret_ref("")


def test_the_default_asset_secret_is_only_acceptable_locally():
    assert asset_link_secret_is_secure(DEFAULT_ASSET_LINK_SECRET, app_env="local")
    assert asset_link_secret_is_secure(DEFAULT_ASSET_LINK_SECRET, app_env="test")
    assert not asset_link_secret_is_secure(DEFAULT_ASSET_LINK_SECRET, app_env="production")
    assert asset_link_secret_is_secure("a-real-secret", app_env="production")
    assert not asset_link_secret_is_secure("", app_env="production")


# --------------------------------------------------------------------------- #
# 2. the vault: off by default, on when configured
# --------------------------------------------------------------------------- #


def test_encryption_off_stores_the_plaintext_inline(asgi):
    credential = asgi.store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"DASHSCOPE_API_KEY": "sk-live-1"})
    )

    assert credential.secret_source is ProviderSecretSource.INLINE
    assert credential.encrypted is False
    assert credential.secret_preview == {"DASHSCOPE_API_KEY": "sk-...ve-1"}
    # The pre-V8.4 reading of the same row: values *are* the plaintext.
    assert asgi.store.provider_credentials.environment_overrides() == {
        "DASHSCOPE_API_KEY": "sk-live-1"
    }


def test_encryption_on_stores_a_reference_and_resolves_it(secure_asgi):
    credential = secure_asgi.store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"DASHSCOPE_API_KEY": "sk-live-1"})
    )

    assert credential.secret_source is ProviderSecretSource.VAULT
    assert credential.encrypted is True
    assert credential.secret_preview == {"DASHSCOPE_API_KEY": "sk-...ve-1"}

    record = secure_asgi.store.provider_credentials.get_credential_record(credential.credential_id)
    stored = record.secret_values["DASHSCOPE_API_KEY"]
    assert is_managed_secret_ref(stored)
    assert "sk-live-1" not in stored
    # ...and the tool environment still gets the real value.
    assert secure_asgi.store.provider_credentials.environment_overrides() == {
        "DASHSCOPE_API_KEY": "sk-live-1"
    }


def test_the_api_never_returns_ciphertext_or_references(secure_asgi):
    status, _, body = secure_asgi.request(
        "POST",
        "/api/provider-credentials",
        json_body={"provider": "dashscope", "secret_values": {"DASHSCOPE_API_KEY": "sk-live-1"}},
        actor_id=OWNER,
    )
    assert status == 200, body

    dumped = json.dumps(body)
    assert "secret_abc" not in dumped
    assert body["secret_preview"] == {"DASHSCOPE_API_KEY": "sk-...ve-1"}
    assert body["secret_source"] == "vault"
    assert body["encrypted"] is True


def test_rotating_migrates_legacy_values_and_reseals_the_rest(secure_asgi):
    """The migration path: values written before encryption was enabled."""
    store = secure_asgi.store
    credential = store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"KEEP": "sk-kept-1"})
    )
    record = store.provider_credentials.get_credential_record(credential.credential_id)
    keep_ref = record.secret_values["KEEP"]
    # Simulate a row that predates the key: one vaulted value, one inline one.
    record.secret_values = {"KEEP": keep_ref, "LEGACY": "sk-legacy-1"}
    store.session.commit()

    legacy = store.get_provider_credential(credential.credential_id)
    assert legacy.secret_source is ProviderSecretSource.MIXED

    rotation = store.rotate_provider_credential(credential.credential_id)
    assert (rotation.sealed, rotation.resealed, rotation.locked) == (1, 1, 0)
    assert rotation.credential.secret_source is ProviderSecretSource.VAULT
    assert store.provider_credentials.environment_overrides() == {
        "KEEP": "sk-kept-1",
        "LEGACY": "sk-legacy-1",
    }

    # Idempotent: a second call re-seals both references and seals nothing new.
    again = store.rotate_provider_credential(credential.credential_id)
    assert (again.sealed, again.resealed) == (0, 2)


def test_a_credential_sealed_under_another_key_reports_locked(secure_asgi):
    store = secure_asgi.store
    credential = store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"DASHSCOPE_API_KEY": "sk-live-1"})
    )

    # A key rotation that has not been completed yet.
    other = SecretBox(key="a-different-master-key")
    store.secret_box = other
    store.provider_credentials.secret_box = other

    locked = store.get_provider_credential(credential.credential_id)
    assert locked.secret_source is ProviderSecretSource.LOCKED
    assert locked.encrypted is False
    # Preview is blank rather than showing the reference: a reference is not a
    # glimpse of the secret.
    assert locked.secret_preview == {}
    # And the value is omitted from the environment rather than passed as "sec_...".
    assert store.provider_credentials.environment_overrides() == {}


def test_a_locked_credential_can_be_recovered_by_restoring_the_key(secure_asgi):
    store = secure_asgi.store
    credential = store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"DASHSCOPE_API_KEY": "sk-live-1"})
    )
    other = SecretBox(key="a-different-master-key")
    store.secret_box = other
    store.provider_credentials.secret_box = other
    assert store.get_provider_credential(credential.credential_id).encrypted is False

    restored = _box()
    store.secret_box = restored
    store.provider_credentials.secret_box = restored

    assert store.get_provider_credential(credential.credential_id).encrypted is True


def test_replacing_secrets_retires_the_previous_vault_rows(secure_asgi):
    store = secure_asgi.store
    credential = store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"OLD_KEY": "old-value"})
    )
    old_ref = store.provider_credentials.get_credential_record(
        credential.credential_id
    ).secret_values["OLD_KEY"]

    store.update_provider_credential(
        credential.credential_id, ProviderCredentialUpdate(secret_values={"NEW_KEY": "new-value"})
    )

    assert store.secrets.get(old_ref) is None
    assert store.provider_credentials.environment_overrides() == {"NEW_KEY": "new-value"}


def test_rotating_without_a_key_is_a_server_configuration_error(asgi):
    from apps.api.app.errors import ApiError

    credential = asgi.store.create_provider_credential(
        ProviderCredentialCreate(provider="dashscope", secret_values={"K": "v"})
    )

    with pytest.raises(ApiError) as excinfo:
        asgi.store.rotate_provider_credential(credential.credential_id)

    assert excinfo.value.code == ErrorCode.INSECURE_SECRET_CONFIGURATION
    assert excinfo.value.status_code == 503


def test_checked_credentials_report_tool_status_with_encryption_on(secure_asgi):
    """A reviewer of this code cares about one thing here: refs never reach the registry."""
    status, _, body = secure_asgi.request(
        "POST",
        "/api/provider-credentials",
        json_body={"provider": "dashscope", "secret_values": {"DASHSCOPE_API_KEY": "sk-live-1"}},
        actor_id=OWNER,
    )
    credential_id = body["credential_id"]

    status, _, checked = secure_asgi.request(
        "POST", f"/api/provider-credentials/{credential_id}/check", actor_id=OWNER
    )
    assert status == 200, checked
    assert checked["env_var_names"] == ["DASHSCOPE_API_KEY"]
    assert checked["credential"]["secret_source"] == "vault"


# --------------------------------------------------------------------------- #
# 3. the takeover: provider:manage is tenant-only
# --------------------------------------------------------------------------- #


def test_provider_manage_is_tenant_only_and_has_no_read_twin():
    assert Permission.PROVIDER_MANAGE in TENANT_ONLY_PERMISSIONS
    # The deliberate omission: the read surface carries secret previews, so
    # "who may look" is "who may manage" and a read key would only widen it.
    assert not any(key.value == "provider:read" for key in Permission)
    assert Permission.PROVIDER_MANAGE not in effective_permissions(
        TeamRole.VIEWER.value, ProjectRole.OWNER.value
    )
    assert Permission.PROVIDER_MANAGE in effective_permissions(
        TeamRole.OWNER.value, None
    )


def test_a_project_role_cannot_reach_the_provider_endpoints(asgi):
    """A project owner + team viewer: the union still lacks the tenant-only key."""
    store = asgi.store
    project = store.create_project(ProjectCreate(title="项目"))
    owner = _default_actor()
    team_id = store.actor_seat(owner)[0]
    store.add_team_member(
        team_id, TeamMemberAdd(user_id=PROJECT_OWNER, role=TeamRole.VIEWER.value), owner
    )
    store.session.commit()
    store.add_project_member(
        project.project_id,
        ProjectMemberAdd(user_id=PROJECT_OWNER, role=ProjectRole.OWNER.value),
        owner,
    )
    store.session.commit()

    status, _, body = asgi.request(
        "GET", "/api/provider-credentials", actor_id=PROJECT_OWNER
    )
    assert status == 403, body
    assert body["detail"]["code"] == "permission_denied"


def test_an_unseated_identity_cannot_list_provider_credentials(asgi):
    status, _, body = asgi.request("GET", "/api/provider-credentials", actor_id="ghost")
    assert status == 403, body


def test_an_owner_can_create_list_check_and_rotate(asgi):
    status, _, created = asgi.request(
        "POST",
        "/api/provider-credentials",
        json_body={"provider": "dashscope", "secret_values": {"DASHSCOPE_API_KEY": "sk-live-1"}},
        actor_id=OWNER,
    )
    assert status == 200, created

    status, _, listed = asgi.request("GET", "/api/provider-credentials", actor_id=OWNER)
    assert status == 200 and len(listed) == 1

    status, _, checked = asgi.request(
        "POST", f"/api/provider-credentials/{created['credential_id']}/check", actor_id=OWNER
    )
    assert status == 200, checked

    # Rotation without a key is a 503 with the deployment-fix code, not a 500.
    status, _, rotated = asgi.request(
        "POST", f"/api/provider-credentials/{created['credential_id']}/rotate", actor_id=OWNER
    )
    assert status == 503, rotated
    assert rotated["detail"]["code"] == ErrorCode.INSECURE_SECRET_CONFIGURATION.value


def test_rotating_a_missing_credential_is_a_404(secure_asgi):
    status, _, body = secure_asgi.request(
        "POST", "/api/provider-credentials/cred_missing/rotate", actor_id=OWNER
    )
    assert status == 404, body


def test_rotating_seals_through_the_api(secure_asgi):
    status, _, created = secure_asgi.request(
        "POST",
        "/api/provider-credentials",
        json_body={"provider": "dashscope", "secret_values": {"DASHSCOPE_API_KEY": "sk-live-1"}},
        actor_id=OWNER,
    )
    assert status == 200

    status, _, rotated = secure_asgi.request(
        "POST", f"/api/provider-credentials/{created['credential_id']}/rotate", actor_id=OWNER
    )
    assert status == 200, rotated
    assert rotated["resealed"] == 1
    assert rotated["credential"]["secret_source"] == "vault"
    assert rotated["secrets"][0]["algorithm"] == ALGORITHM


def test_credential_changes_are_audited_without_their_values(asgi):
    status, _, created = asgi.request(
        "POST",
        "/api/provider-credentials",
        json_body={"provider": "dashscope", "secret_values": {"DASHSCOPE_API_KEY": "sk-secret-value"}},
        actor_id=OWNER,
    )
    assert status == 200

    status, _, logs = asgi.request(
        "GET", "/api/audit-logs?action=provider_credential.created", actor_id=OWNER
    )
    assert status == 200, logs
    assert len(logs) == 1
    entry = logs[0]
    assert entry["entity_id"] == created["credential_id"]
    assert entry["payload"]["secret_names"] == ["DASHSCOPE_API_KEY"]
    # The one thing the audit trail must never hold.
    assert "sk-secret-value" not in json.dumps(entry)


# --------------------------------------------------------------------------- #
# 4. the callback: a signature where a permission cannot exist
# --------------------------------------------------------------------------- #


def _sign(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    return sign_webhook(secret, timestamp=timestamp or int(time.time()), body=body)


def test_a_callback_is_accepted_when_no_secret_is_configured(asgi):
    status, _, body = asgi.request(
        "POST",
        "/api/provider-callbacks/dashscope",
        json_body={"external_task_id": "t1", "status": "succeeded"},
    )
    assert status == 200, body


def test_a_configured_secret_refuses_an_unsigned_callback(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "provider_callback_secret", "cb-secret")
    status, _, body = asgi.request(
        "POST",
        "/api/provider-callbacks/dashscope",
        json_body={"external_task_id": "t1", "status": "succeeded"},
    )

    assert status == 401, body
    assert body["detail"]["code"] == ErrorCode.PROVIDER_CALLBACK_INVALID.value


def test_a_configured_secret_accepts_a_signed_callback(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "provider_callback_secret", "cb-secret")
    payload = {"external_task_id": "t1", "status": "succeeded"}
    body = json.dumps(payload).encode()

    status, _, _ = asgi.raw(
        "POST",
        "/api/provider-callbacks/dashscope",
        body=body,
        headers={
            "content-type": "application/json",
            "X-S2V-Signature": _sign("cb-secret", body),
        },
    )
    assert status == 200


def test_a_signature_over_a_different_body_is_refused(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "provider_callback_secret", "cb-secret")
    signed = json.dumps({"external_task_id": "t1", "status": "succeeded"}).encode()
    sent = json.dumps({"external_task_id": "t1", "status": "failed"}).encode()

    status, _, body = asgi.raw(
        "POST",
        "/api/provider-callbacks/dashscope",
        body=sent,
        headers={
            "content-type": "application/json",
            "X-S2V-Signature": _sign("cb-secret", signed),
        },
    )
    assert status == 401
    assert body  # a JSON envelope, not an empty body


def test_a_stale_signature_is_refused(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "provider_callback_secret", "cb-secret")
    body = json.dumps({"external_task_id": "t1", "status": "succeeded"}).encode()

    status, _, _ = asgi.raw(
        "POST",
        "/api/provider-callbacks/dashscope",
        body=body,
        headers={
            "content-type": "application/json",
            "X-S2V-Signature": _sign("cb-secret", body, timestamp=int(time.time()) - 10**6),
        },
    )
    assert status == 401


# --------------------------------------------------------------------------- #
# 5. the doors: upload admission and the signed link
# --------------------------------------------------------------------------- #


def _upload(client, run_id: str, *, content: bytes, filename: str, mime_type: str):
    return client.request(
        "POST",
        f"/api/runs/{run_id}/assets",
        json_body={
            "asset_type": "video",
            "filename": filename,
            "content_base64": base64.b64encode(content).decode(),
            "mime_type": mime_type,
        },
        actor_id=OWNER,
    )


def test_a_normal_upload_is_accepted(asgi):
    _project_id, run_id = _project_and_run(asgi)

    status, _, body = _upload(
        asgi, run_id, content=b"hello world", filename="clip.mp4", mime_type="video/mp4"
    )
    assert status == 200, body


def test_an_oversized_upload_is_a_413(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "asset_upload_max_bytes", 4)
    _project_id, run_id = _project_and_run(asgi)

    status, _, body = _upload(
        asgi, run_id, content=b"way more than four bytes", filename="clip.mp4", mime_type="video/mp4"
    )
    assert status == 413, body
    assert body["detail"]["code"] == ErrorCode.PAYLOAD_TOO_LARGE.value


def test_a_disguised_executable_is_a_415(asgi):
    """Declared mp4, first bytes are an ELF header — the case the magic check exists for."""
    _project_id, run_id = _project_and_run(asgi)

    status, _, body = _upload(
        asgi,
        run_id,
        content=b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 40,
        filename="innocent.mp4",
        mime_type="video/mp4",
    )
    assert status == 415, body
    assert body["detail"]["detected"] == "ELF executable"


def test_a_denied_content_type_is_a_415(asgi):
    _project_id, run_id = _project_and_run(asgi)

    status, _, body = _upload(
        asgi, run_id, content=b"echo hi", filename="run.sh", mime_type="application/x-sh"
    )
    assert status == 415, body
    assert body["detail"]["code"] == ErrorCode.UNSUPPORTED_MEDIA_TYPE.value


def test_an_allowlist_excludes_everything_else(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "asset_allowed_mime_types", "video/mp4,audio/mpeg")
    _project_id, run_id = _project_and_run(asgi)

    allowed, _, _ = _upload(
        asgi, run_id, content=b"data", filename="clip.mp4", mime_type="video/mp4"
    )
    assert allowed == 200

    refused, _, body = _upload(
        asgi, run_id, content=b"data", filename="notes.txt", mime_type="text/plain"
    )
    assert refused == 415, body


def test_an_asset_link_ttl_is_capped(asgi, monkeypatch):
    from apps.api.app.config import settings

    monkeypatch.setattr(settings, "asset_link_max_ttl_seconds", 120)
    _project_id, run_id = _project_and_run(asgi)
    status, _, asset = _upload(
        asgi, run_id, content=b"data", filename="clip.mp4", mime_type="video/mp4"
    )
    assert status == 200

    status, _, link = asgi.request(
        "GET",
        f"/api/assets/{asset['asset_id']}/access-link?expires_in_seconds=99999999",
        actor_id=OWNER,
    )
    assert status == 200, link

    from datetime import UTC, datetime

    expires_at = datetime.fromisoformat(link["expires_at"])
    remaining = (
        expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
    ) - datetime.now(UTC)
    # Capped to the ceiling (generously: the call itself takes a moment).
    assert 0 < remaining.total_seconds() <= 121


def test_the_default_signing_secret_is_refused_outside_local(asgi, monkeypatch):
    from apps.api.app.config import settings

    _project_id, run_id = _project_and_run(asgi)
    status, _, asset = _upload(
        asgi, run_id, content=b"data", filename="clip.mp4", mime_type="video/mp4"
    )
    assert status == 200

    monkeypatch.setattr(settings, "app_env", "production")
    status, _, body = asgi.request(
        "GET", f"/api/assets/{asset['asset_id']}/access-link", actor_id=OWNER
    )
    assert status == 503, body
    assert body["detail"]["code"] == ErrorCode.INSECURE_SECRET_CONFIGURATION.value

    # ...and minting works again the moment a real secret is configured.
    monkeypatch.setattr(settings, "asset_link_secret", "a-real-signing-secret")
    status, _, link = asgi.request(
        "GET", f"/api/assets/{asset['asset_id']}/access-link", actor_id=OWNER
    )
    assert status == 200, link


def test_the_vault_table_is_registered():
    from apps.api.app.db import Base

    assert "encrypted_secrets" in set(Base.metadata.tables)

