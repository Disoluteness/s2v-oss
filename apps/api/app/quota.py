"""Team quota arithmetic (V8.3).

Two pure concerns live here, deliberately free of any database access so they
can be tested exhaustively without a session:

* **the window** — ``period`` is the only stored thing, and the start/end it
  implies is recomputed on every read. Storing a window would require a
  rollover job, and a deployment without that job would stop enforcing the
  moment the month turned over, silently.
* **the verdict** — given limits and observed usage, is this call allowed? The
  answer is a small dataclass rather than an exception so that the caller (the
  store layer) decides whether a breach refuses the call, warns, or is already
  covered by an override, and can record the same numbers in an event.

Nothing in this module knows about runs, projects or teams: attribution is the
repository's job, judging is this one's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from apps.api.app.models import TeamQuotaEnforcement, TeamQuotaPeriod

#: How far back ``rolling_30d`` reaches. A constant rather than a setting: the
#: name of the period *is* the number, and making them separately configurable
#: would let a deployment ship a period whose name lies.
ROLLING_WINDOW_DAYS = 30

DIMENSION_COST = "cost"
DIMENSION_RUNS = "runs"
DIMENSION_TOKENS = "tokens"

#: ``status`` values a verdict can carry. ``not_configured`` is not a synonym for
#: "ok": a team with no ceiling is *visibly* unguarded, which is the whole point
#: of reporting it (T76 made the same choice for budgets). ``paused`` is applied
#: by the DTO layer, not by :func:`evaluate_quota` — a paused quota still has
#: numbers, it just is not in force.
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_OK = "ok"
STATUS_EXCEEDED = "exceeded"
STATUS_PAUSED = "paused"


@dataclass(frozen=True)
class QuotaWindow:
    """The half-open interval ``[start, end)`` a quota is measured over.

    ``start`` is ``None`` for ``lifetime``; ``end`` is never ``None`` so callers
    can always bound the query without a special case.
    """

    start: datetime | None
    end: datetime


@dataclass
class QuotaUsage:
    """What a team actually consumed inside one window."""

    window: QuotaWindow
    estimated_cost: float = 0.0
    actual_cost: float = 0.0
    spent_cost: float = 0.0
    run_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def dimension(self, name: str) -> float | int:
        if name == DIMENSION_COST:
            return self.spent_cost
        if name == DIMENSION_RUNS:
            return self.run_count
        return self.total_tokens


@dataclass
class QuotaVerdict:
    """The judgement for one prospective call."""

    status: str
    enforcement: TeamQuotaEnforcement | None = None
    exceeded: tuple[str, ...] = ()
    limits: dict[str, float | None] = field(default_factory=dict)
    current: dict[str, float | int] = field(default_factory=dict)
    projected: dict[str, float | int] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.status != STATUS_NOT_CONFIGURED

    @property
    def blocking(self) -> bool:
        """True only for a *hard* breach — a soft one warns and lets the call run."""

        return bool(self.exceeded) and self.enforcement is TeamQuotaEnforcement.HARD

    def details(self) -> dict[str, object]:
        """The numbers behind the verdict, for the refusal payload and the event."""

        return {
            "period_status": self.status,
            "enforcement": self.enforcement.value if self.enforcement else None,
            "exceeded": list(self.exceeded),
            "limits": dict(self.limits),
            "current": dict(self.current),
            "projected": dict(self.projected),
        }


def quota_window(period: TeamQuotaPeriod | str, *, now: datetime) -> QuotaWindow:
    """The window ``period`` implies, anchored on ``now``.

    ``calendar_month`` snaps to UTC month boundaries so every member of a team
    reads the same reset instant regardless of their own timezone; ``rolling_30d``
    slides with each request; ``lifetime`` is open at the left.
    """

    resolved = period if isinstance(period, TeamQuotaPeriod) else TeamQuotaPeriod(period)
    if resolved is TeamQuotaPeriod.LIFETIME:
        return QuotaWindow(start=None, end=now)
    if resolved is TeamQuotaPeriod.ROLLING_30D:
        return QuotaWindow(start=now - timedelta(days=ROLLING_WINDOW_DAYS), end=now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return QuotaWindow(start=month_start, end=next_month)


def evaluate_quota(
    *,
    period: TeamQuotaPeriod | str,
    usage: QuotaUsage,
    limit_cost: float | None,
    limit_runs: int | None,
    limit_tokens: int | None,
    enforcement: TeamQuotaEnforcement | str,
    additional_cost: float = 0.0,
    additional_runs: int = 0,
    additional_tokens: int = 0,
) -> QuotaVerdict:
    """Judge a call that would add ``additional_*`` on top of ``usage``.

    A quota with no ceilings at all is ``not_configured`` rather than ``ok``:
    the caller must be able to tell "this team is under its cap" from "this team
    has no cap", because only one of those is a fact worth reporting.
    """

    resolved_enforcement = (
        enforcement
        if isinstance(enforcement, TeamQuotaEnforcement)
        else TeamQuotaEnforcement(enforcement)
    )
    limits: dict[str, float | None] = {
        DIMENSION_COST: float(limit_cost) if limit_cost is not None else None,
        DIMENSION_RUNS: float(limit_runs) if limit_runs is not None else None,
        DIMENSION_TOKENS: float(limit_tokens) if limit_tokens is not None else None,
    }
    if all(value is None for value in limits.values()):
        return QuotaVerdict(
            status=STATUS_NOT_CONFIGURED,
            enforcement=resolved_enforcement,
            limits=limits,
        )

    additional: dict[str, float] = {
        DIMENSION_COST: max(0.0, float(additional_cost or 0.0)),
        DIMENSION_RUNS: float(max(0, additional_runs)),
        DIMENSION_TOKENS: float(max(0, additional_tokens)),
    }
    current: dict[str, float | int] = {name: usage.dimension(name) for name in limits}
    projected: dict[str, float | int] = {
        name: round(float(current[name]) + additional[name], 6) for name in limits
    }
    exceeded = tuple(
        name
        for name, limit in limits.items()
        if limit is not None and float(projected[name]) > float(limit)
    )
    return QuotaVerdict(
        status=STATUS_EXCEEDED if exceeded else STATUS_OK,
        enforcement=resolved_enforcement,
        exceeded=exceeded,
        limits=limits,
        current=current,
        projected=projected,
    )
