"""Secret material the platform has to be able to *recover* (V8.4).

The API key module answered "how do we verify a credential we must never learn".
This one answers the opposite question: provider keys are handed to a tool
process as environment variables, so the platform has to get the plaintext back —
hashing is not available to it. The only way to keep such a value out of a
database dump is to encrypt it, and that is what ``SecretBox`` does.

Three decisions shape everything below.

**Encryption is opt-in.** An empty key means the box is disabled and secrets are
stored exactly the way they were before this version. Turned on by default, every
existing deployment would silently gain a new single point of failure — lose the
environment variable and every paid provider call stops working — without ever
having agreed to it. Turned on explicitly, the team that sets the key has made a
commitment it can reason about.

**A key is a string, not a blob.** The configured value is hashed to 256 bits
rather than parsed as base64, because parsing buys nothing (it is hashed to the
same length either way) and costs a failure mode: a deployment that pastes a
perfectly good passphrase and gets "not 32 bytes" would either crash or — far
worse — silently fall back to plaintext.

**The associated data is the identity of the secret.** ``tenant_id``, ``name``
and ``key_version`` are authenticated but not encrypted, so a ciphertext lifted
from one row and pasted into another fails to open instead of decrypting into
somebody else's credential.

Nothing here touches a database: the box takes and returns strings, which is what
makes tamper detection, AAD binding and key-version mismatch testable without
opening a single connection.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Stamped on every row so a future rotation is a data migration over the vault
#: alone: nothing else needs to remember which key sealed a given blob.
ALGORITHM = "AESGCM-256"
DEFAULT_KEY_VERSION = 1

#: Managed secret references are opaque pointers, never secret content. The
#: prefix is what lets a stored value be classified without a second column.
MANAGED_SECRET_PREFIX = "sec_"

#: 96 bits, the size AES-GCM is specified for. Prefixed to each ciphertext.
NONCE_BYTES = 12

#: The shipped development secret for signed asset links. Public by definition —
#: a link signed with it can be minted by anyone who has read the source.
DEFAULT_ASSET_LINK_SECRET = "dev-only-change-me"

#: Environments where a development default is an acceptable secret. Anywhere
#: else, signing with the default is refused rather than merely logged.
DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "dev", "development", "test", "testing"})


class SecretEncryptionError(RuntimeError):
    """Raised when a secret cannot be sealed or opened.

    Deliberately *not* a gate error: it never means "the caller may not", only
    "this deployment cannot", so a caller retrying the identical request will not
    succeed and should not bother.
    """


@dataclass(frozen=True)
class SealedSecret:
    """A ciphertext plus everything needed to open it later."""

    ciphertext: str
    algorithm: str
    key_version: int


def _associated_data(tenant_id: str, name: str, key_version: int) -> bytes:
    # Joined with a separator that cannot occur in either field, so ("a|b", "c")
    # and ("a", "b|c") do not collide into the same AAD.
    return f"{tenant_id}\x1f{name}\x1f{key_version}".encode()


def is_managed_secret_ref(value: object) -> bool:
    """Whether ``value`` is a pointer into the vault rather than inline material.

    The check is deliberately structural, not a lookup: a ``sec_`` value whose
    row has gone missing must still be classified as "managed but unresolvable",
    or it would be handed to a provider as if the prefix were the API key.
    """
    return isinstance(value, str) and value.startswith(MANAGED_SECRET_PREFIX)


class SecretBox:
    """AES-256-GCM sealing for values the platform must hand back out."""

    def __init__(self, *, key: str | None, key_version: int = DEFAULT_KEY_VERSION) -> None:
        raw = (key or "").strip()
        self.key_version = key_version
        self._aead = AESGCM(hashlib.sha256(raw.encode()).digest()) if raw else None

    @property
    def enabled(self) -> bool:
        return self._aead is not None

    def seal(self, plaintext: str, *, tenant_id: str, name: str) -> SealedSecret:
        if self._aead is None:
            raise SecretEncryptionError(
                "secret encryption is not configured; set SECRET_ENCRYPTION_KEY"
            )
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(
            nonce,
            plaintext.encode(),
            _associated_data(tenant_id, name, self.key_version),
        )
        return SealedSecret(
            ciphertext=base64.b64encode(nonce + sealed).decode(),
            algorithm=ALGORITHM,
            key_version=self.key_version,
        )

    def open(
        self,
        ciphertext: str,
        *,
        tenant_id: str,
        name: str,
        key_version: int,
        algorithm: str = ALGORITHM,
    ) -> str:
        if self._aead is None:
            raise SecretEncryptionError(
                "secret encryption is not configured; set SECRET_ENCRYPTION_KEY"
            )
        if algorithm != ALGORITHM:
            raise SecretEncryptionError(f"unsupported secret algorithm '{algorithm}'")
        if key_version != self.key_version:
            # Naming the versions is safe: they are integers, not key material,
            # and "sealed with v1, running v2" is the entire diagnosis.
            raise SecretEncryptionError(
                f"secret was sealed with key version {key_version}, "
                f"the configured key is version {self.key_version}"
            )
        try:
            raw = base64.b64decode(ciphertext)
        except (ValueError, TypeError) as exc:
            raise SecretEncryptionError("secret ciphertext is not valid base64") from exc
        if len(raw) <= NONCE_BYTES:
            raise SecretEncryptionError("secret ciphertext is truncated")
        nonce, payload = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        try:
            return self._aead.decrypt(
                nonce, payload, _associated_data(tenant_id, name, key_version)
            ).decode()
        except InvalidTag as exc:
            raise SecretEncryptionError(
                "secret failed authentication (wrong key, wrong identity, or tampering)"
            ) from exc
        except UnicodeDecodeError as exc:
            raise SecretEncryptionError("secret decrypted to invalid UTF-8") from exc


def asset_link_secret_is_secure(secret: str, *, app_env: str) -> bool:
    """Whether signed links may be minted with this secret in this environment.

    A development default is fine locally and unacceptable anywhere else, and the
    remedy (set ``ASSET_LINK_SECRET``) is one the operator can carry out, which is
    what makes refusing louder than warning: a warning on a mint that succeeds is
    read by nobody.
    """
    if app_env.strip().lower() in DEVELOPMENT_ENVIRONMENTS:
        return True
    return bool(secret) and secret != DEFAULT_ASSET_LINK_SECRET


def signature_matches(
    secret: str,
    *,
    header: str | None,
    body: bytes,
    tolerance_seconds: int,
) -> bool:
    """Verify an ``X-S2V-Signature`` header against ``body``.

    Reuses the V7.3 webhook scheme verbatim — ``t=<unix>,v1=<hex>`` over
    ``"{t}.{body}"`` — because the two exchanges differ in *who holds the secret*
    (a configured value here, a subscription row there), not in what the signature
    has to prove. Sharing the format means a provider integrator who has already
    implemented the webhook receiver needs no second parser.
    """
    from apps.api.app.webhooks import verify_webhook_signature

    return verify_webhook_signature(
        secret,
        header=header,
        body=body,
        tolerance_seconds=tolerance_seconds,
    )
