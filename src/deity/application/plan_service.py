"""Application entry point for immutable read-only Plan Runs."""

from datetime import UTC, datetime

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts.planning import ExecutionMode, PlanArtifact


class PlanService:
    def __init__(self, runs: RunService) -> None:
        self.runs = runs

    async def create(
        self, task: str, *, model_name: str, session_id: str | None = None
    ) -> PlanArtifact:
        """Run the model in PLAN capability mode and return only persisted truth."""
        if not self.runs.coding.plan_build_enabled:
            raise ValueError("Plan/Build is disabled")
        result = await self.runs.run(
            task,
            model_name=model_name,
            session_id=session_id,
            execution_mode=ExecutionMode.PLAN,
        )
        repo = await SQLiteSessionRepository.open(self.runs.db_path)
        try:
            return await repo.plan_for_run(result.run_id)
        except KeyError:
            if await repo.rejected_plan_submission(result.run_id):
                raise ValueError(
                    "计划提交被拒绝：模型生成的步骤不符合结构要求；请重试 Plan。"
                ) from None
            raise ValueError("模型未提交结构化计划；请重试 Plan。") from None
        finally:
            await repo.close()

    async def get(self, plan_id: str) -> PlanArtifact:
        repo = await SQLiteSessionRepository.open(self.runs.db_path)
        try:
            return await repo.get_plan(plan_id)
        finally:
            await repo.close()

    async def list(self, session_id: str | None = None) -> tuple[PlanArtifact, ...]:
        repo = await SQLiteSessionRepository.open(self.runs.db_path)
        try:
            return await repo.list_plans(session_id)
        finally:
            await repo.close()

    async def buildable(self, session_id: str) -> tuple[PlanArtifact, ...]:
        """List draft plans belonging to this session and workspace."""
        repo = await SQLiteSessionRepository.open(self.runs.db_path)
        try:
            candidates = await repo.list_plans(session_id)
            buildable: list[PlanArtifact] = []
            for item in candidates:
                if item.status != "draft":
                    continue
                age = (datetime.now(UTC) - datetime.fromisoformat(item.created_at)).total_seconds()
                if age > self.runs.coding.plan_ttl_seconds:
                    continue
                snapshot = await repo.get_workspace_snapshot(item.workspace_snapshot_id)
                if snapshot.workspace_root == str(self.runs.workspace):
                    buildable.append(item)
            return tuple(buildable)
        finally:
            await repo.close()

    async def get_buildable(self, plan_id: str, session_id: str) -> PlanArtifact:
        """Reject another session/workspace or a non-draft plan."""
        repo = await SQLiteSessionRepository.open(self.runs.db_path)
        try:
            artifact = await repo.get_plan(plan_id)
            snapshot = await repo.get_workspace_snapshot(artifact.workspace_snapshot_id)
            if (
                artifact.session_id != session_id
                or artifact.status != "draft"
                or snapshot.workspace_root != str(self.runs.workspace)
            ):
                raise ValueError("计划不属于当前会话或工作区，或不可执行")
            return artifact
        finally:
            await repo.close()
