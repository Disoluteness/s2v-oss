"""Mapping from "a choice was made" to one row of the decision log.

Pure functions only — nothing in here touches a session. The point of the split
is that the *shape* of an auditable decision is decided in one place, while the
transaction it lands in stays the caller's business. A new write point (a new
gate, a new selector) adds a mapping function here and one call in the store,
and the read endpoint keeps working without knowing what it is looking at.

Deliberately **not** recorded here:

* the tool contract that was loaded (that is ``preflight_report``'s job), and
* the artifact payload itself (that is ``execution_plan``'s job).

A decision row carries the reasoning, and points at its source artifact through
``source_artifact_id`` so the two can be read together.
"""

from __future__ import annotations

from typing import Any

CATEGORY_TOOL_SELECTION = "tool_selection"
CATEGORY_REVIEW_DECISION = "review_decision"
CATEGORY_QUALITY_DOWNGRADE = "quality_downgrade"
CATEGORY_PAID_PROVIDER_CONSENT = "paid_provider_consent"

ACTOR_SYSTEM = "system"
ACTOR_HUMAN = "human"

_CONFIDENCE_BASIS_MARGIN = "score_margin"
_CONFIDENCE_BASIS_UNCONTESTED = "uncontested"


def tool_selection_entries(
    plan_decisions: list[dict[str, Any]],
    *,
    artifact_id: str | None,
) -> list[dict[str, Any]]:
    """One entry per capability the plan decided about.

    ``blocked`` and ``skip_optional`` decisions are recorded too, on purpose: the
    interesting audit question is usually "why did this run not have background
    music", and an answer that only lists successful picks cannot answer it.
    """

    entries: list[dict[str, Any]] = []
    for decision in plan_decisions:
        selected = decision.get("selected_tool") or {}
        candidates = [_candidate_summary(option) for option in decision.get("options_considered", [])]
        selected_score = selected.get("selector_score")
        confidence, basis = _score_margin(
            chosen_score=selected_score,
            candidate_scores=[
                candidate["score"]
                for candidate in candidates
                if candidate["tool_key"] != selected.get("tool_key")
            ],
        )
        entries.append(
            {
                "category": CATEGORY_TOOL_SELECTION,
                "subject": decision.get("subject") or decision.get("capability") or "unknown",
                "stage_key": decision.get("stage_key"),
                "decision": decision.get("decision") or "unknown",
                "chosen": {
                    "tool_key": decision.get("selected_tool_key"),
                    "provider": decision.get("selected_provider"),
                    "runtime": selected.get("runtime"),
                    "score": selected_score,
                    "capability": decision.get("capability"),
                    "required": decision.get("required"),
                    "intent_satisfied": decision.get("intent_satisfied"),
                    "intent_mismatch_kind": decision.get("intent_mismatch_kind"),
                },
                "candidates": candidates,
                "confidence": confidence,
                "confidence_basis": basis,
                "rationale": decision.get("rationale") or decision.get("selection_reason"),
                "actor_type": ACTOR_SYSTEM,
                "source_artifact_id": artifact_id,
            }
        )
    return entries


def review_decision_entry(
    *,
    stage_key: str,
    decision: str,
    review_request_id: str,
    review_decision_id: str | None,
    round_number: int,
    comment: str | None,
    actor_id: str | None,
    actor_display_name: str | None,
) -> dict[str, Any]:
    """A human gate decision. ``subject`` is the stage: one current verdict each."""

    return {
        "category": CATEGORY_REVIEW_DECISION,
        "subject": stage_key,
        "stage_key": stage_key,
        "decision": decision,
        "chosen": {
            "review_request_id": review_request_id,
            "review_decision_id": review_decision_id,
            "decision": decision,
            "round": round_number,
        },
        # A reviewer expresses a verdict, not a preference ranking. Leaving this
        # empty is the honest answer; a synthetic candidate list would imply the
        # reviewer weighed options the platform never showed them.
        "candidates": [],
        "confidence": None,
        "confidence_basis": None,
        "rationale": comment,
        "actor_type": ACTOR_HUMAN,
        "actor_id": actor_id,
        "actor_display_name": actor_display_name,
        "source_artifact_id": None,
    }


