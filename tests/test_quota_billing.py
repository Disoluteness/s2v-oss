"""V8.3 quotas: the window, the verdict, the attribution, the gate, the API.

Four layers, cheapest first:

1. **The window** (``quota.py``) — ``period`` implies a half-open interval that
   is recomputed on every read rather than stored, so there is no rollover job
   to forget to deploy. Pure function, no session.
2. **The verdict** (``quota.py``) — limits versus observed usage, including the
   distinction the whole feature rests on: *no ceiling* is not the same answer
   as *under a ceiling*.
3. **Attribution** — spend reaching a team through
   ``usage_ledger_entries -> production_runs -> projects``, where a project with
   no team belongs to the default one.
4. **The gate** — a hard ceiling refusing the run that would cross it, an
   override letting exactly one run through, and the API surface around both.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from harness import build_client
from sqlalchemy import select

from apps.api.app import db_models  # registers the tables
from apps.api.app.actor import ActorContext
from apps.api.app.models import (
    TeamCreate,
    TeamMemberAdd,
    TeamQuotaEnforcement,
    TeamQuotaPeriod,
    TeamQuotaUpdate,
)
from apps.api.app.permissions import TeamRole
from apps.api.app.quota import (
    DIMENSION_COST,
    DIMENSION_RUNS,
    DIMENSION_TOKENS,
    STATUS_EXCEEDED,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    QuotaUsage,
    evaluate_quota,
    quota_window,
)
from apps.api.app.repositories import DEFAULT_TENANT_ID
from apps.api.app.run_gate import RunGateError

OWNER = "system"
VIEWER = "user_viewer"


def _now() -> datetime:
    """Naive UTC, matching what the store writes into every timestamp column."""
    return datetime.now(UTC).replace(tzinfo=None)


def _at(value: str) -> datetime:
    """A naive-UTC instant, spelled as a string so the intent is unambiguous.

    Every window boundary is compared against columns holding *naive* UTC (see
    ``_naive_utc_now``), so a tz-aware literal would be the wrong thing twice
    over: wrong type for the comparison, and wrong claim about the data.
    """
    return datetime.fromisoformat(value)


def _default_actor() -> ActorContext:
    return ActorContext(
        actor_id=OWNER,
        tenant_id=DEFAULT_TENANT_ID,
        display_name="Internal System",
        source="default",
    )


@pytest.fixture()
def asgi(tmp_path):
    client = build_client(tmp_path / "quota.db")
    # A default actor owns the default team only once seated (V6.5 rule: a new
    # identity does not join a team by itself). Without this, every run would be
    # unattributed and every gate below would silently pass.
    client.store.ensure_default_seat(_default_actor())
    client.store.session.commit()
    try:
        yield client
    finally:
        client.close()


def _default_team_id(asgi) -> str:
    team_id = asgi.store.teams.default_team_id(DEFAULT_TENANT_ID)
    assert team_id is not None  # the fixture seated an owner
    return team_id


def _project_and_run(asgi) -> tuple[str, str]:
    status, _, project = asgi.request(
        "POST", "/api/projects", json_body={"title": "配额项目"}, actor_id=OWNER
    )
    assert status == 200, project
    status, _, run = asgi.request(
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        json_body={"raw_script": "秋天第一杯奶茶"},
        actor_id=OWNER,
    )
    assert status == 200, run
    return project["project_id"], run["run_id"]


def _set_quota(asgi, team_id: str, **overrides) -> None:
    asgi.store.set_team_quota(
        team_id,
        TeamQuotaUpdate(**overrides),
        _default_actor(),
    )


def _spend(asgi, run_id: str, *, cost: float | None = None, actual: float | None = None,
           input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    asgi.store.usage_ledger.append_entry(
        run_id=run_id,
        source_type="tool_invocation",
        source_id=run_id,
        operation="unit_test",
        provider="dashscope",
        tool_key="unit_test_tool",
        estimated_cost=cost,
        actual_cost=actual,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    asgi.store.session.commit()


def _backdate_ledger(asgi, run_id: str, *, days: int) -> None:
    """Move a run's ledger rows into the past so the window has something to exclude."""
    rows = list(
        asgi.store.session.scalars(
            select(db_models.UsageLedgerRecord).where(
                db_models.UsageLedgerRecord.run_id == run_id
            )
        )
    )
    assert rows
    for row in rows:
        row.created_at = _now() - timedelta(days=days)
    asgi.store.session.commit()


