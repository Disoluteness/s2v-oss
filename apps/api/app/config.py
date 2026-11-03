from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    app_name: str = "script2video-studio"
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    database_url: str = "postgresql+psycopg://script2video:script2video@localhost:5432/script2video"
    redis_url: str = "redis://localhost:6379/0"
    object_storage_backend: str = "local"
    object_storage_endpoint: str = "http://localhost:9000"
    object_storage_bucket: str = "script2video"
    local_storage_root: str = "storage"
    asset_link_secret: str = "dev-only-change-me"
    asset_link_default_ttl_seconds: int = 600
    public_base_url: str | None = None

    # Identity / permission layer (V6.5). ``actor_trust_mode`` controls whether the
    # ``X-Actor-Id`` header is honoured outright ("open", the default for a single
    # internal deployment) or only together with a matching ``X-Actor-Secret``
    # ("secret"). Requests with no identity fall back to ``default_actor_id``, which
    # is registered as the owner of the default team, so the existing unauthenticated
    # callers keep working.
    actor_trust_mode: str = "open"
    actor_header_secret: str | None = None
    default_actor_id: str = "system"
    default_actor_name: str = "Internal System"
    default_team_slug: str = "default"
    default_team_name: str = "Default Team"

    # Idempotency (V7.1). A claim older than this is treated as abandoned and may
    # be taken over by a retry; the recorded response is kept for the same window.
    idempotency_ttl_seconds: int = 86400

    # Programmatic credentials (V7.2). ``api_key_pepper`` is mixed into the stored
    # digest, so a dump of ``api_keys`` alone cannot be turned into usable keys.
    # Rotating it invalidates every issued key, which is the intended emergency
    # lever; leave it empty for a single internal deployment.
    api_key_pepper: str | None = None
    api_key_default_expires_days: int | None = None

    # Username-password sessions (V1.1). A session token's stored digest mixes in
    # ``api_key_pepper`` — one credential pepper for every bearer credential the
    # platform issues, so the rotation lever stays a single switch. The TTL bounds
    # how long a login stays usable without a re-prompt.
    session_ttl_hours: int = 72

    # Outbound webhooks (V7.3). Delivery is drained by the worker; these tune the
    # exchange and the auto-pause threshold rather than the retry policy, which is
    # per-delivery state (``max_attempts`` on each outbox row). Setting
    # ``webhook_dispatch_enabled`` to false turns the worker's webhook loop into a
    # no-op for deployments that never subscribe.
    webhook_dispatch_enabled: bool = True
    webhook_request_timeout_seconds: float = 10.0
    webhook_lease_seconds: int = 300
    webhook_backoff_base_seconds: float = 30.0
    webhook_backoff_cap_seconds: float = 3600.0
    webhook_disable_after_failures: int = 20
    webhook_batch_size: int = 10

    # Embeddable workspace (V7.4). ``embed_token_secret`` signs the stateless
    # embed links an external platform opens for its end users. It is destructive
    # to rotate — every link in flight stops verifying — which is exactly what
    # makes it the emergency lever, the same stance ``api_key_pepper`` takes.
    # The TTLs bound how long a link that lives in a URL stays usable.
    embed_token_secret: str | None = None
    embed_token_default_ttl_seconds: int = 900
    embed_token_max_ttl_seconds: int = 86400

    # Worker scaling (V8.1). Automatic retries back off exponentially between
    # ``base * 2**(attempt-1)`` and ``cap`` seconds, and only for job types that
    # are safe to resubmit (local ffmpeg/subtitle/analysis work — never the paid
    # avatar submission). The reaper fails jobs stuck in ``running`` longer than
    # ``job_timeout_seconds``; ``waiting_provider`` is exempt because provider
    # polls are slow by nature.
    worker_job_timeout_seconds: int = 1800
    worker_retry_backoff_base_seconds: float = 30.0
    worker_retry_backoff_cap_seconds: float = 3600.0

    # Observability (V8.2). ``log_format="json"`` switches every log line to one
    # JSON object per line (text stays the default so existing deployments and
    # shell pipelines see no change); ``log_level`` is the root logger level.
    # Prometheus scrapes ``/metrics`` — it is intentionally unauthenticated and
    # outside the ``/api`` permission contract; put it behind the network edge
    # if the port is reachable from outside the cluster.
    log_format: str = "text"
    log_level: str = "INFO"

    # Security hardening (V8.4). Encryption of provider secrets is **opt-in**:
    # an empty ``secret_encryption_key`` means secrets are stored exactly as they
    # were before this version, so no existing deployment changes behaviour by
    # upgrading. Setting it moves secret material into ``encrypted_secrets`` as
    # AES-256-GCM ciphertext; the raw value is hashed to 256 bits, so any
    # high-entropy string works (``openssl rand -base64 32``). Rotating it
    # requires re-sealing — see ``POST /api/provider-credentials/{id}/rotate``.
    secret_encryption_key: str | None = None
    # Uploaded assets are bounded before and after base64 decoding: the encoded
    # length is checked first so an oversized body is refused without being
    # decoded into memory at all.
    asset_upload_max_bytes: int = 268435456
    asset_allowed_mime_types: str = ""
    asset_content_type_denylist: str = (
        "application/x-msdownload,application/x-dosexec,application/x-executable,"
        "application/x-mach-binary,application/x-sh,application/x-shellscript,"
        "text/x-shellscript,application/x-httpd-php,application/x-msdos-program,"
        "application/vnd.microsoft.portable-executable"
    )
    # Signed asset links are capped in TTL and refused outright while the
    # built-in development secret is still in use outside a local environment:
    # that secret is public, so any link it signs can be forged by anyone.
    asset_link_max_ttl_seconds: int = 3600
    # Provider callbacks carry no platform identity, so authenticity is a
    # signature rather than a permission. Unset means "accept" — the platform
    # cannot decide on the upstream's behalf, and local runs have no secret.
    provider_callback_secret: str | None = None
    provider_callback_tolerance_seconds: int = 300

    # Deployment posture (V8.5). ``deployment_name`` is a label and nothing more:
    # it is the name an operator's dashboard prints, never a value the platform
    # branches on — which is why an unset one is fine and just reads back as the
    # app name. Maintenance mode is deliberately *absent* from this list. It is a
    # switch that has to be flippable while the install is being repaired, i.e.
    # at exactly the moment editing a file and restarting is the thing you cannot
    # do; keeping it in the database is what makes it usable.
    deployment_name: str | None = None
    deployment_backup_retention_days: int = 14

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
