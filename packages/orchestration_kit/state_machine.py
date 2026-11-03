from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateTransition:
    action: str
    from_statuses: frozenset[str]
    to_status: str | None
    description: str


RUN_STATE_TRANSITIONS: dict[str, StateTransition] = {
    "pause": StateTransition(
        action="pause",
        from_statuses=frozenset({"draft", "queued", "running", "waiting_review", "failed"}),
        to_status="paused",
        description="Temporarily stop a non-terminal run without changing its current stage.",
    ),
    "resume": StateTransition(
        action="resume",
        from_statuses=frozenset({"paused"}),
        to_status=None,
        description="Resume a paused run at its current stage or review gate.",
    ),
    "cancel": StateTransition(
        action="cancel",
        from_statuses=frozenset({"draft", "queued", "running", "waiting_review", "paused", "failed"}),
        to_status="cancelled",
        description="Cancel the run and any unfinished background work.",
    ),
    "rerun_stage": StateTransition(
        action="rerun_stage",
        from_statuses=frozenset({"draft", "running", "waiting_review", "paused", "failed", "completed"}),
        to_status="running",
        description="Reset one stage and later stages, then continue from that stage.",
    ),
    "rerun_artifact": StateTransition(
        action="rerun_artifact",
        from_statuses=frozenset({"draft", "running", "waiting_review", "paused", "failed", "completed"}),
        to_status="running",
        description="Find the producing stage for an artifact and rerun that stage.",
    ),
    "skip_optional_stage": StateTransition(
        action="skip_optional_stage",
        from_statuses=frozenset({"running", "waiting_review", "paused"}),
        to_status="running",
        description="Skip the current stage only when the pipeline marks it skippable.",
    ),
}


TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled"})
RESETTABLE_STAGE_STATUSES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "waiting_review",
        "approved",
        "rejected",
        "completed",
        "failed",
        "skipped",
        "cancelled",
    }
)
UNFINISHED_JOB_STATUSES = frozenset({"queued", "running", "waiting_provider", "failed"})


def assert_run_action_allowed(action: str, status: str) -> StateTransition:
    transition = RUN_STATE_TRANSITIONS.get(action)
    if transition is None:
        raise ValueError(f"unsupported run state action: {action}")
    if status not in transition.from_statuses:
        allowed = ", ".join(sorted(transition.from_statuses))
        raise ValueError(f"cannot {action} run from status {status}; allowed from: {allowed}")
    return transition


def run_state_transition_table() -> list[dict[str, object]]:
    return [
        {
            "action": transition.action,
            "from_statuses": sorted(transition.from_statuses),
            "to_status": transition.to_status,
            "description": transition.description,
        }
        for transition in sorted(RUN_STATE_TRANSITIONS.values(), key=lambda item: item.action)
    ]