def _events(asgi, run_id: str, prefix: str) -> list:
    return [
        event
        for event in asgi.store.list_events(run_id, limit=200)
        if event.event_type.startswith(prefix)
    ]


# --------------------------------------------------------------------------- #
# 1. the window
# --------------------------------------------------------------------------- #


def test_calendar_month_snaps_to_utc_month_boundaries():
    window = quota_window(TeamQuotaPeriod.CALENDAR_MONTH, now=_at("2026-10-12T09:30"))

    assert window.start == _at("2026-10-01T00:00")
    assert window.end == _at("2026-11-01T00:00")


def test_calendar_month_wraps_into_the_next_year():
    window = quota_window(TeamQuotaPeriod.CALENDAR_MONTH, now=_at("2026-12-31T23:59"))

    assert window.start == _at("2026-12-01T00:00")
    assert window.end == _at("2027-01-01T00:00")


def test_rolling_window_reaches_back_exactly_thirty_days():
    anchor = _at("2026-10-12T09:30")
    window = quota_window(TeamQuotaPeriod.ROLLING_30D, now=anchor)

    assert window.start == anchor - timedelta(days=30)
    # A rolling window ends at the instant it is asked, not at a boundary.
    assert window.end == anchor


def test_lifetime_window_has_no_lower_bound():
    anchor = _at("2026-10-12T09:30")
    window = quota_window(TeamQuotaPeriod.LIFETIME, now=anchor)

    assert window.start is None
    assert window.end == anchor


def test_the_period_accepts_its_raw_string_form():
    """The value travels through a DB column, so it arrives as a plain string."""
    window = quota_window("rolling_30d", now=_at("2026-10-12"))

    assert window.start == _at("2026-09-12")


# --------------------------------------------------------------------------- #
# 2. the verdict
# --------------------------------------------------------------------------- #


def _usage(**kwargs) -> QuotaUsage:
    window = quota_window(TeamQuotaPeriod.CALENDAR_MONTH, now=_at("2026-10-12"))
    return QuotaUsage(window=window, **kwargs)


def test_a_quota_without_limits_is_not_configured_rather_than_ok():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=99.0, run_count=99),
        limit_cost=None,
        limit_runs=None,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.HARD,
    )

    assert verdict.status == STATUS_NOT_CONFIGURED
    assert verdict.configured is False
    assert verdict.exceeded == ()
    assert verdict.blocking is False


def test_a_call_inside_every_limit_is_ok():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=1.0, run_count=2),
        limit_cost=10.0,
        limit_runs=5,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.HARD,
    )

    assert verdict.status == STATUS_OK
    assert verdict.configured is True
    assert verdict.projected[DIMENSION_COST] == 1.0


def test_a_hard_breach_blocks_and_names_the_dimension():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=9.0),
        limit_cost=10.0,
        limit_runs=None,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.HARD,
        additional_cost=2.0,
    )

    assert verdict.status == STATUS_EXCEEDED
    assert verdict.exceeded == (DIMENSION_COST,)
    assert verdict.blocking is True
    assert verdict.projected[DIMENSION_COST] == 11.0
    assert verdict.details()["enforcement"] == "hard"


def test_a_soft_breach_is_recorded_but_does_not_block():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=9.0),
        limit_cost=10.0,
        limit_runs=None,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.SOFT,
        additional_cost=2.0,
    )

    assert verdict.exceeded == (DIMENSION_COST,)
    assert verdict.blocking is False


def test_costs_runs_and_tokens_are_independent_dimensions():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=0.0, run_count=0),
        limit_cost=100.0,
        limit_runs=0,
        limit_tokens=1_000,
        enforcement=TeamQuotaEnforcement.HARD,
        additional_runs=1,
    )

    # Only the dimension that actually overflowed is reported: the other two
    # being untouched is what tells an operator where to raise the number.
    assert verdict.exceeded == (DIMENSION_RUNS,)


