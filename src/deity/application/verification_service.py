"""Detect, execute and persist deterministic verification facts."""

import asyncio
from pathlib import Path
from uuid import uuid4

from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.verification import ProjectVerificationDetector, VerificationExecutor
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.verification import FinalReport, VerificationPlan
from deity.core.verification import aggregate_verification


class VerificationService:
    def __init__(
        self,
        db_path: Path,
        workspace: Path,
        *,
        detector: ProjectVerificationDetector | None = None,
        executor: VerificationExecutor | None = None,
    ) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.detector = detector or ProjectVerificationDetector(self.workspace)
        self.executor = executor or VerificationExecutor(self.workspace)

    async def detect(self) -> VerificationPlan:
        return await self.detector.detect()

    async def run_plan(self, plan: VerificationPlan, *, run_id: str | None = None) -> FinalReport:
        """Persist plan before execution and aggregate only committed check evidence."""
        repo = await SQLiteSessionRepository.open(self.db_path)
        linked_run_id = run_id or ""
        verification_id = ""
        owns_run = run_id is None
        try:
            if owns_run:
                request = AgentRequest(
                    str(uuid4()),
                    str(uuid4()),
                    str(self.workspace),
                    "deterministic verification",
                    "verification",
                    provider_name="deterministic",
                )
                linked_run_id = await repo.create_run(request)
                await repo.set_status(linked_run_id, RunStatus.RUNNING)
            else:
                info = await repo.run_info(linked_run_id)
                if info["workspace_root"] != str(self.workspace):
                    raise ValueError("verification run workspace mismatch")
            snapshot = await GitWorkspaceAdapter(self.workspace, ignored=(self.db_path,)).capture()
            await repo.create_workspace_snapshot(snapshot, run_id=linked_run_id)
            verification_id = await repo.create_verification(
                linked_run_id, snapshot.snapshot_id, plan
            )
            checks = await self.executor.run(plan)
            for ordinal, evidence in enumerate(checks, 1):
                await repo.record_verification_check(
                    verification_id, linked_run_id, ordinal, evidence
                )
            status = aggregate_verification(checks)
            await repo.complete_verification(verification_id, linked_run_id, status)
            if owns_run:
                await repo.set_status(linked_run_id, RunStatus.COMPLETED)
            return await repo.verification_report(linked_run_id)
        except asyncio.CancelledError:
            if verification_id:
                await repo.abort_verification(verification_id, linked_run_id, "cancelled")
            if owns_run and linked_run_id and await repo.status(linked_run_id) == RunStatus.RUNNING:
                await repo.set_status(linked_run_id, RunStatus.CANCELLED)
            raise
        except Exception:
            if verification_id:
                await repo.abort_verification(verification_id, linked_run_id, "execution_error")
            if owns_run and linked_run_id:
                current = await repo.status(linked_run_id)
                if current == RunStatus.RUNNING:
                    await repo.set_status(linked_run_id, RunStatus.FAILED)
            raise
        finally:
            await repo.close()

    async def run_detected(self) -> FinalReport:
        return await self.run_plan(await self.detect())

    async def report(self, run_id: str) -> FinalReport:
        repo = await SQLiteSessionRepository.open(self.db_path)
        try:
            return await repo.verification_report(run_id)
        finally:
            await repo.close()
