"""T76: a run must not start past its budget, nor reach a paid provider unasked.

Two independent gates are covered here:

* **cost reservation** — the execution plan sums every selected tool's estimate,
  so an unaffordable plan is refused at ``start`` instead of being discovered one
  charge at a time;
* **first-use paid confirmation** — the first real charge to a paid provider needs
  a per-run, per-provider acknowledgement from a human.

The registry used by the store-level tests is built from two catalogs: the real
default catalog plus a generated one that gives every pipeline capability a local
tool and hands ``image_generation`` to a paid API tool. That makes the reservation
non-zero and deterministic without needing a provider key.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.actor import ActorContext
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.main import app
from apps.api.app.models import (
    ImageGenerateRequest,
    ProjectCreate,
    ProviderConsentRequest,
    RunCreate,
    RunStatus,
)
from apps.api.app.routes import confirm_paid_provider
from apps.api.app.routes import start_run as start_run_route
from apps.api.app.run_gate import RunGateError
from packages.pipeline_kit.loader import PipelineLoader
from packages.tool_kit.contracts import ToolDefinition, ToolStatus
from packages.tool_kit.execution_plan import build_cost_reservation, build_execution_plan
from packages.tool_kit.preflight import build_preflight_report
from packages.tool_kit.registry import ToolRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "apps" / "web"

#: Route functions take the resolved actor as a parameter; the decision log
#: records who confirmed a paid provider, so the direct calls need one.
ACTOR = ActorContext(
    actor_id="user_test_owner",
    tenant_id="tenant_default",
    display_name="Test Owner",
)


def _pipeline_capabilities(pipeline) -> list[str]:
    capabilities = set(pipeline.required_capabilities) | set(pipeline.optional_capabilities)
    for stage in pipeline.stages:
        capabilities.update(stage.required_capabilities)
        capabilities.update(stage.optional_capabilities)
    return sorted(capabilities)


def _local_tool(tool_key: str, capability: str) -> ToolDefinition:
    return ToolDefinition(
        tool_key=tool_key,
        display_name=tool_key,
        capability=capability,
        provider="internal",
        runtime_type="local",
        status=ToolStatus.AVAILABLE,
        quality_profile={"tier": "preview"},
    )


def _paid_api_tool(tool_key: str, capability: str, *, cost: float = 1.25) -> ToolDefinition:
    return ToolDefinition(
        tool_key=tool_key,
        display_name=tool_key,
        capability=capability,
        provider="dashscope",
        runtime_type="api",
        status=ToolStatus.AVAILABLE,
        cost_model={"unit": "operation", "estimated_cost": cost, "currency": "USD"},
        quality_profile={"tier": "standard"},
    )


# --------------------------------------------------------------------------- #
# execution plan: the reservation itself
# --------------------------------------------------------------------------- #


def _report_and_plan(*, cost_budget: float | None) -> tuple[dict, dict]:
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    tools = [
        _local_tool(f"local_{capability}", capability)
        for capability in _pipeline_capabilities(pipeline)
        if capability != "image_generation"
    ]
    tools.append(_paid_api_tool("paid_image_generation", "image_generation"))
    run_input: dict = {"raw_script": "预算测试"}
    if cost_budget is not None:
        run_input["cost_budget"] = cost_budget
    report = build_preflight_report(pipeline=pipeline, tools=tools, run_input=run_input)
    return report, build_execution_plan(preflight_report=report)


def test_execution_plan_reserves_the_selected_tool_cost() -> None:
    _report, plan = _report_and_plan(cost_budget=0.5)

    reservation = plan["cost_reservation"]
    # image_generation is planned in two stages, and each one is a real provider
    # call, so the reservation is deliberately conservative: 2 x 1.25.
    assert reservation["reserved_cost"] == 2.5
    assert reservation["currency"] == "USD"
    assert reservation["budget_limit"] == 0.5
    assert reservation["budget_status"] == "exceeds_budget"
    assert reservation["paid_providers"] == ["dashscope"]
    assert [item["tool_key"] for item in reservation["paid_tools"]] == [
        "paid_image_generation",
        "paid_image_generation",
    ]
    assert {item["stage_key"] for item in reservation["paid_tools"]} == {
        "storyboard",
        "asset_generation",
    }
    # Local tools are free, so they never enter the paid list.
    assert all(item["estimated_cost"] > 0.0 for item in reservation["paid_tools"])
    assert len(reservation["line_items"]) > len(reservation["paid_tools"])


def test_plan_without_a_budget_marks_the_reservation_unconfigured() -> None:
    _report, plan = _report_and_plan(cost_budget=None)

    reservation = plan["cost_reservation"]
    assert reservation["budget_limit"] is None
    assert reservation["budget_status"] == "not_configured"
    # The reservation is still reported, it is just not compared to a limit.
    assert reservation["reserved_cost"] == 2.5


def test_plan_within_budget_is_marked_as_such() -> None:
    _report, plan = _report_and_plan(cost_budget=10.0)

    assert plan["cost_reservation"]["budget_status"] == "within_budget"


def test_reservation_ignores_skipped_and_blocked_decisions() -> None:
    plan = {
        "run_input": {},
        "decision_log": [
            {
                "stage_key": "s",
                "capability": "c1",
                "decision": "skip_optional",
                "selected_tool_key": "paid_skipped",
                "selected_provider": "dashscope",
                "selected_tool": {"cost_model": {"estimated_cost": 9.0}, "runtime": "api"},
            },
            {
                "stage_key": "s",
                "capability": "c2",
                "decision": "blocked",
                "selected_tool_key": None,
                "selected_provider": None,
                "selected_tool": None,
            },
            {
                "stage_key": "s",
                "capability": "c3",
                "decision": "use_tool",
                "selected_tool_key": "paid_used",
                "selected_provider": "dashscope",
                "selected_tool": {"cost_model": {"estimated_cost": 2.0}, "runtime": "api"},
            },
        ],
    }

    reservation = build_cost_reservation(plan)
    assert reservation["reserved_cost"] == 2.0
    assert [item["tool_key"] for item in reservation["line_items"]] == ["paid_used"]


# --------------------------------------------------------------------------- #
# store registry helpers
# --------------------------------------------------------------------------- #


def _write_paid_catalog(tmp_path: Path, pipeline, *, paid_cost: float = 1.25) -> Path:
    entries = []
    for capability in _pipeline_capabilities(pipeline):
        if capability == "image_generation":
            entries.append(
                {
                    "tool_key": "paid_image_generation",
                    "display_name": "Paid Image Generation",
                    "capability": capability,
                    "provider": "dashscope",
                    "runtime_type": "api",
                    "cost_model": {
                        "unit": "operation",
                        "estimated_cost": paid_cost,
                        "currency": "USD",
                    },
                }
            )
        else:
            entries.append(
                {
                    "tool_key": f"local_{capability}",
                    "display_name": f"Local {capability}",
                    "capability": capability,
                    "provider": "internal",
                    "runtime_type": "local",
                }
            )
    path = tmp_path / "paid_tools.json"
    path.write_text(json.dumps({"tools": entries}), encoding="utf-8")
    return path


class _PaidImageAdapter:
    """A paid, always-available image provider stand-in."""

    provider_key = "dashscope"
    tool_key = "dashscope_image"

    def check_availability(self) -> bool:
        return True

    def estimate_cost(self, capability, payload) -> float:
        assert capability == "image_generation"
        return 1.25

    def generate(self, payload):
        return {
            "content": b"image bytes",
            "mime_type": "image/png",
            "image_url": "https://example.com/person.png",
            "model": payload.get("model") or "qwen-image",
            "size": payload.get("size") or "1024*1024",
        }


def _store(
    tmp_path,
    *,
    cost_budget: float | None,
    name: str,
    image_adapter=None,
) -> tuple[DatabaseStore, object]:
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    pipeline = PipelineLoader().get_pipeline("avatar_spokesperson")
    catalog = _write_paid_catalog(tmp_path, pipeline)
    registry = ToolRegistry(
        catalog_paths=[PROJECT_ROOT / "tool_catalog" / "default.json", catalog],
        env={},
        binary_checker=lambda name: f"/fake/bin/{name}",
    )
    store = DatabaseStore(
        Session(engine),
        tool_registry=registry,
        image_adapter=image_adapter,
    )
    project = store.create_project(ProjectCreate(title="cost gate"))
    run = store.create_run(
        project_id=project.project_id,
        payload=RunCreate(raw_script="预算与付费确认测试", cost_budget=cost_budget),
        pipeline=pipeline,
    )
    return store, run


# --------------------------------------------------------------------------- #
# run start gate: reservation
# --------------------------------------------------------------------------- #


def test_start_blocks_when_the_reservation_exceeds_the_budget(tmp_path) -> None:
    store, run = _store(tmp_path, cost_budget=0.5, name="reserve-block.db")

    with pytest.raises(RunGateError) as exc_info:
        store.start_run(run.run_id)

    error = exc_info.value
    assert error.code == "cost_reservation_exceeds_budget"
    assert error.status_code == 400
    assert error.details["reserved_cost"] == 2.5
    assert error.details["budget_limit"] == 0.5
    assert error.details["paid_providers"] == ["dashscope"]

    blocked = [
        event
        for event in store.list_events(run.run_id, limit=200)
        if event.event_type == "run.start_blocked"
        and event.payload.get("code") == "cost_reservation_exceeds_budget"
    ]
    assert blocked


def test_start_records_the_reservation_when_within_budget(tmp_path) -> None:
    store, run = _store(tmp_path, cost_budget=10.0, name="reserve-ok.db")

    started = store.start_run(run.run_id)

    assert started is not None
    assert started.status is not RunStatus.DRAFT
    recorded = [
        event
        for event in store.list_events(run.run_id, limit=200)
        if event.event_type == "run.cost_reservation_recorded"
    ]
    assert recorded
    assert recorded[-1].payload["budget_status"] == "within_budget"
    assert recorded[-1].payload["reserved_cost"] == 2.5
    assert recorded[-1].payload["paid_providers"] == ["dashscope"]


def test_start_without_a_budget_makes_the_gap_visible(tmp_path) -> None:
    store, run = _store(tmp_path, cost_budget=None, name="reserve-none.db")

    started = store.start_run(run.run_id)

    assert started is not None
    recorded = [
        event
        for event in store.list_events(run.run_id, limit=200)
        if event.event_type == "run.cost_reservation_recorded"
    ]
    assert recorded
    assert recorded[-1].payload["budget_status"] == "not_configured"


# --------------------------------------------------------------------------- #
# dispatch gate: first paid use
# --------------------------------------------------------------------------- #


def _image_request() -> ImageGenerateRequest:
    return ImageGenerateRequest(
        prompt="front-facing product spokesperson",
        filename="person.png",
    )


def test_paid_provider_requires_a_first_use_confirmation(tmp_path) -> None:
    store, run = _store(
        tmp_path,
        cost_budget=None,
        name="paid-block.db",
        image_adapter=_PaidImageAdapter(),
    )

    with pytest.raises(RunGateError) as exc_info:
        store.generate_image(run.run_id, _image_request())

    error = exc_info.value
    assert error.code == "paid_provider_confirmation_required"
    assert error.details["provider"] == "dashscope"
    assert error.details["tool_key"] == "dashscope_image"
    assert error.details["estimated_cost"] == 1.25
    assert error.details["resolution"]["body"] == {"provider": "dashscope", "accept": True}

    required = [
        event
        for event in store.list_events(run.run_id, limit=200)
        if event.event_type == "run.paid_provider_confirmation_required"
    ]
    assert required


def test_confirming_the_paid_provider_unblocks_the_call(tmp_path) -> None:
    store, run = _store(
        tmp_path,
        cost_budget=None,
        name="paid-ok.db",
        image_adapter=_PaidImageAdapter(),
    )

    store.confirm_paid_provider(run.run_id, provider="dashscope")

    result = store.generate_image(run.run_id, _image_request())
    assert result is not None

    confirmed = [
        event
        for event in store.list_events(run.run_id, limit=200)
        if event.event_type == "run.paid_provider_confirmed"
    ]
    assert confirmed
    assert confirmed[-1].payload["provider"] == "dashscope"


def test_revoking_the_consent_blocks_the_next_call(tmp_path) -> None:
    store, run = _store(
        tmp_path,
        cost_budget=None,
        name="paid-revoke.db",
        image_adapter=_PaidImageAdapter(),
    )
    store.confirm_paid_provider(run.run_id, provider="dashscope")

    store.confirm_paid_provider(run.run_id, provider="dashscope", accept=False)

    with pytest.raises(RunGateError) as exc_info:
        store.generate_image(run.run_id, _image_request())
    assert exc_info.value.code == "paid_provider_confirmation_required"


def test_a_budget_still_applies_before_the_consent_question(tmp_path) -> None:
    store, run = _store(
        tmp_path,
        cost_budget=0.5,
        name="paid-budget-first.db",
        image_adapter=_PaidImageAdapter(),
    )

    with pytest.raises(RunGateError) as exc_info:
        store.generate_image(run.run_id, _image_request())

    # The run cannot afford the call at all, so that is the more useful refusal.
    assert exc_info.value.code == "cost_budget_exceeded"
    assert exc_info.value.details["budget_limit"] == 0.5


# --------------------------------------------------------------------------- #
# run board
# --------------------------------------------------------------------------- #


def test_run_board_exposes_the_reservation_and_pending_consents(tmp_path) -> None:
    store, run = _store(tmp_path, cost_budget=None, name="board.db")
    store.start_run(run.run_id)

    board = store.run_board(run.run_id)

    assert board is not None
    assert board.cost_reservation["reserved_cost"] == 2.5
    assert board.cost_reservation["paid_providers"] == ["dashscope"]
    assert board.paid_providers_pending == ["dashscope"]
    assert board.paid_provider_consents == {}

    store.confirm_paid_provider(run.run_id, provider="dashscope")
    board = store.run_board(run.run_id)
    assert board is not None
    assert board.paid_providers_pending == []
    assert board.paid_provider_consents == {"dashscope": True}


# --------------------------------------------------------------------------- #
# http layer
# --------------------------------------------------------------------------- #


def test_start_route_returns_the_structured_reservation_refusal(tmp_path) -> None:
    store, run = _store(tmp_path, cost_budget=0.5, name="api-reserve.db")

    with pytest.raises(HTTPException) as exc_info:
        start_run_route(run.run_id, store, ACTOR, None)

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "cost_reservation_exceeds_budget"
    assert detail["reserved_cost"] == 2.5
    assert detail["budget_limit"] == 0.5


def test_provider_consent_route_round_trips(tmp_path) -> None:
    store, run = _store(
        tmp_path,
        cost_budget=None,
        name="api-consent.db",
        image_adapter=_PaidImageAdapter(),
    )

    response = confirm_paid_provider(
        run.run_id,
        ProviderConsentRequest(provider="dashscope", note="approved by ops"),
        store,
        ACTOR,
    )

    assert response["accept"] is True
    assert response["paid_provider_consents"] == {"dashscope": True}
    # With the consent recorded, the paid call is allowed through.
    assert store.generate_image(run.run_id, _image_request()) is not None


def test_provider_consent_route_404s_for_an_unknown_run(tmp_path) -> None:
    store, _run = _store(tmp_path, cost_budget=None, name="api-missing.db")

    with pytest.raises(HTTPException) as exc_info:
        confirm_paid_provider(
            "run_does_not_exist",
            ProviderConsentRequest(provider="dashscope"),
            store,
            ACTOR,
        )
    assert exc_info.value.status_code == 404


def test_app_returns_gate_errors_as_structured_json() -> None:
    """Adapter routes raise the gate deep in a call, so the app maps it centrally."""

    handler = app.exception_handlers.get(RunGateError)
    assert handler is not None

    error = RunGateError(
        "paid_provider_confirmation_required",
        "confirm the paid provider",
        details={"provider": "dashscope", "estimated_cost": 1.25},
    )
    response = asyncio.run(handler(None, error))

    assert response.status_code == 400
    payload = json.loads(response.body)
    assert payload["detail"]["code"] == "paid_provider_confirmation_required"
    assert payload["detail"]["provider"] == "dashscope"
    assert payload["detail"]["estimated_cost"] == 1.25


# --------------------------------------------------------------------------- #
# web layer
# --------------------------------------------------------------------------- #


def _page(name: str) -> str:
    return (WEB_ROOT / name).read_text(encoding="utf-8")


def test_workspace_shows_the_reservation_and_offers_paid_confirmation() -> None:
    html = _page("review.html")

    assert "成本预留" in html
    assert "provider-consents" in html
    assert "首次调用付费 provider" in html
    assert "cost_reservation" in html


def test_run_board_shows_the_reservation_and_pending_providers() -> None:
    html = _page("run_board.html")

    assert "成本预留" in html
    assert "cost_reservation" in html
    assert "paid_providers_pending" in html
