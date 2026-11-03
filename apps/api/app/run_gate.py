"""Structured refusals for run-level risk gates.

Two gates live under this roof:

* **T75 intent gate** — the run declared a quality mode, the selector could only
  satisfy it below that declaration, and nobody has acknowledged the difference.
* **T76 budget gates** — the execution plan's cost reservation does not fit the
  budget, or a run is about to reach a paid provider for the first time.

Both stop an operation that would otherwise spend money or change the quality
the caller asked for. Each refusal carries a machine-readable ``code`` plus the
numbers behind it, so the Web layer can explain the block instead of printing a
raw string.
"""

from __future__ import annotations

from typing import Any


class RunGateError(Exception):
    """A run risk gate blocked the requested operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}
