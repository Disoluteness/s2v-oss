#!/usr/bin/env python3
"""Pre-flight checks for a Script2Video Studio deployment (V8.5).

Answers the questions that decide whether a deployment is fit to serve, without
asking it to serve anything. It talks to the database and to configuration
directly rather than over HTTP, for one reason: half of what it checks is what
the API would do *before* it can answer a request — an unset signing secret, a
schema behind its own code, a database that cannot be reached at all. An HTTP
probe cannot report those, because it cannot get in.

Run it after applying migrations and before opening the doors:

    docker compose -f docker-compose.prod.yml run --rm --entrypoint python migrate \\
      scripts/deployment_preflight.py

    # or from the host, against whatever DATABASE_URL points at
    python scripts/deployment_preflight.py --database-url "$DATABASE_URL"

Exit codes: 0 nothing failed (warnings are allowed), 1 a check failed, 2 usage.
That is the same convention as ``s2v`` and ``restore.sh``, so a wrapper script
does not have to know which tool refused.

Every check reports the value it saw. A pre-flight report that says "FAIL" and
nothing else is a report someone has to reproduce by hand.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.config import Settings
from apps.api.app.config import settings as default_settings
from apps.api.app.deployment import alembic_head, alembic_pending
from apps.api.app.secrets import DEVELOPMENT_ENVIRONMENTS, asset_link_secret_is_secure
from apps.api.app.version import APP_VERSION

OK = "ok"
WARN = "warn"
FAIL = "fail"

_ICONS = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def _config_checks(config: Settings) -> list[Check]:
    """Everything answerable from configuration alone.

    Ordering is deliberate: the checks that make a deployment *unable to serve a
    documented feature* come before the ones that merely make it a poor idea, so
    the first line of output is the thing worth fixing first.
    """
    checks: list[Check] = []

    secure = asset_link_secret_is_secure(config.asset_link_secret, app_env=config.app_env)
    checks.append(
        _check(
            "asset_link_secret",
            OK if secure else FAIL,
            (
                f"app_env={config.app_env!r} accepts the current secret"
                if secure
                else (
                    "app_env is "
                    f"{config.app_env!r} but ASSET_LINK_SECRET is still the development "
                    "default; every mint of /assets/{id}/access-link will be refused "
                    "with 503 insecure_secret_configuration"
                )
            ),
        )
    )

    env_is_dev = config.app_env.strip().lower() in DEVELOPMENT_ENVIRONMENTS
    checks.append(
        _check(
            "app_env",
            WARN if env_is_dev else OK,
            (
                f"{config.app_env!r} is a development environment; if this is a "
                "production host the environment label is wrong"
                if env_is_dev
                else config.app_env
            ),
        )
    )

    trust_mode = (config.actor_trust_mode or "").strip().lower()
    if trust_mode == "secret":
        checks.append(
            _check(
                "actor_trust_mode",
                OK if config.actor_header_secret else FAIL,
                (
                    "secret mode with ACTOR_HEADER_SECRET configured"
                    if config.actor_header_secret
                    else (
                        "ACTOR_TRUST_MODE=secret but ACTOR_HEADER_SECRET is empty: no "
                        "header identity is trusted at all, so every caller falls back "
                        "to the default actor"
                    )
                ),
            )
        )
    else:
        checks.append(
            _check(
                "actor_trust_mode",
                WARN if trust_mode == "open" else FAIL,
                (
                    "open mode trusts X-Actor-Id from anyone who can reach the API; "
                    "only acceptable when no untrusted traffic can reach it"
                    if trust_mode == "open"
                    else f"unrecognised trust mode {config.actor_trust_mode!r}"
                ),
            )
        )

    checks.append(
        _check(
            "api_key_pepper",
            OK if config.api_key_pepper else WARN,
            (
                "set; issued API keys are salted"
                if config.api_key_pepper
                else "empty; key digests are unsalted (still one-way, but not salted)"
            ),
        )
    )

    checks.append(
        _check(
            "embed_token_secret",
            OK if config.embed_token_secret else WARN,
            (
                "set; embed links are signed with it"
                if config.embed_token_secret
                else "empty; embed links fall back to the development default, which "
                "anyone can forge — fine for local smoke tests, not for a host"
            ),
        )
    )

    # The two optionals are reported, never failed. Both are opt-in by design
    # (V8.2 / V8.4), and a pre-flight that turned "you have not adopted this yet"
    # into a failure would make the tool unusable on the deployments it is
    # supposed to describe.
    checks.append(
        _check(
            "secret_encryption",
            OK,
            (
                "SECRET_ENCRYPTION_KEY set; new provider secrets are sealed at rest"
                if config.secret_encryption_key
                else "off; provider secrets are stored inline exactly as before V8.4 "
                "(set SECRET_ENCRYPTION_KEY to adopt it, then POST .../rotate once)"
            ),
        )
    )

    checks.append(
        _check(
            "provider_callback_signature",
            OK,
            (
                "PROVIDER_CALLBACK_SECRET set; unsigned callbacks are refused"
                if config.provider_callback_secret
                else "off; provider callbacks are accepted unsigned (upstreams that "
                "cannot sign are unaffected)"
            ),
        )
    )

    checks.append(
        _check(
            "public_base_url",
            OK if config.public_base_url else WARN,
            (
                config.public_base_url
                if config.public_base_url
                else "empty; embed and asset links are built from the request Host "
                "header, which is the internal hop behind a gateway"
            ),
        )
    )

    return checks


def _database_checks(config: Settings, engine: Any, database_url: str) -> list[Check]:
    """Everything that needs a live connection. Ordered so the first failure is
    the one to fix first — a schema question is meaningless without a connection."""
    checks: list[Check] = []
    # Credentials are stripped from anything printed: a pre-flight report ends up
    # in a terminal scrollback, a CI log and often a chat message.
    target = database_url.rsplit("@", 1)[-1]

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return [_check("database", FAIL, f"cannot connect to {target}: {exc.__class__.__name__}")]
    checks.append(_check("database", OK, f"connected to {target}"))

    head = alembic_head()
    try:
        current: str | None = None
        with engine.connect() as connection:
            if inspect(connection).has_table("alembic_version"):
                value = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                current = str(value) if value is not None else None
    except SQLAlchemyError as exc:
        current = None
        checks.append(
            _check("schema_revision", FAIL, f"cannot read alembic_version: {exc.__class__.__name__}")
        )
    if head is None:
        checks.append(
            _check("schema_revision", FAIL, "cannot determine the head revision from the migrations")
        )
    elif current is None:
        checks.append(
            _check(
                "schema_revision",
                FAIL,
                f"alembic_version is absent or empty; run 'alembic upgrade head' (head={head})",
            )
        )
    else:
        pending = alembic_pending(current)
        checks.append(
            _check(
                "schema_revision",
                OK if not pending else FAIL,
                (
                    f"{current} is the head the shipped code expects"
                    if not pending
                    else f"database is at {current} but the code expects {head}; "
                    f"{len(pending)} migration(s) pending"
                ),
            )
        )

    try:
        with engine.connect() as connection:
            tables = [
                # Alembic's own bookkeeping table is not part of the application
                # schema, and counting it would make this number disagree with the
                # one in the docs and in the tests for no useful reason.
                name
                for name in inspect(connection).get_table_names()
                if name != "alembic_version"
            ]
        checks.append(_check("tables", OK if tables else FAIL, f"{len(tables)} tables"))
    except SQLAlchemyError as exc:
        checks.append(_check("tables", FAIL, f"introspection failed: {exc.__class__.__name__}"))

    # Deployment settings (V8.5). Absent on a database assembled from the ORM
    # metadata rather than the migrations, which is worth knowing before someone
    # wonders why /api/deployment reports configured=false. ``maintenance_mode``
    # and ``last_backup`` are reported either way: with no row the code's own
    # fallback is "not in maintenance", so that answer is still knowable, and a
    # report that omits the two questions an operator actually asks during an
    # incident would be a worse report.
    row: Any = None
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("deployment_settings"):
                checks.append(
                    _check(
                        "deployment_settings",
                        FAIL,
                        "table missing; migrations have not been applied to this database",
                    )
                )
            else:
                row = connection.execute(
                    text(
                        "SELECT maintenance_mode, last_backup_at, backup_retention_days "
                        "FROM deployment_settings WHERE id = 'default'"
                    )
                ).first()
                if row is None:
                    checks.append(
                        _check(
                            "deployment_settings",
                            WARN,
                            "no singleton row; /api/deployment will report defaults with "
                            "configured=false (the migration seeds it, hand-built "
                            "databases do not)",
                        )
                    )
                else:
                    checks.append(_check("deployment_settings", OK, "singleton row present"))
    except SQLAlchemyError as exc:
        checks.append(
            _check("deployment_settings", FAIL, f"read failed: {exc.__class__.__name__}")
        )

    maintenance, last_backup_at, retention = row if row is not None else (False, None, None)
    checks.append(
        _check(
            "maintenance_mode",
            WARN if maintenance else OK,
            (
                "ON — mutating requests are being refused; turn it off "
                "(POST /api/deployment/maintenance) once the maintenance window is over"
                if maintenance
                else "off"
            ),
        )
    )
    checks.append(_backup_check(last_backup_at, retention))

    return checks


def _backup_check(last_backup_at: datetime | str | None, retention: int | None) -> Check:
    """Age the last recorded backup against the configured retention window.

    A deployment with no backup on record is not blocked from serving — plenty of
    internal deployments are restorable from a snapshot the platform cannot see.
    It is a warning instead, and the wording is deliberately specific: "no backup
    recorded" is a fact about the bookkeeping, not a claim that none exists.
    """
    if last_backup_at is None:
        return _check(
            "last_backup",
            WARN,
            "none recorded; scripts/backup.sh --record makes this answerable "
            "(a missed backup is survivable until the first bad migration)",
        )
    moment = last_backup_at
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            return _check("last_backup", WARN, f"unparseable timestamp: {last_backup_at!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    age_days = (datetime.now(UTC) - moment).total_seconds() / 86400.0
    window = retention if retention and retention > 0 else 14
    return _check(
        "last_backup",
        WARN if age_days > window else OK,
        f"{age_days:.1f} days old (retention window {window} days)",
    )


def run_preflight(*, config: Settings, engine: Any, database_url: str | None = None) -> list[Check]:
    """Every check, in the order a person would fix them."""
    return _config_checks(config) + _database_checks(
        config,
        engine,
        database_url or config.database_url,
    )


def _render(checks: list[Check], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {
                "app_version": APP_VERSION,
                "checks": [asdict(check) for check in checks],
                "failed": sum(1 for check in checks if check.status == FAIL),
                "warned": sum(1 for check in checks if check.status == WARN),
            },
            ensure_ascii=False,
            indent=2,
        )
    width = max(len(check.name) for check in checks)
    lines = [f"deployment preflight (app {APP_VERSION})"]
    for check in checks:
        lines.append(f"[{_ICONS[check.status]}] {check.name.ljust(width)}  {check.detail}")
    failed = sum(1 for check in checks if check.status == FAIL)
    warned = sum(1 for check in checks if check.status == WARN)
    lines.append(f"\n{len(checks)} checks, {warned} warning(s), {failed} failure(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override DATABASE_URL for the checks (defaults to the configured one)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    config = default_settings
    database_url = args.database_url or config.database_url
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        checks = run_preflight(config=config, engine=engine, database_url=database_url)
    finally:
        engine.dispose()

    print(_render(checks, as_json=args.json))
    return 1 if any(check.status == FAIL for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
