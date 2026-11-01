"""Application entry point for immutable read-only Plan Runs."""

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
            raise ValueError("plan run completed without submit_plan") from None
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
