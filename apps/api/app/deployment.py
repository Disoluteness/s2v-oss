"""Deployment posture: where the install is, and whether it is accepting writes.

Two things live here, and they are here together because they answer adjacent
questions for the same audience — the person running the box.

**Posture** is read-only introspection: which migration is the code expecting,
which one is the database actually on, and which opt-in capabilities are turned
on. It is computed from things that already exist (the migration scripts, the
settings object, ``alembic_version``) rather than stored, so it can never go
stale — a cache of "we are up to date" is worse than no cache.

**Maintenance mode** is a write guard. It refuses mutating requests while the
install is being repaired — a migration, a restore, a botched provider rollout —
without taking reads down, so the UI can still show what is going on and an
operator can still turn it back off. The switch itself lives in the database
(see ``DeploymentSettingRecord``) precisely because the moment you need it is the
moment you cannot edit ``.env`` and restart.

Nothing in this module touches the store or the request pipeline; that keeps the
policy here testable as plain functions and keeps the imports acyclic.
``enforce_maintenance_mode`` itself lives in ``permission_policy`` next to the
permission guard it is mounted with.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from apps.api.app.config import Settings
from apps.api.app.secrets import asset_link_secret_is_secure

#: The primary key of the single deployment settings row.
DEPLOYMENT_SETTINGS_ID = "default"

#: Methods that never mutate, and therefore may never be refused by maintenance
#: mode. ``HEAD``/``OPTIONS`` are in the set for the same reason as ``GET``: a
#: WebDAV-style probe or a CORS preflight carries no intent to change anything.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Path prefixes that stay reachable while maintenance mode is on.
#:
#: ``/api/deployment`` is unavoidable rather than convenient: the endpoint that
#: turns maintenance mode *off* is a write to that path, so refusing it would make
#: the switch one-way and force a database edit to recover. Reads are not the
#: issue here — only the writes are, and those two writes are the escape hatch.
#:
#: ``/api/provider-callbacks`` is exempt on purpose, and it is the subtler call.
#: A callback carries the *result of a paid provider job*: refusing it does not
#: postpone work, it discards work that has already been billed and that the
#: platform cannot regenerate on its own. The endpoint is already authenticated by
#: HMAC signature (V8.4), so exempting it reopens no door that was closed — while
#: refusing it can turn a maintenance window into an unrecoverable result.
#:
#: ``/api/auth`` (V1.1) is exempt because maintenance suspends *tenant writes*,
#: not the ability to prove who you are. A repair window is exactly when a human
#: needs to sign in instead of riding the default actor, and a login that is
#: itself a maintenance refusal turns the window into a lockout. Logout stays
#: reachable for the same reason in reverse: ending a session must never depend
#: on the deployment being healthy.
MAINTENANCE_EXEMPT_PREFIXES = (
    "/api/deployment",
    "/api/provider-callbacks",
    "/api/auth",
)

#: Individual paths that stay reachable. All are reads in practice (the probe and
#: the scrape endpoint), and are listed explicitly so the exemption is a decision
#: on the record rather than a side effect of the method check.
MAINTENANCE_EXEMPT_PATHS = ("/api/health", "/health", "/metrics")

#: Used when maintenance is switched on without a message.
DEFAULT_MAINTENANCE_MESSAGE = "deployment is in maintenance mode; write requests are suspended"

#: ``alembic.ini`` sits at the repository root, three levels above this file
#: (``apps/api/app/deployment.py``). Resolved from ``__file__`` rather than the
#: working directory so the answer is the same whether the process was started by
#: uvicorn at the root, by a script in ``scripts/``, or inside the image.
_REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
ALEMBIC_MIGRATIONS = _REPO_ROOT / "infra" / "migrations"

#: How far the walk up the revision chain may go before it gives up. A guard
#: against a malformed history turning a status endpoint into a hang; the real
#: chain is currently twenty-one revisions long.
_MAX_REVISION_WALK = 10_000


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS


def is_maintenance_exempt(method: str, path: str) -> bool:
    """Whether this request may proceed while maintenance mode is on.

    Reads always may; a couple of writes are exempt for the reasons recorded on
    ``MAINTENANCE_EXEMPT_PREFIXES``.

    Prefix matching is on *path segments*, not raw characters: ``/api/deployment``
    covers ``/api/deployment/maintenance`` (the escape hatch sits behind it, so a
    literal comparison would leave the off switch unreachable) but not
    ``/api/deployment-evil``. A bare ``startswith`` would grant an exemption to
    every path that merely begins with the same characters, which is exactly the
    kind of opening a write guard must not have.
    """
    if is_safe_method(method):
        return True
    if path in MAINTENANCE_EXEMPT_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in MAINTENANCE_EXEMPT_PREFIXES)


def _script_directory() -> ScriptDirectory | None:
    """The migration scripts, with ``script_location`` pinned to an absolute path.

    ``alembic.ini`` names the directory relatively, which resolves against the
    current working directory — fine for the documented invocation, silently
    wrong for any other. Overriding it here means the answer does not depend on
    where the process happens to have been started.
    """
    if not ALEMBIC_INI.exists():
        return None
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_MIGRATIONS))
    try:
        return ScriptDirectory.from_config(config)
    except CommandError:
        # Alembic's own error for "the script location is not a migrations
        # directory". A status endpoint exists to be readable when something is
        # wrong, so a mangled checkout reports "cannot tell" like any other
        # unknown — it does not become a second, louder failure.
        return None


def alembic_head() -> str | None:
    """The revision the shipped migration scripts end at, or ``None``.

    ``None`` means "cannot tell" (no scripts on disk, e.g. a stripped image), and
    every caller is expected to report that rather than guess. There is a single
    head today; if the history ever branches, the first sorted head is reported —
    a status endpoint should not be the thing that fails loudly about it.
    """
    script = _script_directory()
    if script is None:
        return None
    heads = sorted(script.get_heads())
    return heads[0] if heads else None


def alembic_pending(current: str | None) -> list[str]:
    """Revisions between ``current`` and the head, in the order they must run.

    ``current`` is what ``alembic_version`` holds, or ``None`` when that cannot be
    read. An empty list therefore means two different things — "already at head"
    and "cannot tell" — which is why the caller publishes ``current`` and ``head``
    alongside it: ``up_to_date`` is only meaningful when both are known, and an
    operator reads ``current: null`` as the difference.

    An unknown ``current`` reports *no* pending revisions on purpose. The full
    chain would be the literal reading ("nothing is known to be applied"), but
    printing twenty-one migrations as outstanding against a database that is
    probably fine is how a status endpoint trains people to ignore it.
    """
    if current is None:
        return []
    head = alembic_head()
    script = _script_directory()
    if head is None or script is None or current == head:
        return []
    pending: list[str] = []
    revision: str | None = head
    steps = 0
    while revision is not None and revision != current and steps < _MAX_REVISION_WALK:
        pending.append(revision)
        entry = script.get_revision(revision)
        down = entry.down_revision if entry is not None else None
        if down is not None and not isinstance(down, str):
            # A merge point. This history is linear, so reaching one means we are
            # looking at something unexpected; stop rather than invent an order.
            break
        revision = down
        steps += 1
    if revision != current:
        # ``current`` is not an ancestor of the head (a divergent or unknown
        # revision). Report the whole chain: a truncated prefix would read as
        # "nearly up to date", which is the one conclusion that is certainly wrong.
        pending.reverse()
        return pending
    pending.reverse()
    return pending


def deployment_capabilities(settings: Settings) -> dict[str, bool]:
    """Which opt-in features this process has switched on, as booleans.

    Booleans and nothing else, deliberately. The point is posture at a glance —
    "is encryption on here?" — and a value-bearing config dump on an endpoint is
    how secrets end up in a dashboard screenshot. Each key maps to one visible
    behaviour, not to one environment variable, so the answer stays meaningful if
    a variable is ever split or renamed.
    """
    return {
        "secret_encryption": bool(settings.secret_encryption_key),
        "provider_callback_signature": bool(settings.provider_callback_secret),
        "asset_link_secret_secure": asset_link_secret_is_secure(
            settings.asset_link_secret,
            app_env=settings.app_env,
        ),
        "structured_logs": settings.log_format.strip().lower() == "json",
        "webhook_dispatch": bool(settings.webhook_dispatch_enabled),
    }
