"""Unit matrix for the six pre-render promise signals (P3).

The gate's verdict is only as trustworthy as its arithmetic, so every signal is
exercised at both sides of its threshold, plus the most important property of
all: an artifact set that cannot support a conclusion produces *no* signal.
Absence of evidence is not a conflict, and the gate must never fire on data it
does not have.
"""

from packages.quality_gate_kit.pre_render import (
    PreRenderEvaluationRequest,
    evaluate_pre_render_promises,
)


def _evaluation(**kwargs):
    return evaluate_pre_render_promises(PreRenderEvaluationRequest(**kwargs))


def _stills_plan(share: float, total: int = 10):
    """A shot list whose typed shots are ``share`` still images."""

    stills = round(share * total)
    shots = [
        {"shot_id": f"still-{index}", "duration_seconds": 3, "asset_type": "image"}
        for index in range(stills)
    ]
    shots += [
        {"shot_id": f"footage-{index}", "duration_seconds": 3, "asset_type": "video"}
        for index in range(total - stills)
    ]
    return {"shots": shots}


def test_signal_keys_are_stable_vocabulary():
    """Clients and the audit log switch on these strings; renaming one is a
    breaking change even though it is not an error code."""

    evaluation = _evaluation(
        shot_list={"shots": [{"shot_id": "s", "duration_seconds": 12, "asset_type": "image"}]},
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    keys = {signal.signal_key for signal in evaluation.signals}
    assert keys <= {
        "static_shot_share",
        "avg_shot_duration",
        "missing_intent_share",
        "text_screen_share",
        "duplicate_composition_share",
        "shot_density",
    }
    assert "static_shot_share" in keys


def test_all_stills_with_motion_claim_is_critical():
    evaluation = _evaluation(
        shot_list=_stills_plan(1.0),
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    assert evaluation.critical_count >= 1
    assert evaluation.blocking
    static = next(s for s in evaluation.signals if s.signal_key == "static_shot_share")
    assert static.severity == "critical"
    assert static.observed["static_share"] == 1.0


def test_ninety_percent_stills_is_the_acceptance_scenario():
    """The acceptance case from the capability plan: 90% stills + motion claim."""

    evaluation = _evaluation(
        shot_list=_stills_plan(0.9),
        execution_plan={"motion_expectation": "motion_heavy", "target_duration_seconds": 40},
    )
    assert evaluation.blocking
    static = next(s for s in evaluation.signals if s.signal_key == "static_shot_share")
    assert static.observed["static_shots"] == 9
    assert static.observed["footage_shots"] == 1


def test_moderate_stills_share_is_a_warning_not_a_block():
    evaluation = _evaluation(
        shot_list=_stills_plan(0.4),
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    assert not evaluation.blocking
    static = next(s for s in evaluation.signals if s.signal_key == "static_shot_share")
    assert static.severity == "warning"


def test_low_stills_share_is_silent():
    evaluation = _evaluation(
        shot_list=_stills_plan(0.1),
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    assert all(s.signal_key != "static_shot_share" for s in evaluation.signals)
    assert not evaluation.blocking


def test_stills_without_a_claim_are_informational_only():
    """No declared expectation means no promise to break — the composition is
    reported but never blocked on a claim the plan never made."""

    evaluation = _evaluation(shot_list=_stills_plan(1.0))
    assert not evaluation.blocking
    static = next(s for s in evaluation.signals if s.signal_key == "static_shot_share")
    assert static.severity == "warning"
    assert evaluation.motion_expectation == "unspecified"
    assert evaluation.claim_declared is False


def test_static_claim_never_triggers_the_conflict_signals():
    evaluation = _evaluation(
        shot_list=_stills_plan(1.0),
        execution_plan={"motion_expectation": "static_fine"},
    )
    assert not evaluation.blocking
    assert evaluation.claim_declared is True


def test_long_held_shots_under_a_motion_claim_are_critical():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 9, "asset_type": "video"},
                {"shot_id": "b", "duration_seconds": 9, "asset_type": "video"},
            ]
        },
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    duration = next(s for s in evaluation.signals if s.signal_key == "avg_shot_duration")
    assert duration.severity == "critical"
    assert duration.observed["avg_shot_seconds"] == 9.0


def test_long_shots_without_a_claim_are_silent():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 9, "asset_type": "video"},
                {"shot_id": "b", "duration_seconds": 9, "asset_type": "video"},
            ]
        }
    )
    assert all(s.signal_key != "avg_shot_duration" for s in evaluation.signals)


def test_shots_without_any_camera_or_transition_note_are_flagged():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "b", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "c", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "d", "duration_seconds": 2, "asset_type": "video"},
                {"shot_id": "e", "duration_seconds": 2, "asset_type": "video", "camera": "slow push-in"},
            ]
        }
    )
    missing = next(s for s in evaluation.signals if s.signal_key == "missing_intent_share")
    assert missing.severity == "warning"
    assert missing.observed["shots_without_intent"] == 4
    assert missing.evidence == ["a", "b", "c", "d"]


