from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StoredObject:
    object_key: str
    storage_url: str
    checksum: str
    file_size: int
    mime_type: str | None = None


@dataclass(frozen=True)
class TemporaryAccessLink:
    url: str
    expires_at: datetime
