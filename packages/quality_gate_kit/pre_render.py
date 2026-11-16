"""Pre-render promise checks: does the plan promise something the footage cannot keep?

A run's plan can describe motion the footage will never deliver — "a dynamic,
camera-driven piece" assembled almost entirely from still images renders as an
animated slideshow, and by the time a human watches the MP4 the only fix is a
re-plan. This module compares what the plan *claims* against what the planning
artifacts *contain*, before a single frame is rendered.

Six signals, all derived from artifacts the run already produced (no new model
calls, no new binaries):

1. ``static_shot_share``      — share of shots built from still images vs the
                                declared motion expectation.
2. ``avg_shot_duration``      — long held shots + a motion-heavy claim conflict.
3. ``missing_intent_share``   — shots that carry no camera / transition note at
                                all: the plan is not even claiming motion.
4. ``text_screen_share``      — how much of the timeline is subtitle / title
                                carrying rather than picture carrying.
5. ``duplicate_composition_share`` — adjacent shots that reuse the same visual,
                                the montage tell of a slideshow.
6. ``shot_density``           — shots per unit of target duration, vs the pace
                                the claim implies.

Severity rules:

* ``critical``  — the claim is contradicted by the footage; composition is
  blocked until a human accepts the risk explicitly.
* ``warning``   — worth a look, never blocks on its own.

Every check is *lenient by absence*: a signal that cannot be computed from the
artifacts present (no shots, no asset types, no declared expectation) is simply
not emitted. A missing signal must never fabricate a conflict — the gate only
fires on evidence it can point at. That is also why the declared expectation is
read from the execution plan rather than inferred from the shots: "what the plan
promises" is an input, and inferring it from the thing being judged would make
the check vacuous.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

#: Claim values that promise motion-heavy output. Anything else — including no
#: claim at all — disables the claim-dependent signals (1, 2, 6) rather than
#: guessing at what the planner meant.
_MOTION_HEAVY_CLAIMS = frozenset({"motion_heavy", "dynamic", "movement_heavy"})

#: Fields a shot may use to declare what kind of source material it is built
#: from. Checked in order; the first present, non-empty value wins.
_STATIC_TYPE_VALUES = frozenset({"image", "still", "still_image", "photo", "picture"})
_DYNAMIC_TYPE_VALUES = frozenset({"video", "clip", "footage", "avatar_video_clip", "rendered_video"})
_SHOT_TYPE_FIELDS = ("asset_type", "source_asset_type", "media_type", "asset_kind", "material_type")
_SHOT_ASSET_REF_FIELDS = ("source_asset_id", "asset_id", "media_asset_id")
_SHOT_CAMERA_FIELDS = ("camera", "camera_motion", "camera_move", "movement", "motion_note")
_SHOT_TRANSITION_FIELDS = ("transition", "transition_in", "cut", "cut_type")
_SHOT_VISUAL_FIELDS = ("visual", "visual_description", "description", "composition")

#: Signal 1: with a motion-heavy claim, this share of still-image shots is
#: already a slideshow.
CRITICAL_STATIC_SHARE = 0.6
WARNING_STATIC_SHARE = 0.35

#: Signal 2: seconds. Held shots this long with a motion-heavy claim read as
#: slow pans over stills.
CRITICAL_AVG_SHOT_SECONDS = 8.0
WARNING_AVG_SHOT_SECONDS = 6.0

#: Signal 3: share of shots with no camera/transition note at all.
WARNING_MISSING_INTENT_SHARE = 0.8

#: Signal 4: share of timeline duration spent carrying text rather than picture.
WARNING_TEXT_SCREEN_SHARE = 0.9

#: Signal 5: share of adjacent shot pairs that reuse the same visual.
WARNING_DUPLICATE_SHARE = 0.6

#: Signal 6: shots per 10 seconds of target duration.
CRITICAL_SHOTS_PER_10S = 0.3
WARNING_SHOTS_PER_10S = 0.5


class PreRenderEvaluationRequest(BaseModel):
    """The artifacts the check reads, exactly as stored (loose dicts on purpose).

    The registry validates these payloads on write; this module re-reads them as
    dicts so a producer adding fields does not have to coordinate with the gate.
    """

    shot_list: dict[str, Any] = Field(default_factory=dict)
    execution_plan: dict[str, Any] = Field(default_factory=dict)
    edit_plan: dict[str, Any] = Field(default_factory=dict)
    media_analysis: dict[str, Any] = Field(default_factory=dict)


class PreRenderSignal(BaseModel):
    """One computed signal. ``observed`` / ``threshold`` carry the numbers so a
    reviewer can audit the verdict without re-deriving it."""

    signal_key: str
    severity: str  # critical | warning
    observed: dict[str, Any] = Field(default_factory=dict)
    threshold: dict[str, Any] = Field(default_factory=dict)
    message: str
    evidence: list[str] = Field(default_factory=list)


class PreRenderEvaluation(BaseModel):
    motion_expectation: str = "unspecified"
    claim_declared: bool = False
    shots_total: int = 0
    signals: list[PreRenderSignal] = Field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0

    @property
    def blocking(self) -> bool:
        return self.critical_count > 0


def evaluate_pre_render_promises(request: PreRenderEvaluationRequest) -> PreRenderEvaluation:
    """Run all six signals and return the ones that actually fired."""

    shots = _shots(request.shot_list)
    claim_raw = request.execution_plan.get("motion_expectation")
    claim = str(claim_raw).strip().lower() if isinstance(claim_raw, str) and claim_raw.strip() else "unspecified"
    claim_declared = claim in _MOTION_HEAVY_CLAIMS or claim in {"static", "static_fine", "static_ok"}

    evaluation = PreRenderEvaluation(
        motion_expectation=claim,
        claim_declared=claim_declared,
        shots_total=len(shots),
    )
    signals: list[PreRenderSignal] = []

    if shots:
        signals.extend(_static_share_signal(shots, request, claim))
        signals.extend(_avg_duration_signal(shots, claim))
        signals.extend(_missing_intent_signal(shots))
        signals.extend(_duplicate_signal(shots))
        signals.extend(_density_signal(shots, request, claim))
    signals.extend(_text_share_signal(request.edit_plan))

    evaluation.signals = signals
    evaluation.critical_count = sum(1 for s in signals if s.severity == "critical")
    evaluation.warning_count = sum(1 for s in signals if s.severity == "warning")
    return evaluation


# --- signal 1 ------------------------------------------------------------


def _static_share_signal(
    shots: list[dict[str, Any]],
    request: PreRenderEvaluationRequest,
    claim: str,
) -> list[PreRenderSignal]:
    """Share of shots whose source material is a still image.

    Shot-level type fields win; otherwise the shot list's ``asset_requirements``
    decide (a shot list that requires only images *is* a stills plan). Shots
    with no knowable type are counted separately and never silently folded into
    either side.
    """

    known_static = 0
    known_dynamic = 0
    unknown: list[str] = []
    static_ids: list[str] = []
    for shot in shots:
        kind = _shot_material_kind(shot, request.shot_list)
        if kind == "static":
            known_static += 1
            static_ids.append(_shot_label(shot))
        elif kind == "dynamic":
            known_dynamic += 1
        else:
            unknown.append(_shot_label(shot))
    known = known_static + known_dynamic
    if known == 0 or known_static == 0:
        return []
    share = known_static / known
    observed = {
        "static_shots": known_static,
        "footage_shots": known_dynamic,
        "known_type_shots": known,
        "unknown_type_shots": len(unknown),
        "static_share": round(share, 3),
    }
    evidence = static_ids[:10]
    if claim not in _MOTION_HEAVY_CLAIMS:
        # No claim to contradict: report the composition as a warning so the
        # operator sees what the plan is actually made of, without pretending
        # the plan broke a promise it never made.
        return [
            PreRenderSignal(
                signal_key="static_shot_share",
                severity="warning",
                observed=observed,
                threshold={"motion_expectation": claim},
                message=(
                    f"{known_static}/{known} shots with a known type are still images "
                    f"(share {share:.2f}); the plan does not declare a motion expectation, "
                    "so this is informational rather than a broken promise."
                ),
                evidence=evidence,
            )
        ]
    if share >= CRITICAL_STATIC_SHARE:
        return [
            PreRenderSignal(
                signal_key="static_shot_share",
                severity="critical",
                observed=observed,
                threshold={"critical_static_share": CRITICAL_STATIC_SHARE, "motion_expectation": claim},
                message=(
                    f"The plan claims {claim} output but {known_static}/{known} typed shots "
                    f"({share:.0%}) are still images; rendered as-is this is a slideshow, "
                    "not a motion piece."
                ),
                evidence=evidence,
            )
        ]
    if share >= WARNING_STATIC_SHARE:
        return [
            PreRenderSignal(
                signal_key="static_shot_share",
                severity="warning",
                observed=observed,
                threshold={"warning_static_share": WARNING_STATIC_SHARE, "motion_expectation": claim},
                message=(
                    f"{known_static}/{known} typed shots ({share:.0%}) are still images under a "
                    f"{claim} claim — enough stills to dilute the promised motion."
                ),
                evidence=evidence,
            )
        ]
    return []


def _shot_material_kind(shot: dict[str, Any], shot_list: dict[str, Any]) -> str | None:
    """``"static"`` / ``"dynamic"`` / ``None`` when unknowable."""

    for field in _SHOT_TYPE_FIELDS:
        value = shot.get(field)
        if isinstance(value, str) and value.strip():
            token = value.strip().lower()
            if token in _STATIC_TYPE_VALUES:
                return "static"
            if token in _DYNAMIC_TYPE_VALUES:
                return "dynamic"
            return None  # a type is declared but not one we can classify
    # Fall back to the shot list's declared asset requirements: a requirement
    # set that only asks for images means every shot it feeds is a still.
    requirements = shot_list.get("asset_requirements")
    if isinstance(requirements, list) and requirements:
        kinds: list[str] = []
        for item in requirements:
            if not isinstance(item, dict):
                continue
            for field in _SHOT_TYPE_FIELDS + ("type", "kind"):
                value = item.get(field)
                if isinstance(value, str) and value.strip():
                    kinds.append(value.strip().lower())
                    break
        if kinds and all(kind in _STATIC_TYPE_VALUES for kind in kinds):
            return "static"
        if kinds and all(kind in _DYNAMIC_TYPE_VALUES for kind in kinds):
            return "dynamic"
        return None
    return None


# --- signal 2 ------------------------------------------------------------


def _avg_duration_signal(shots: list[dict[str, Any]], claim: str) -> list[PreRenderSignal]:
    durations = [
        float(shot["duration_seconds"])
        for shot in shots
        if isinstance(shot.get("duration_seconds"), (int, float)) and shot["duration_seconds"] > 0
    ]
    if not durations or claim not in _MOTION_HEAVY_CLAIMS:
        return []
    avg = sum(durations) / len(durations)
    observed = {"avg_shot_seconds": round(avg, 2), "shots_with_duration": len(durations)}
    if avg >= CRITICAL_AVG_SHOT_SECONDS:
        return [
            PreRenderSignal(
                signal_key="avg_shot_duration",
                severity="critical",
                observed=observed,
                threshold={"critical_avg_shot_seconds": CRITICAL_AVG_SHOT_SECONDS, "motion_expectation": claim},
                message=(
                    f"Average shot length is {avg:.1f}s under a {claim} claim; shots held this "
                    "long cannot carry the promised motion without camera work the plan does not describe."
                ),
            )
        ]
    if avg >= WARNING_AVG_SHOT_SECONDS:
        return [
            PreRenderSignal(
                signal_key="avg_shot_duration",
                severity="warning",
                observed=observed,
                threshold={"warning_avg_shot_seconds": WARNING_AVG_SHOT_SECONDS, "motion_expectation": claim},
                message=f"Average shot length is {avg:.1f}s, on the slow side for a {claim} claim.",
            )
        ]
    return []


# --- signal 3 ------------------------------------------------------------


def _missing_intent_signal(shots: list[dict[str, Any]]) -> list[PreRenderSignal]:
    """Shots with neither a camera note nor a transition note.

    This one never depends on the claim: a shot list that says nothing about how
    anything moves is a promise vacuum, whatever the plan header says.
    """

    missing: list[str] = []
    for shot in shots:
        has_intent = any(
            isinstance(shot.get(field), str) and shot.get(field).strip()
            for field in _SHOT_CAMERA_FIELDS + _SHOT_TRANSITION_FIELDS
        )
        if not has_intent:
            missing.append(_shot_label(shot))
    share = len(missing) / len(shots)
    if share < WARNING_MISSING_INTENT_SHARE:
        return []
    return [
        PreRenderSignal(
            signal_key="missing_intent_share",
            severity="warning",
            observed={"shots_without_intent": len(missing), "shots_total": len(shots), "share": round(share, 3)},
            threshold={"warning_missing_intent_share": WARNING_MISSING_INTENT_SHARE},
            message=(
                f"{len(missing)}/{len(shots)} shots carry no camera or transition note; "
                "the plan never states how these shots are meant to move."
            ),
            evidence=missing[:10],
        )
    ]


# --- signal 4 ------------------------------------------------------------


def _text_share_signal(edit_plan: dict[str, Any]) -> list[PreRenderSignal]:
    """How much of the edit timeline carries text overlays rather than picture.

    Read from ``edit_plan.timeline`` entries that declare a duration and a text
    payload (subtitle / caption / title). Plans without a timeline are skipped —
    many pipelines keep subtitles in ``subtitle_plan`` instead, which is a
    rendering detail rather than a screen-time promise.
    """

    timeline = edit_plan.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return []
    total = 0.0
    text = 0.0
    text_ids: list[str] = []
    for index, item in enumerate(timeline):
        if not isinstance(item, dict):
            continue
        duration = _item_duration(item)
        if duration is None:
            continue
        total += duration
        has_text = any(
            value for key in ("subtitle", "caption", "title", "text_overlay", "on_screen_text")
            if (value := item.get(key))
        )
        if has_text:
            text += duration
            text_ids.append(str(item.get("shot_id") or item.get("clip_id") or index))
    if total <= 0 or text <= 0:
        return []
    share = text / total
    if share < WARNING_TEXT_SCREEN_SHARE:
        return []
    return [
        PreRenderSignal(
            signal_key="text_screen_share",
            severity="warning",
            observed={"text_seconds": round(text, 2), "timeline_seconds": round(total, 2), "share": round(share, 3)},
            threshold={"warning_text_screen_share": WARNING_TEXT_SCREEN_SHARE},
            message=(
                f"{share:.0%} of the edit timeline carries text overlays; the picture is "
                "supporting the subtitles rather than the other way around."
            ),
            evidence=text_ids[:10],
        )
    ]


# --- signal 5 ------------------------------------------------------------


def _duplicate_signal(shots: list[dict[str, Any]]) -> list[PreRenderSignal]:
    """Adjacent shot pairs that reuse the same visual or the same source asset.

    Composition repeats are the clearest montage-level tell that a plan will
    render as a slideshow even when every individual shot looks reasonable.
    """

    if len(shots) < 2:
        return []
    pairs_total = len(shots) - 1
    duplicates: list[str] = []
    for index in range(pairs_total):
        if _same_composition(shots[index], shots[index + 1]):
            duplicates.append(f"{_shot_label(shots[index])} -> {_shot_label(shots[index + 1])}")
    if not duplicates:
        return []
    share = len(duplicates) / pairs_total
    if share < WARNING_DUPLICATE_SHARE:
        return []
    return [
        PreRenderSignal(
            signal_key="duplicate_composition_share",
            severity="warning",
            observed={"duplicate_adjacent_pairs": len(duplicates), "adjacent_pairs": pairs_total, "share": round(share, 3)},
            threshold={"warning_duplicate_share": WARNING_DUPLICATE_SHARE},
            message=(
                f"{len(duplicates)}/{pairs_total} adjacent shot pairs reuse the same visual; "
                "the sequence will read as repeated composition."
            ),
            evidence=duplicates[:10],
        )
    ]


def _same_composition(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for field in _SHOT_ASSET_REF_FIELDS:
        left_ref = left.get(field)
        right_ref = right.get(field)
        if isinstance(left_ref, str) and left_ref and left_ref == right_ref:
            return True
    for field in _SHOT_VISUAL_FIELDS:
        left_visual = left.get(field)
        if isinstance(left_visual, str) and left_visual.strip():
            return bool(isinstance(right.get(field), str) and right.get(field).strip() == left_visual.strip())
    return False


# --- signal 6 ------------------------------------------------------------


def _density_signal(
    shots: list[dict[str, Any]],
    request: PreRenderEvaluationRequest,
    claim: str,
) -> list[PreRenderSignal]:
    """Shots per 10 seconds of *target* duration.

    Uses the plan's declared target (``target_duration_seconds``) or the edit
    plan's output duration; without a target this signal is skipped because a
    shot count against an unknown runtime means nothing.
    """

    if claim not in _MOTION_HEAVY_CLAIMS:
        return []
    target = _target_duration(request)
    if target is None or target <= 0:
        return []
    per_10s = len(shots) / target * 10.0
    observed = {"shots_total": len(shots), "target_duration_seconds": target, "shots_per_10s": round(per_10s, 3)}
    if per_10s < CRITICAL_SHOTS_PER_10S:
        return [
            PreRenderSignal(
                signal_key="shot_density",
                severity="critical",
                observed=observed,
                threshold={"critical_shots_per_10s": CRITICAL_SHOTS_PER_10S, "motion_expectation": claim},
                message=(
                    f"{len(shots)} shots across {target:.0f}s of target runtime ({per_10s:.2f} shots/10s) "
                    f"cannot sustain a {claim} pace."
                ),
            )
        ]
    if per_10s < WARNING_SHOTS_PER_10S:
        return [
            PreRenderSignal(
                signal_key="shot_density",
                severity="warning",
                observed=observed,
                threshold={"warning_shots_per_10s": WARNING_SHOTS_PER_10S, "motion_expectation": claim},
                message=(
                    f"{per_10s:.2f} shots per 10s of target runtime is sparse for a {claim} claim."
                ),
            )
        ]
    return []


def _target_duration(request: PreRenderEvaluationRequest) -> float | None:
    for key in ("target_duration_seconds", "planned_duration_seconds"):
        value = request.execution_plan.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    output = request.edit_plan.get("output")
    if isinstance(output, dict):
        for key in ("duration_seconds", "target_duration_seconds", "total_duration_seconds"):
            value = output.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    return None


# --- helpers -------------------------------------------------------------


def _shots(shot_list: dict[str, Any]) -> list[dict[str, Any]]:
    shots = shot_list.get("shots")
    if not isinstance(shots, list):
        return []
    return [shot for shot in shots if isinstance(shot, dict)]


def _shot_label(shot: dict[str, Any]) -> str:
    return str(shot.get("shot_id") or shot.get("id") or "unnamed-shot")


def _item_duration(item: dict[str, Any]) -> float | None:
    for key in ("duration_seconds", "duration", "length_seconds", "length"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None
