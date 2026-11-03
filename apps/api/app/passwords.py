"""Password hashing and session-token cryptography (V1.1).

A leaf module on purpose: it imports configuration and small helpers only, so
both the store (which verifies credentials) and the identity dependency (which
parks sessions on the request) can use it without an import cycle.

Three decisions are worth recording:

* **Passwords use ``hashlib.scrypt``** — the standard-library KDF, no new
  dependency. Each hash carries its own random salt and its own cost
  parameters, encoded in the stored string, so costs can rise later without a
  migration: old rows keep verifying, new rows get written with the current
  parameters.
* **Sessions mirror the API key storage split**: a short ``token_prefix`` for
  the point lookup, a peppered digest for the constant-time comparison, and the
  plaintext returned once at login and never persisted. The digest reuses
  ``hash_api_key`` — one credential pepper for every bearer credential the
  platform issues, so the rotation lever stays a single switch.
* **Every verification failure is the same quiet ``False`` / ``None``.** No
  hash at all, a hash from some other scheme, a truncated string — none of them
  is a different answer than "credentials do not match", so a malformed row
  neither crashes the login nor turns it into an oracle.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from apps.api.app.actor import SESSION_TOKEN_PREFIX
from apps.api.app.config import settings
from apps.api.app.repositories import as_utc, hash_api_key

# scrypt parameters (RFC 7914). ``n`` must stay a power of two. These are the
# parameters *new* hashes are written with; verification reads the parameters
# out of the stored string, so raising them later needs no data migration.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

HASH_ALGORITHM = "scrypt"

#: A session token looks like ``s2v_sess_<16 hex>.<32 hex>``; the lookup handle
#: is everything before the dot. The dot separator keeps the session shape from
#: colliding with the underscore-shaped API keys even by accident.
_PREFIX_HEX_CHARS = 16
_SECRET_HEX_CHARS = 32


# --- password hashing ---------------------------------------------------------


def hash_password(password: str) -> str:
    """Encode a password as ``scrypt$n$r$p$<salt hex>$<hash hex>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"{HASH_ALGORITHM}${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Check a password against a stored hash; any malformed hash is just False."""
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != HASH_ALGORITHM:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except (ValueError, TypeError):
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN
    )
    return hmac.compare_digest(actual, expected)


# --- session tokens ------------------------------------------------------------


def generate_session_token() -> tuple[str, str]:
    """Return ``(lookup prefix, plaintext)`` for a fresh session token."""
    handle = secrets.token_hex(_PREFIX_HEX_CHARS)
    secret = secrets.token_hex(_SECRET_HEX_CHARS)
    prefix = f"{SESSION_TOKEN_PREFIX}{handle}"
    return prefix, f"{prefix}.{secret}"


def session_token_prefix_of(plaintext: str) -> str | None:
    """Recover the lookup handle, or ``None`` for anything not session-shaped."""
    token = plaintext.strip()
    if not token.startswith(SESSION_TOKEN_PREFIX):
        return None
    handle, dot, _secret = token[len(SESSION_TOKEN_PREFIX) :].partition(".")
    if not dot or not handle:
        return None
    return f"{SESSION_TOKEN_PREFIX}{handle}"


def hash_session_token(plaintext: str) -> str:
    """The peppered digest that is actually stored; never the plaintext."""
    return hash_api_key(plaintext.strip())


def default_session_expiry(*, now: datetime | None = None) -> datetime:
    """When a session minted now stops verifying."""
    moment = as_utc(now) or datetime.now(UTC)
    return moment + timedelta(hours=settings.session_ttl_hours)