def test_tokens_count_input_and_output_together():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(input_tokens=600, output_tokens=500),
        limit_cost=None,
        limit_runs=None,
        limit_tokens=1_000,
        enforcement=TeamQuotaEnforcement.HARD,
    )

    assert verdict.exceeded == (DIMENSION_TOKENS,)
    assert verdict.current[DIMENSION_TOKENS] == 1_100


def test_the_projection_adds_the_prospective_call_to_the_current_usage():
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=4.0),
        limit_cost=10.0,
        limit_runs=None,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.HARD,
        additional_cost=6.5,
    )

    assert verdict.current[DIMENSION_COST] == 4.0
    assert verdict.projected[DIMENSION_COST] == 10.5
    assert verdict.exceeded == (DIMENSION_COST,)


def test_a_negative_addition_cannot_buy_headroom():
    """A caller passing a nonsense estimate must not enlarge the allowance."""
    verdict = evaluate_quota(
        period=TeamQuotaPeriod.CALENDAR_MONTH,
        usage=_usage(spent_cost=11.0),
        limit_cost=10.0,
        limit_runs=None,
        limit_tokens=None,
        enforcement=TeamQuotaEnforcement.HARD,
        additional_cost=-5.0,
    )

    assert verdict.projected[DIMENSION_COST] == 11.0
    assert verdict.exceeded == (DIMENSION_COST,)


# --------------------------------------------------------------------------- #
# 3. attribution
# --------------------------------------------------------------------------- #


def test_a_project_without_a_team_spends_the_default_teams_allowance(asgi):
    project_id, run_id = _project_and_run(asgi)
    _spend(asgi, run_id, cost=3.0)

    quota = asgi.store.team_quota(_default_team_id(asgi))
    assert quota is not None
    # projects.team_id is NULL for every project created the ordinary way, and
    # NULL has to mean "the default team" or nobody's spend would ever be counted.
    assert quota.usage.spent_cost == 3.0
    assert quota.usage.run_count == 1
    assert project_id  # the project exists, it is just unattributed


def test_another_teams_spend_is_not_charged_to_the_default_team(asgi):
    project_id, run_id = _project_and_run(asgi)
    _spend(asgi, run_id, cost=7.0)

    second = asgi.store.create_team(TeamCreate(name="第二团队"), _default_actor())
    status, _, moved = asgi.request(
        "PATCH",
        f"/api/projects/{project_id}/team",
        json_body={"team_id": second.team_id},
        actor_id=OWNER,
    )
    assert status == 200, moved
    assert moved["team_id"] == second.team_id

    default_quota = asgi.store.team_quota(_default_team_id(asgi))
    second_quota = asgi.store.team_quota(second.team_id)
    assert default_quota is not None and second_quota is not None
    # The same ledger rows change owner with the project: attribution is read
    # through the project, never copied onto the usage rows.
    assert default_quota.usage.spent_cost == 0.0
    assert second_quota.usage.spent_cost == 7.0


