from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from apps.api.app.models import (
    ApprovalRequest,
    Artifact,
    ProductionRun,
    Project,
    ProjectCreate,
    RunCreate,
    RunStatus,
    StageRun,
    StageStatus,
)
from packages.agent_runner.models import AgentInput
from packages.agent_runner.runner import RuleBasedAgentRunner
from packages.pipeline_kit.models import PipelineDefinition


class InMemoryStore:
    """Temporary store for the first runnable skeleton.

    This keeps the MVP API easy to exercise before database migrations are
    added. The V1 implementation should replace this with PostgreSQL-backed
    repositories while preserving the route-level contracts.
    """

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._runs: dict[str, ProductionRun] = {}
        self._pipelines_by_run: dict[str, PipelineDefinition] = {}
        self._artifacts_by_run: dict[str, list[Artifact]] = {}
        self._agent = RuleBasedAgentRunner()

    def create_project(self, payload: ProjectCreate) -> Project:
        project = Project(
            project_id=f"project_{uuid4().hex[:12]}",
            title=payload.title,
            description=payload.description,
            default_language=payload.default_language,
            default_aspect_ratio=payload.default_aspect_ratio,
        )
        self._projects[project.project_id] = project
        return deepcopy(project)

    def get_project(self, project_id: str) -> Project | None:
        project = self._projects.get(project_id)
        return deepcopy(project) if project else None

    def create_run(
        self,
        *,
        project_id: str,
        payload: RunCreate,
        pipeline: PipelineDefinition,
    ) -> ProductionRun:
        stages = [
            StageRun(
                stage_key=stage.stage_key,
                order=stage.order,
                status=StageStatus.PENDING,
                requires_human_review=stage.requires_human_review,
                output_artifact_types=stage.output_artifact_types,
            )
            for stage in pipeline.stages
        ]
        input_payload = payload.model_dump()
        input_payload["pipeline_key"] = pipeline.pipeline_key
        run = ProductionRun(
            run_id=f"run_{uuid4().hex[:12]}",
            project_id=project_id,
            pipeline_key=pipeline.pipeline_key,
            status=RunStatus.DRAFT,
            current_stage_key=stages[0].stage_key if stages else None,
            input_payload=input_payload,
            stages=stages,
        )
        self._runs[run.run_id] = run
        self._pipelines_by_run[run.run_id] = pipeline
        self._artifacts_by_run[run.run_id] = []
        return deepcopy(run)

    def get_run(self, run_id: str) -> ProductionRun | None:
        run = self._runs.get(run_id)
        return deepcopy(run) if run else None

    def start_run(self, run_id: str) -> ProductionRun | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        run.status = RunStatus.RUNNING
        self._advance_until_gate(run)
        return deepcopy(run)

    def review_stage(
        self,
        run_id: str,
        stage_key: str,
        payload: ApprovalRequest,
    ) -> ProductionRun | None:
        run = self._runs.get(run_id)
        if run is None:
            return None

        target_index = next((i for i, stage in enumerate(run.stages) if stage.stage_key == stage_key), None)
        if target_index is None:
            return None

        stage = run.stages[target_index]
        if payload.decision in {"approved", "approved_with_changes"}:
            stage.status = StageStatus.APPROVED
            if target_index + 1 < len(run.stages):
                next_stage = run.stages[target_index + 1]
                run.current_stage_key = next_stage.stage_key
                run.status = RunStatus.RUNNING
                self._advance_until_gate(run)
            else:
                run.status = RunStatus.COMPLETED
                run.current_stage_key = None
        elif payload.decision == "rejected":
            stage.status = StageStatus.REJECTED
            run.status = RunStatus.PAUSED
        elif payload.decision == "cancelled":
            stage.status = StageStatus.CANCELLED
            run.status = RunStatus.CANCELLED
        return deepcopy(run)

    def list_artifacts(self, run_id: str) -> list[Artifact] | None:
        artifacts = self._artifacts_by_run.get(run_id)
        return deepcopy(artifacts) if artifacts is not None else None

    def _advance_until_gate(self, run: ProductionRun) -> None:
        pipeline = self._pipelines_by_run[run.run_id]
        while run.current_stage_key is not None:
            stage_index = next(
                i for i, candidate in enumerate(run.stages) if candidate.stage_key == run.current_stage_key
            )
            stage_run = run.stages[stage_index]
            stage_definition = pipeline.stages[stage_index]
            stage_run.status = StageStatus.RUNNING

            agent_output = self._agent.run_stage(
                AgentInput(
                    run_id=run.run_id,
                    pipeline_key=run.pipeline_key,
                    stage_key=stage_run.stage_key,
                    stage_definition=stage_definition.model_dump(),
                    current_artifacts=self._current_artifact_payloads(run.run_id),
                    available_tools=[],
                    user_preferences=run.input_payload,
                    cost_policy={},
                )
            )
            if agent_output.artifact_type and agent_output.artifact_payload is not None:
                self._append_artifact(
                    run_id=run.run_id,
                    stage_key=stage_run.stage_key,
                    artifact_type=agent_output.artifact_type,
                    payload=agent_output.artifact_payload,
                )

            if stage_run.requires_human_review:
                stage_run.status = StageStatus.WAITING_REVIEW
                run.status = RunStatus.WAITING_REVIEW
                return

            stage_run.status = StageStatus.COMPLETED
            if stage_index + 1 >= len(run.stages):
                run.current_stage_key = None
                run.status = RunStatus.COMPLETED
                return
            run.current_stage_key = run.stages[stage_index + 1].stage_key

    def _append_artifact(
        self,
        *,
        run_id: str,
        stage_key: str,
        artifact_type: str,
        payload: dict,
    ) -> Artifact:
        existing_versions = [
            artifact.version
            for artifact in self._artifacts_by_run[run_id]
            if artifact.artifact_type == artifact_type
        ]
        artifact = Artifact(
            artifact_id=f"artifact_{uuid4().hex[:12]}",
            run_id=run_id,
            stage_key=stage_key,
            artifact_type=artifact_type,
            version=(max(existing_versions) + 1) if existing_versions else 1,
            is_current=True,
            payload=payload,
        )
        for existing_artifact in self._artifacts_by_run[run_id]:
            if existing_artifact.artifact_type == artifact_type:
                existing_artifact.is_current = False
        self._artifacts_by_run[run_id].append(artifact)
        return artifact

    def _current_artifact_payloads(self, run_id: str) -> dict:
        payloads = {}
        for artifact in self._artifacts_by_run[run_id]:
            payloads[artifact.artifact_type] = artifact.payload
        return payloads


store = InMemoryStore()