def test_camera_notes_satisfy_the_intent_signal():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "asset_type": "video", "camera": "pan left"},
                {"shot_id": "b", "duration_seconds": 2, "asset_type": "video", "transition": "cut"},
            ]
        }
    )
    assert all(s.signal_key != "missing_intent_share" for s in evaluation.signals)


def test_text_heavy_timeline_is_a_warning():
    evaluation = _evaluation(
        edit_plan={
            "timeline": [
                {"shot_id": "a", "duration_seconds": 9, "subtitle": "第一句"},
                {"shot_id": "b", "duration_seconds": 1, "subtitle": None},
            ]
        }
    )
    text = next(s for s in evaluation.signals if s.signal_key == "text_screen_share")
    assert text.severity == "warning"
    assert text.observed["share"] == 0.9


def test_picture_led_timeline_is_silent():
    evaluation = _evaluation(
        edit_plan={
            "timeline": [
                {"shot_id": "a", "duration_seconds": 8},
                {"shot_id": "b", "duration_seconds": 2, "title": "片尾"},
            ]
        }
    )
    assert all(s.signal_key != "text_screen_share" for s in evaluation.signals)


def test_adjacent_shots_reusing_the_same_visual_are_flagged():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "asset_type": "video", "visual": "一杯奶茶特写"},
                {"shot_id": "b", "duration_seconds": 2, "asset_type": "video", "visual": "一杯奶茶特写"},
                {"shot_id": "c", "duration_seconds": 2, "asset_type": "video", "visual": "一杯奶茶特写"},
                {"shot_id": "d", "duration_seconds": 2, "asset_type": "video", "visual": "门店外景"},
            ]
        }
    )
    duplicates = next(s for s in evaluation.signals if s.signal_key == "duplicate_composition_share")
    assert duplicates.observed["duplicate_adjacent_pairs"] == 2
    assert duplicates.observed["adjacent_pairs"] == 3


def test_adjacent_shots_reusing_the_same_source_asset_are_flagged():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "source_asset_id": "asset-1"},
                {"shot_id": "b", "duration_seconds": 2, "source_asset_id": "asset-1"},
            ]
        }
    )
    duplicates = next(s for s in evaluation.signals if s.signal_key == "duplicate_composition_share")
    assert duplicates.observed["share"] == 1.0


def test_distinct_visuals_are_silent():
    evaluation = _evaluation(
        shot_list={
            "shots": [
                {"shot_id": "a", "duration_seconds": 2, "visual": "特写"},
                {"shot_id": "b", "duration_seconds": 2, "visual": "中景"},
            ]
        }
    )
    assert all(s.signal_key != "duplicate_composition_share" for s in evaluation.signals)


def test_sparse_shots_against_a_declared_target_are_critical():
    evaluation = _evaluation(
        shot_list={"shots": [{"shot_id": "a", "duration_seconds": 10, "asset_type": "video"}]},
        execution_plan={"motion_expectation": "motion_heavy", "target_duration_seconds": 60},
    )
    density = next(s for s in evaluation.signals if s.signal_key == "shot_density")
    assert density.severity == "critical"
    assert density.observed["shots_per_10s"] < 0.3


def test_density_without_a_target_duration_is_silent():
    evaluation = _evaluation(
        shot_list={"shots": [{"shot_id": "a", "duration_seconds": 10, "asset_type": "video"}]},
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    assert all(s.signal_key != "shot_density" for s in evaluation.signals)


def test_an_empty_artifact_set_produces_no_signal():
    """The lenience contract: nothing to judge, nothing to say."""

    evaluation = _evaluation()
    assert evaluation.signals == []
    assert not evaluation.blocking
    assert evaluation.critical_count == 0
    assert evaluation.warning_count == 0


def test_signal_counts_match_the_signal_list():
    evaluation = _evaluation(
        shot_list=_stills_plan(1.0),
        execution_plan={"motion_expectation": "motion_heavy", "target_duration_seconds": 40},
        edit_plan={"timeline": [{"shot_id": "a", "duration_seconds": 10, "subtitle": "字幕"}]},
    )
    assert evaluation.critical_count == sum(1 for s in evaluation.signals if s.severity == "critical")
    assert evaluation.warning_count == sum(1 for s in evaluation.signals if s.severity == "warning")


def test_evidence_points_at_real_shot_ids():
    evaluation = _evaluation(
        shot_list=_stills_plan(1.0),
        execution_plan={"motion_expectation": "motion_heavy"},
    )
    static = next(s for s in evaluation.signals if s.signal_key == "static_shot_share")
    assert static.evidence[:2] == ["still-0", "still-1"]
