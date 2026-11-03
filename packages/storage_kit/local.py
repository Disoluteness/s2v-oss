from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from packages.storage_kit.models import StoredObject, TemporaryAccessLink


class LocalObjectStorage:
    """Filesystem-backed object storage used for local development and tests."""

    def __init__(
        self,
        *,
        root: str | Path,
        bucket: str,
        link_secret: str,
        public_path_prefix: str = "/api/assets",
    ) -> None:
        self.root = Path(root)
        self.bucket = bucket
        self.link_secret = link_secret
        self.public_path_prefix = public_path_prefix.rstrip("/")

    def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        mime_type: str | None = None,
    ) -> StoredObject:
        safe_key = self._safe_object_key(object_key)
        target = self._path_for_key(safe_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return StoredObject(
            object_key=safe_key,
            storage_url=self._storage_url(safe_key),
            checksum=hashlib.sha256(content).hexdigest(),
            file_size=len(content),
            mime_type=mime_type,
        )

    def read_bytes(self, storage_url: str) -> bytes:
        return self._path_for_url(storage_url).read_bytes()

    def exists(self, storage_url: str) -> bool:
        return self._path_for_url(storage_url).is_file()

    def temporary_url(
        self,
        *,
        asset_id: str,
        storage_url: str,
        expires_in_seconds: int,
    ) -> TemporaryAccessLink:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        expires = int(expires_at.timestamp())
        signature = self.sign(asset_id=asset_id, storage_url=storage_url, expires=expires)
        url = f"{self.public_path_prefix}/{asset_id}/content?expires={expires}&signature={signature}"
        return TemporaryAccessLink(url=url, expires_at=expires_at)

    def sign(self, *, asset_id: str, storage_url: str, expires: int) -> str:
        payload = f"{asset_id}.{storage_url}.{expires}".encode()
        return hmac.new(self.link_secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify(self, *, asset_id: str, storage_url: str, expires: int, signature: str) -> bool:
        now = int(datetime.now(UTC).timestamp())
        if expires < now:
            return False
        expected = self.sign(asset_id=asset_id, storage_url=storage_url, expires=expires)
        return hmac.compare_digest(expected, signature)

    def _storage_url(self, object_key: str) -> str:
        return f"local://{self.bucket}/{object_key}"

    def _path_for_url(self, storage_url: str) -> Path:
        prefix = f"local://{self.bucket}/"
        if not storage_url.startswith(prefix):
            raise ValueError("unsupported storage url")
        return self._path_for_key(storage_url.removeprefix(prefix))

    def _path_for_key(self, object_key: str) -> Path:
        safe_key = self._safe_object_key(object_key)
        root = self.root.resolve()
        path = (root / safe_key).resolve()
        if root != path and root not in path.parents:
            raise ValueError("object key escapes storage root")
        return path

    def _safe_object_key(self, object_key: str) -> str:
        path = PurePosixPath(object_key)
        if path.is_absolute():
            raise ValueError("object key must be relative")
        parts = [part for part in path.parts if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            raise ValueError("invalid object key")
        return "/".join(parts)
