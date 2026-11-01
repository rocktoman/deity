import asyncio
from pathlib import Path

import pytest

from deity.adapters.execution.fake import FakeExecutionBackend
from deity.adapters.storage import SQLiteSessionRepository
from deity.application.sandbox_service import SandboxService, SandboxUnavailable
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.execution import (
    ArtifactEntry,
    BackendKind,
    ExecutionRequest,
    ExecutionStatus,
    ResourceBudget,
    SandboxSpec,
)


def request(run_id: str) -> ExecutionRequest:
    return ExecutionRequest(
        "fake-execution",
        run_id,
        "fake-call",
        ("true",),
        ".",
        SandboxSpec(
            BackendKind.FAKE,
            None,
            ResourceBudget(2, 1, 1024, 2, 1000, 1024),
        ),
    )


@pytest.mark.asyncio
async def test_fake_lifecycle_and_audit_order(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        await repo.set_status(run_id, RunStatus.RUNNING)
        backend = FakeExecutionBackend()
        result, manifest = await SandboxService(backend, repo).run(request(run_id))
        assert result.exit_code == 0
        assert not manifest.entries
        assert backend.calls == [
            "capabilities",
            "preflight",
            "prepare",
            "execute",
            "collect",
            "cleanup",
        ]
        events = [event["type"] for event in await repo.events(run_id)]
        assert events.index("SandboxExecutionPrepared") < events.index("SandboxCleanupCompleted")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_fake_capability_rejection_has_no_intent(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        backend = FakeExecutionBackend(available=False)
        with pytest.raises(SandboxUnavailable):
            await SandboxService(backend, repo).run(request(run_id))
        assert backend.calls == ["capabilities"]
        assert not any(
            event["type"] == "SandboxExecutionPrepared" for event in await repo.events(run_id)
        )
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_fake_preflight_failure_has_no_intent(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        backend = FakeExecutionBackend(fail_at="preflight")
        with pytest.raises(RuntimeError, match="preflight"):
            await SandboxService(backend, repo).run(request(run_id))
        assert backend.calls == ["capabilities", "preflight"]
        assert not any(
            event["type"] == "SandboxExecutionPrepared" for event in await repo.events(run_id)
        )
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cleanup_failure_is_durable(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        await repo.set_status(run_id, RunStatus.RUNNING)
        backend = FakeExecutionBackend(fail_at="cleanup")
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await SandboxService(backend, repo).run(request(run_id))
        assert backend.calls[-1] == "cleanup"
        assert any(event["type"] == "SandboxCleanupFailed" for event in await repo.events(run_id))
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_unknown_result_is_not_collected_or_reviewed(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        await repo.set_status(run_id, RunStatus.RUNNING)
        backend = FakeExecutionBackend(result_status=ExecutionStatus.UNKNOWN)

        async def forbidden_review(_handle: object, _manifest: object) -> None:
            raise AssertionError("unknown result must not review artifacts")

        result, manifest = await SandboxService(backend, repo, reviewer=forbidden_review).run(
            request(run_id)
        )
        assert result.status == ExecutionStatus.UNKNOWN
        assert manifest.status == "unknown"
        assert backend.calls == ["capabilities", "preflight", "prepare", "execute", "cleanup"]
    finally:
        await repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ExecutionStatus.UNKNOWN, ExecutionStatus.TIMED_OUT])
async def test_uncertain_result_never_scans_live_staging(
    tmp_path: Path, status: ExecutionStatus
) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / f"{status.value}.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        await repo.set_status(run_id, RunStatus.RUNNING)
        backend = FakeExecutionBackend(result_status=status)
        result, manifest = await SandboxService(backend, repo).run(request(run_id))
        assert result.status == status
        assert manifest.status == "unknown"
        assert "collect" not in backend.calls
        assert backend.calls[-1] == "cleanup"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cancelled_execution_records_unknown_and_cleanup(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "request", str(tmp_path), "test", "fake")
        )
        await repo.set_status(run_id, RunStatus.RUNNING)
        started = asyncio.Event()

        class BlockingBackend(FakeExecutionBackend):
            async def execute(self, handle, request):
                self._step("execute")
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        backend = BlockingBackend()
        task = asyncio.create_task(SandboxService(backend, repo).run(request(run_id)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.calls[-1] == "cleanup"
        events = [event["type"] for event in await repo.events(run_id)]
        assert "SandboxOutcomeUnknown" in events
        assert "SandboxCleanupCompleted" in events
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_collect_and_review_failures_leave_unknown_manifest(tmp_path: Path) -> None:
    for fail_at in ("collect", "review"):
        repo = await SQLiteSessionRepository.open(tmp_path / f"{fail_at}.db")
        try:
            run_id = await repo.create_run(
                AgentRequest("session", "request", str(tmp_path), "test", "fake")
            )
            await repo.set_status(run_id, RunStatus.RUNNING)
            backend = FakeExecutionBackend(
                fail_at=fail_at if fail_at == "collect" else None,
                manifest_entries=(ArtifactEntry("a.txt", "create", None, "a" * 64, 1),),
            )

            async def review(_handle, _manifest):
                raise RuntimeError("fake review failed")

            with pytest.raises(RuntimeError):
                await SandboxService(backend, repo, reviewer=review).run(request(run_id))
            assert backend.calls[-1] == "cleanup"
            async with repo.db.execute(
                "SELECT status FROM artifact_manifests WHERE execution_id='fake-execution'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == "unknown"
        finally:
            await repo.close()
