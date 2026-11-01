"""Detect, execute and persist deterministic verification facts."""

import asyncio
import tempfile
from pathlib import Path
from uuid import uuid4

from deity.adapters.execution.docker import DockerExecutionBackend
from deity.adapters.git import GitWorkspaceAdapter
from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.verification import ProjectVerificationDetector, VerificationExecutor
from deity.application.config import SandboxConfig
from deity.application.sandbox_service import SandboxService, SandboxUnavailable
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.execution import (
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxSpec,
)
from deity.core.contracts.verification import (
    FinalReport,
    VerificationCommand,
    VerificationEvidence,
    VerificationPlan,
    VerificationStatus,
)
from deity.core.verification import aggregate_verification


class VerificationService:
    def __init__(
        self,
        db_path: Path,
        workspace: Path,
        *,
        detector: ProjectVerificationDetector | None = None,
        executor: VerificationExecutor | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> None:
        self.db_path = db_path
        self.workspace = workspace.resolve(strict=True)
        self.detector = detector or ProjectVerificationDetector(self.workspace)
        self.executor = executor or VerificationExecutor(self.workspace)
        self.sandbox = sandbox or SandboxConfig()

    async def _sandbox_check(
        self,
        command: VerificationCommand,
        repo: SQLiteSessionRepository,
        run_id: str,
        verification_id: str,
    ) -> VerificationEvidence:
        config = self.sandbox
        backend = DockerExecutionBackend(
            self.workspace,
            Path(tempfile.gettempdir()) / "deity-sandbox",
            excluded_paths=(
                self.db_path,
                self.db_path.with_name(self.db_path.name + "-wal"),
                self.db_path.with_name(self.db_path.name + "-shm"),
                *(self.db_path.with_suffix(f".v{version}.bak") for version in range(1, 5)),
            ),
            excluded_patterns=config.exclude,
            max_workspace_files=config.max_workspace_files,
            max_workspace_bytes=config.max_workspace_bytes,
            max_artifact_files=config.max_artifact_files,
            max_artifact_bytes=config.max_artifact_bytes,
            cleanup_timeout_seconds=config.cleanup_timeout_seconds,
        )
        request = ExecutionRequest(
            str(uuid4()),
            run_id,
            f"{verification_id}:{uuid4()}",
            command.argv,
            ".",
            SandboxSpec(
                BackendKind.DOCKER,
                config.image,
                ResourceBudget(
                    config.timeout_seconds,
                    config.cpu_count,
                    config.memory_mb * 1024 * 1024,
                    config.pids_limit,
                    config.max_output_chars,
                    config.tmpfs_mb * 1024 * 1024,
                ),
                network=config.network,
                env_names=config.env_names,
            ),
        )
        try:
            result, _manifest = await SandboxService(backend, repo).run(request)
        except SandboxUnavailable as exc:
            return VerificationEvidence(
                str(uuid4()), command, VerificationStatus.NOT_RUN, None, 0, "", reason=str(exc)
            )
        except RuntimeError as exc:
            return VerificationEvidence(
                str(uuid4()), command, VerificationStatus.INCONCLUSIVE, None, 0, "", reason=str(exc)
            )
        status = (
            VerificationStatus.PASSED
            if result.status == ExecutionStatus.SUCCEEDED
            else VerificationStatus.FAILED
            if result.status == ExecutionStatus.FAILED
            else VerificationStatus.INCONCLUSIVE
        )
        return VerificationEvidence(
            str(uuid4()),
            command,
            status,
            result.exit_code,
            result.duration_ms,
            result.output,
            result.truncated,
            result.error_code,
            result.execution_id,
            "docker",
            result.image_digest,
            "cleaned",
        )

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
            runner = (
                (lambda command: self._sandbox_check(command, repo, linked_run_id, verification_id))
                if self.sandbox.enabled and self.sandbox.backend == "docker"
                else None
            )
            checks = await self.executor.run(plan, runner=runner)
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
