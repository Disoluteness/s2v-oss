from packages.agent_runner.models import AgentInput, AgentOutput
from packages.agent_runner.planner import SpokespersonAdPlanner


class RuleBasedAgentRunner:
    """Stage runner for the MVP orchestration contract.

    The intake stage stays deterministic. Planning stages delegate to the
    planner, which can use an explicitly configured LLM provider with a local
    deterministic fallback.
    """

    def __init__(self, *, planner: SpokespersonAdPlanner | None = None) -> None:
        self.planner = planner or SpokespersonAdPlanner.from_environment()

    def run_stage(self, payload: AgentInput) -> AgentOutput:
        stage_key = payload.stage_key
        if stage_key == "intake":
            return self._intake(payload)
        if stage_key == "plan":
            return self._planner_output(self.planner.create_production_plan(self._planner_context(payload)))
        if stage_key == "script":
            return self._planner_output(self.planner.create_spoken_script(self._planner_context(payload)))
        if stage_key == "storyboard":
            return self._planner_output(self.planner.create_shot_list(self._planner_context(payload)))
        return AgentOutput(
            next_action="request_review" if payload.stage_definition.get("requires_human_review") else "write_artifact",
            artifact_type=(payload.stage_definition.get("output_artifact_types") or [None])[0],
            artifact_payload={"stage_key": stage_key, "status": "placeholder"},
            review_required=bool(payload.stage_definition.get("requires_human_review")),
            summary=f"Generated placeholder output for {stage_key}.",
        )

    def _intake(self, payload: AgentInput) -> AgentOutput:
        preferences = payload.user_preferences
        artifact = {
            "version": "1.0",
            "language": "zh-CN",
            "brand_name": preferences.get("brand_name"),
            "product_name": preferences.get("product_name"),
            "raw_script": preferences.get("raw_script", ""),
            "target_platform": preferences.get("target_platform", "douyin"),
            "aspect_ratio": preferences.get("aspect_ratio", "9:16"),
            "target_duration_seconds": preferences.get("target_duration_seconds", 15),
            "selling_points": preferences.get("selling_points", []),
            "must_keep_lines": preferences.get("must_keep_lines", []),
            "avoid_terms": preferences.get("avoid_terms", []),
        }
        return AgentOutput(
            next_action="write_artifact",
            artifact_type="intake_brief",
            artifact_payload=artifact,
            review_required=False,
            summary="Created intake brief from user input.",
        )

    def _planner_context(self, payload: AgentInput) -> dict:
        return {
            "run_id": payload.run_id,
            "pipeline_key": payload.pipeline_key,
            "stage_key": payload.stage_key,
            "stage_definition": payload.stage_definition,
            "current_artifacts": payload.current_artifacts,
            "available_tools": payload.available_tools,
            "user_preferences": payload.user_preferences,
            "cost_policy": payload.cost_policy,
        }

    def _planner_output(self, result) -> AgentOutput:
        return AgentOutput(
            next_action="write_artifact",
            artifact_type=result.artifact_type,
            artifact_payload=result.payload,
            review_required=True,
            summary=result.summary,
            risk_flags=result.risk_flags,
        )