def test_spend_outside_the_window_is_excluded(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _spend(asgi, run_id, cost=3.0)
    _backdate_ledger(asgi, run_id, days=400)

    quota = asgi.store.team_quota(_default_team_id(asgi))
    assert quota is not None
    assert quota.usage.spent_cost == 0.0
    # The cap is calendar_month, so a row from over a year ago is out of scope
    # even though it is the same team and the same ledger.
    assert quota.period is TeamQuotaPeriod.CALENDAR_MONTH


def test_estimated_cost_stands_in_until_an_actual_arrives(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _spend(asgi, run_id, cost=2.0)
    _spend(asgi, run_id, cost=2.0, actual=5.0)

    quota = asgi.store.team_quota(_default_team_id(asgi))
    assert quota is not None
    # Row one has no actual, so its estimate is what it spent; row two has one,
    # and the actual wins. Summing only actuals would report 5.0 and let a team
    # in flight spend far past a ceiling that looked untouched.
    assert quota.usage.estimated_cost == 4.0
    assert quota.usage.actual_cost == 5.0
    assert quota.usage.spent_cost == 7.0


def test_the_quota_view_reports_what_is_left_on_each_dimension(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _spend(asgi, run_id, cost=2.5, input_tokens=100, output_tokens=150)
    _set_quota(asgi, _default_team_id(asgi), limit_cost=10.0, limit_runs=3, limit_tokens=1_000)

    quota = asgi.store.team_quota(_default_team_id(asgi))
    assert quota is not None
    assert quota.usage.cost_remaining == 7.5
    assert quota.usage.runs_remaining == 2
    assert quota.usage.tokens_remaining == 750
    assert quota.usage.status == STATUS_OK


# --------------------------------------------------------------------------- #
# 4. the gate
# --------------------------------------------------------------------------- #


def test_a_hard_quota_refuses_the_run_that_would_cross_it(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_runs=0)

    with pytest.raises(RunGateError) as exc_info:
        asgi.store.start_run(run_id)

    error = exc_info.value
    assert error.code == "team_quota_exceeded"
    assert error.status_code == 400
    assert error.details["exceeded"] == ["runs"]
    assert error.details["team_id"] == _default_team_id(asgi)
    assert error.details["period"] == "calendar_month"
    # The refusal is recorded before it is raised, and the record names the
    # operation the caller tried, not a generic "quota".
    events = _events(asgi, run_id, "run.team_quota")
    assert [event.event_type for event in events] == ["run.team_quota_exceeded"]
    assert events[0].payload["operation"] == "cost_reservation"


def test_a_soft_quota_warns_and_lets_the_run_go(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(
        asgi,
        _default_team_id(asgi),
        limit_runs=0,
        enforcement=TeamQuotaEnforcement.SOFT,
    )

    started = asgi.store.start_run(run_id)

    assert started is not None
    warnings = _events(asgi, run_id, "run.team_quota_warning")
    assert warnings
    assert all(event.severity.value == "warning" for event in warnings)


def test_an_override_lets_exactly_the_exempted_run_through(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_runs=0)

    asgi.store.confirm_quota_override(
        run_id, accept=True, note="一次性放行", actor=_default_actor()
    )
    started = asgi.store.start_run(run_id)

    assert started is not None
    # The record changes from a refusal to an exemption — the override is not a
    # bypass of the log, it is a different entry in it.
    assert _events(asgi, run_id, "run.team_quota_exceeded") == []
    overridden = _events(asgi, run_id, "run.team_quota_overridden")
    assert overridden
    assert overridden[0].payload["overridden"] is True


def test_retracting_an_override_restores_the_refusal(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_runs=0)
    asgi.store.confirm_quota_override(run_id, accept=True, actor=_default_actor())

    asgi.store.confirm_quota_override(
        run_id, accept=False, note="收回", actor=_default_actor()
    )

    assert asgi.store.quota_override(run_id).accepted is False
    with pytest.raises(RunGateError):
        asgi.store.start_run(run_id)


def test_a_paused_quota_is_not_enforced(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_runs=0, status="paused")

    started = asgi.store.start_run(run_id)

    assert started is not None
    assert _events(asgi, run_id, "run.team_quota") == []
    quota = asgi.store.team_quota(_default_team_id(asgi))
    assert quota is not None
    # Paused is reported, not hidden: the ceiling exists, it is just not in force.
    assert quota.status == "paused"
    assert quota.configured is False
    assert quota.usage.status == "paused"


def test_a_team_with_no_quota_writes_no_events_at_all(asgi):
    _project_id, run_id = _project_and_run(asgi)

    asgi.store.start_run(run_id)

    # A warning on every paid call from an unmanaged team would drown the log,
    # so "no ceiling" is answered by the read endpoint, not by an event.
    assert _events(asgi, run_id, "run.team_quota") == []


def test_the_run_start_record_carries_the_team_dimension(asgi):
    _project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_cost=0.0)

    started = asgi.store.start_run(run_id)
    assert started is not None

    recorded = _events(asgi, run_id, "run.cost_reservation_recorded")
    assert recorded
    team_quota = recorded[-1].payload["team_quota"]
    # One event per run, not per paid call: this is where "the team had this
    # much room when the run began" is written down.
    assert team_quota["team_id"] == _default_team_id(asgi)
    assert team_quota["period"] == "calendar_month"


def test_a_ceiling_follows_the_projects_attribution(asgi):
    project_id, run_id = _project_and_run(asgi)
    _set_quota(asgi, _default_team_id(asgi), limit_runs=0)
    looser = asgi.store.create_team(TeamCreate(name="宽松团队"), _default_actor())

    # On the team that is out of allowance, the run is refused...
    with pytest.raises(RunGateError):
        asgi.store.start_run(run_id)

    # ...and re-pointing the project is exactly the move that escapes a ceiling,
    # which is why it needs ``quota:manage``. With no ceiling on the new team
    # there is nothing to refuse.
    status, _, moved = asgi.request(
        "PATCH",
        f"/api/projects/{project_id}/team",
        json_body={"team_id": looser.team_id},
        actor_id=OWNER,
    )
    assert status == 200, moved
    started = asgi.store.start_run(run_id)
    assert started is not None


def test_only_a_quota_manager_can_move_a_projects_team(asgi):
    project_id, _run_id = _project_and_run(asgi)
    asgi.store.add_team_member(
        _default_team_id(asgi),
        TeamMemberAdd(user_id=VIEWER, role=TeamRole.MEMBER.value),
        _default_actor(),
    )
    asgi.store.session.commit()

    # A member can create projects and start runs, but not choose which team's
    # allowance they spend — otherwise a hard ceiling is a formality.
    status, _, refusal = asgi.request(
        "PATCH",
        f"/api/projects/{project_id}/team",
        json_body={"team_id": None},
        actor_id=VIEWER,
    )

    assert status == 403
    assert refusal["detail"]["permission"] == "quota:manage"


def test_moving_a_project_to_an_unknown_team_is_refused(asgi):
    project_id, _run_id = _project_and_run(asgi)

    status, _, _ = asgi.request(
        "PATCH",
        f"/api/projects/{project_id}/team",
        json_body={"team_id": "team_missing"},
        actor_id=OWNER,
    )

    assert status == 404
    # The refusal left the project where it was.
    project = asgi.store.projects.get_project(project_id)
    assert project is not None
    assert project.team_id is None


# --------------------------------------------------------------------------- #
# 5. the API surface
# --------------------------------------------------------------------------- #


def test_the_quota_endpoints_round_trip(asgi):
    team_id = _default_team_id(asgi)

    status, _, before = asgi.request("GET", f"/api/teams/{team_id}/quota", actor_id=OWNER)
    assert status == 200, before
    assert before["configured"] is False
    assert before["period"] == "calendar_month"

    status, _, updated = asgi.request(
        "PUT",
        f"/api/teams/{team_id}/quota",
        json_body={"limit_cost": 12.5, "limit_runs": 4, "enforcement": "soft"},
        actor_id=OWNER,
    )
    assert status == 200, updated
    assert updated["configured"] is True
    assert updated["limit_cost"] == 12.5
    assert updated["enforcement"] == "soft"
    assert updated["team_name"]

    # PUT is idempotent: a team has one quota, so a second send lands on the
    # same state rather than creating a second row.
    status, _, again = asgi.request(
        "PUT",
        f"/api/teams/{team_id}/quota",
        json_body={"limit_cost": 12.5, "limit_runs": 4, "enforcement": "soft"},
        actor_id=OWNER,
    )
    assert status == 200
    assert again["quota_id"] == updated["quota_id"]

    status, _, removed = asgi.request(
        "DELETE", f"/api/teams/{team_id}/quota", actor_id=OWNER
    )
    assert status == 200, removed
    # The response is the post-delete view, so a caller can see the team is now
    # measured against nothing.
    assert removed["configured"] is False
    assert removed["quota_id"] is None

    status, _, _ = asgi.request("DELETE", f"/api/teams/{team_id}/quota", actor_id=OWNER)
    assert status == 404


def test_the_tenant_overview_lists_every_team_including_the_unguarded(asgi):
    _project_and_run(asgi)
    second = asgi.store.create_team(TeamCreate(name="无配额团队"), _default_actor())
    _set_quota(asgi, _default_team_id(asgi), limit_cost=5.0)

    status, _, quotas = asgi.request("GET", "/api/team-quotas", actor_id=OWNER)

    assert status == 200, quotas
    by_team = {quota["team_id"]: quota for quota in quotas}
    assert by_team[_default_team_id(asgi)]["configured"] is True
    # A team with no ceiling is listed too: hiding it would make "no row" and
    # "not a team" look the same.
    assert by_team[second.team_id]["configured"] is False


def test_an_unknown_team_is_a_404(asgi):
    status, _, _ = asgi.request("GET", "/api/teams/team_missing/quota", actor_id=OWNER)
    assert status == 404
    status, _, _ = asgi.request(
        "PUT",
        "/api/teams/team_missing/quota",
        json_body={"limit_cost": 1.0},
        actor_id=OWNER,
    )
    # A validated body on an unknown team still 404s: existence is answered
    # before the ceiling is looked at, not after.
    assert status == 404
    status, _, _ = asgi.request("DELETE", "/api/teams/team_missing/quota", actor_id=OWNER)
    assert status == 404
    status, _, _ = asgi.request(
        "PATCH",
        "/api/projects/project_missing/team",
        json_body={"team_id": None},
        actor_id=OWNER,
    )
    assert status == 404


def test_a_viewer_can_read_quotas_but_not_move_the_number(asgi):
    team_id = _default_team_id(asgi)
    asgi.store.add_team_member(
        team_id, TeamMemberAdd(user_id=VIEWER, role=TeamRole.VIEWER.value), _default_actor()
    )
    asgi.store.session.commit()

    assert asgi.request("GET", "/api/team-quotas", actor_id=VIEWER)[0] == 200
    assert asgi.request("GET", f"/api/teams/{team_id}/quota", actor_id=VIEWER)[0] == 200
    # Raising the tenant's spending ceiling is not a read, and a viewer is
    # explicitly not trusted with it.
    status, _, refusal = asgi.request(
        "PUT",
        f"/api/teams/{team_id}/quota",
        json_body={"limit_cost": 1.0},
        actor_id=VIEWER,
    )
    assert status == 403
    assert refusal["detail"]["permission"] == "quota:manage"


def test_the_override_endpoint_records_who_exempted_the_run(asgi):
    _project_id, run_id = _project_and_run(asgi)

    status, _, override = asgi.request(
        "POST",
        f"/api/runs/{run_id}/quota-override",
        json_body={"accept": True, "note": "本季度最后一次"},
        actor_id=OWNER,
    )

    assert status == 200, override
    assert override["accepted"] is True
    assert override["accepted_by"] == OWNER
    assert override["note"] == "本季度最后一次"
    assert override["accepted_at"]


def test_the_override_endpoint_404s_on_an_unknown_run(asgi):
    status, _, _ = asgi.request(
        "POST",
        "/api/runs/run_missing/quota-override",
        json_body={"accept": True},
        actor_id=OWNER,
    )
    assert status == 404


def test_a_negative_or_malformed_quota_is_rejected(asgi):
    team_id = _default_team_id(asgi)

    cases = [
        {"limit_cost": -1.0},
        {"limit_runs": -1},
        {"limit_tokens": -1},
        {"enforcement": "sometimes"},
        {"period": "fortnightly"},
        {"status": "maybe"},
    ]
    for body in cases:
        status, _, _ = asgi.request(
            "PUT", f"/api/teams/{team_id}/quota", json_body=body, actor_id=OWNER
        )
        assert status == 422, body

    # Nothing was written by any of the rejected attempts.
    quota = asgi.store.team_quota(team_id)
    assert quota is not None
    assert quota.configured is False
