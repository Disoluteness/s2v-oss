"""Upload admission control (V8.4).

An upload is the one place a caller hands the platform arbitrary bytes and the
platform then hands them back out again — the asset content endpoint serves them
with whatever ``mime_type`` was declared. Before this module the only thing
between the two was ``base64.b64decode``: no size bound (a body is decoded into
memory before anyone can look at it) and no type check (a "mp4" whose first bytes
are an ELF header was stored and served as a video).

The checks live here, as pure functions over bytes and strings, because that is
what makes "declared mp4, actually an ELF" a one-line test rather than an
end-to-end upload.

What this is *not* is an antivirus engine. A signature scanner is a separate
process with its own database and its own update channel, and the platform's
threat model here is a mislabelled or disguised file, not a zero-day payload. The
magic-number check below catches the former; it cannot catch the latter, and
pretending otherwise would be worse than saying so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.api.app.errors import ErrorCode

#: Lowercased, parameter-stripped MIME types, plus the extension-level fallbacks
#: ``mimetypes`` cannot always resolve on a bare server image.
_DENYLIST_ALIASES: dict[str, str] = {
    "application/x-msdos-program": "application/x-msdownload",
}

#: Leading byte sequences that mean "this is not the media it claims to be".
#: Executables and scripts only — a PDF that starts with ``%PDF`` or a ZIP whose
#: ``PK`` is also a valid OOXML container are neither dangerous nor disguisable
#: into a video pipeline, so refusing them would only generate support tickets.
_EXECUTABLE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x7fELF", "ELF executable"),
    (b"MZ", "Windows executable"),
    (b"\xfe\xed\xfa\xce", "Mach-O executable (32-bit)"),
    (b"\xfe\xed\xfa\xcf", "Mach-O executable (64-bit)"),
    (b"\xce\xfa\xed\xfe", "Mach-O executable (32-bit, reversed)"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable (64-bit, reversed)"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary"),
    (b"#!", "script with a shebang"),
)

#: Base64 expands 3 bytes to 4 characters; this is the exact encoded length a
#: payload of ``max_bytes`` occupies once padded. Checked *before* decoding so an
#: oversized body is refused without being materialised as bytes first.
_BYTES_PER_BLOCK = 3
_CHARS_PER_BLOCK = 4


@dataclass(frozen=True)
class UploadVerdict:
    """The outcome of admission control. ``ok`` is the whole contract."""

    ok: bool
    code: ErrorCode | None = None
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


def encoded_length_bound(max_bytes: int) -> int:
    """Largest base64 string that can decode to at most ``max_bytes`` bytes."""
    blocks = -(-max(max_bytes, 0) // _BYTES_PER_BLOCK)  # ceiling division
    return blocks * _CHARS_PER_BLOCK


def check_encoded_length(content_base64: str, *, max_bytes: int) -> UploadVerdict:
    bound = encoded_length_bound(max_bytes)
    if len(content_base64) > bound:
        return UploadVerdict(
            ok=False,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message=f"encoded content exceeds {max_bytes} bytes once decoded",
            detail={"max_bytes": max_bytes, "encoded_length": len(content_base64)},
        )
    return UploadVerdict(ok=True)


def check_decoded_size(content: bytes, *, max_bytes: int) -> UploadVerdict:
    if len(content) > max_bytes:
        return UploadVerdict(
            ok=False,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message=f"content exceeds the {max_bytes} byte upload limit",
            detail={"max_bytes": max_bytes, "file_size": len(content)},
        )
    return UploadVerdict(ok=True)


def normalize_mime_type(value: str | None) -> str:
    """Lowercase and drop parameters, so ``text/plain; charset=utf-8`` is ``text/plain``."""
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


def parse_mime_set(value: str) -> frozenset[str]:
    """Turn a comma-separated config string into a set of normalised types."""
    return frozenset(
        normalized
        for part in (value or "").split(",")
        if (normalized := normalize_mime_type(part))
    )


def detect_executable(content: bytes) -> str | None:
    """Name the executable format ``content`` starts with, if any."""
    for magic, label in _EXECUTABLE_MAGIC:
        if content.startswith(magic):
            return label
    return None


def check_content_type(
    *,
    declared_mime: str | None,
    filename: str,
    content: bytes,
    allowed_types: frozenset[str],
    denied_types: frozenset[str],
) -> UploadVerdict:
    """Type admission: declared value, then content.

    The order matters for the message, not the outcome. A caller who lied about
    the type gets told so; a caller who merely used a type this deployment does
    not accept gets told that instead.
    """
    effective = normalize_mime_type(declared_mime)
    if effective in denied_types:
        return UploadVerdict(
            ok=False,
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message=f"content type '{effective}' is not accepted for uploads",
            detail={"mime_type": effective, "filename": filename},
        )
    if allowed_types and effective and effective not in allowed_types:
        return UploadVerdict(
            ok=False,
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message=f"content type '{effective}' is not in the allowed upload types",
            detail={"mime_type": effective, "filename": filename},
        )
    detected = detect_executable(content)
    if detected is not None:
        return UploadVerdict(
            ok=False,
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            message="content looks like an executable or script, not media",
            detail={
                "detected": detected,
                "declared_mime_type": effective or None,
                "filename": filename,
            },
        )
    return UploadVerdict(ok=True)
