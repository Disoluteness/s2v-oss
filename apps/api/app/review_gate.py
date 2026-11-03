"""Shared review-gate rules.

Two things live here rather than in the store:

* ``ReviewGateError`` — a structured blocking reason. The review gate is policy
  data declared in the pipeline DSL; the code only enforces it, and every
  refusal has to carry a machine-readable code so the Web layer can explain it.
* the stage capability -> action kind mapping used to turn a reviewer's asks
  into trackable action items.
"""

from __future__ import annotations

from typing import Any

from apps.api.app.models import ReviewActionKind

# Stage keys whose fix is "regenerate the upstream text artifact", so the cheap
# and correct action is to rerun the stage that produced it.
PLANNING_STAGE_KEYS = frozenset(
    {
        "plan",
        "script",
        "storyboard",
        "demo_plan",
        "recording_plan",
        "clip_selection",
        "annotation",
        "edit",
    }
)

# Stage keys that produce paid media, where an individual asset can be regenerated.
MEDIA_STAGE_KEYS = frozenset({"asset_generation", "capture"})

# Stage keys whose output is an analysis of source material.
ANALYSIS_STAGE_KEYS = frozenset({"analysis", "source_ingest"})


class ReviewGateError(Exception):
    """A review gate rule blocked the requested operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def infer_action_kind(stage_key: str | None) -> ReviewActionKind:
    """Map a stage to the cheapest action that can satisfy a review request."""
    if stage_key in MEDIA_STAGE_KEYS:
        return ReviewActionKind.REGENERATE_MEDIA
    if stage_key in ANALYSIS_STAGE_KEYS:
        return ReviewActionKind.REANALYZE_SOURCE
    if stage_key in PLANNING_STAGE_KEYS:
        return ReviewActionKind.RERUN_STAGE
    return ReviewActionKind.MANUAL


def action_item_result_action(kind: ReviewActionKind) -> str | None:
    """The existing action contract that fulfils this review action kind."""
    if kind == ReviewActionKind.RERUN_STAGE:
        return "rerun_stage"
    if kind == ReviewActionKind.RERUN_ARTIFACT:
        return "rerun_artifact"
    return None