def quality_downgrade_entry(
    *,
    downgrades: list[dict[str, Any]],
    quality_mode: str | None,
    actor_id: str | None,
    actor_display_name: str | None,
    artifact_id: str | None,
) -> dict[str, Any]:
    """The operator accepted running below the quality the run declared."""

    return {
        "category": CATEGORY_QUALITY_DOWNGRADE,
        "subject": "quality_mode",
        "stage_key": "preflight",
        "decision": "accept_intent_downgrade",
        "chosen": {
            "quality_mode": quality_mode,
            "accepted": True,
            "downgrade_count": len(downgrades),
        },
        # Each downgrade is a thing the operator wanted and did not get, which is
        # exactly what "candidate" means here: considered, not obtained.
        "candidates": [
            {
                "stage_key": item.get("stage_key"),
                "capability": item.get("capability"),
                "required": item.get("required"),
                "kind": item.get("kind"),
                "expected_runtime": item.get("expected_runtime"),
                "selected_tool_key": item.get("selected_tool_key"),
                "selected_runtime": item.get("selected_runtime"),
                "reason": item.get("reason"),
            }
            for item in downgrades
        ],
        "confidence": None,
        "confidence_basis": None,
        "rationale": (
            f"Accepted {len(downgrades)} capability downgrade(s) below the declared "
            f"quality mode {quality_mode}."
        ),
        "actor_type": ACTOR_HUMAN,
        "actor_id": actor_id,
        "actor_display_name": actor_display_name,
        "source_artifact_id": artifact_id,
    }


def paid_provider_consent_entry(
    *,
    provider: str,
    accepted: bool,
    note: str | None,
    actor_id: str | None,
    actor_display_name: str | None,
) -> dict[str, Any]:
    """First paid call to a provider was confirmed (or the confirmation retracted)."""

    return {
        "category": CATEGORY_PAID_PROVIDER_CONSENT,
        "subject": f"provider:{provider}",
        "stage_key": "preflight",
        "decision": "accept_paid_provider" if accepted else "retract_paid_provider",
        "chosen": {"provider": provider, "accepted": accepted},
        "candidates": [],
        "confidence": None,
        "confidence_basis": None,
        "rationale": note,
        "actor_type": ACTOR_HUMAN,
        "actor_id": actor_id,
        "actor_display_name": actor_display_name,
        "source_artifact_id": None,
    }


def _candidate_summary(option: dict[str, Any]) -> dict[str, Any]:
    """Trim a plan's tool option down to what an auditor needs.

    The full option carries contracts, dependencies and cost models; keeping all
    of it would make every row a copy of the tool catalog and hide the two
    numbers that matter — the score and the role it played.
    """

    return {
        "tool_key": option.get("tool_key"),
        "provider": option.get("provider"),
        "status": option.get("status"),
        "runtime": option.get("runtime"),
        "score": option.get("selector_score"),
        "rank": option.get("selector_rank"),
        "selection_role": option.get("selection_role"),
        "score_breakdown": option.get("score_breakdown") or {},
    }


def _score_margin(
    *,
    chosen_score: float | None,
    candidate_scores: list[float | None],
) -> tuple[float | None, str | None]:
    """How far ahead of the runner-up the winner was, relative to its own score.

    This is a *separation* measure, not a probability: ``0.0`` means the choice
    was a tie and ``1.0`` means nothing else came close. Stated as a basis
    alongside the number so nobody reads it as "90% likely to be correct".
    """

    if chosen_score is None or chosen_score <= 0:
        return None, None
    others = [score for score in candidate_scores if score is not None]
    if not others:
        return 1.0, _CONFIDENCE_BASIS_UNCONTESTED
    runner_up = max(others)
    separation = (float(chosen_score) - float(runner_up)) / float(chosen_score)
    return round(max(0.0, min(1.0, separation)), 3), _CONFIDENCE_BASIS_MARGIN
