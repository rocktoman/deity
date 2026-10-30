"""Application-level explicit recovery gates."""

import asyncio
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, RunStatus, ToolResult


class FinalModel:
    async def stream(self, request: ModelRequest):
        yield ModelEvent("text_delta", text="resumed")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_resume_uses_successor_run_and_rejects_workspace_drift(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("value = 1\n")
    db = tmp_path.parent / "resume.db"
    repo = await SQLiteSessionRepository.open(db)
    parent = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(parent, RunStatus.RUNNING)
    await repo.save_checkpoint(
        parent,
        [{"role": "user", "content": "goal"}],
        {"turns": 0, "tool_calls": 0, "workspace_fingerprint": workspace_digest(tmp_path)},
    )
    await repo.set_status(parent, RunStatus.INTERRUPTED)
    await repo.close()

    service = RunService(
        db_path=db,
        workspace=tmp_path,
        model=FinalModel(),
        limits=RunConfig(resume_enabled=True),
    )
    inspection = await service.inspect(parent)
    assert inspection["decision"] == "resume"
    (tmp_path / "code.py").write_text("value = 2\n")
    assert (await service.inspect(parent))["decision"] == "abort"
    with pytest.raises(ValueError):
        await service.resume(parent)
    (tmp_path / "code.py").write_text("value = 1\n")
    result = await service.resume(parent)
    assert result.final_text == "resumed" and result.run_id != parent
    repo = await SQLiteSessionRepository.open(db)
    assert await repo.status(parent) == RunStatus.INTERRUPTED
    assert await repo.status(result.run_id) == RunStatus.COMPLETED
    await repo.close()


@pytest.mark.asyncio
async def test_run_service_cancel_propagates_to_model_and_storage(tmp_path: Path) -> None:
    started = asyncio.Event()

    class WaitingModel:
        async def stream(self, request: ModelRequest):
            started.set()
            await asyncio.Future()
            yield ModelEvent("message_completed")

    db = tmp_path / "deity.db"
    service = RunService(db_path=db, workspace=tmp_path, model=WaitingModel())
    task = asyncio.create_task(service.run("goal", model_name="fake"))
    await started.wait()
    repo = await SQLiteSessionRepository.open(db)
    async with repo.db.execute("SELECT id FROM runs") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    run_id = row[0]
    assert await repo.status(run_id) == RunStatus.RUNNING
    await repo.close()
    assert service.cancel(run_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    repo = await SQLiteSessionRepository.open(db)
    assert await repo.status(run_id) == RunStatus.CANCELLED
    await repo.close()


@pytest.mark.asyncio
async def test_completed_effect_after_checkpoint_requires_review(tmp_path: Path) -> None:
    db = tmp_path.parent / "uncheckpointed.db"
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    await repo.save_checkpoint(
        run_id,
        [{"role": "user", "content": "goal"}],
        {"turns": 0, "tool_calls": 0, "workspace_fingerprint": workspace_digest(tmp_path)},
    )
    assert await repo.claim_tool(run_id, "c1", "write_file", {"path": "x"})
    await repo.record_tool(run_id, ToolResult("c1", "ok", "created"))
    await repo.set_status(run_id, RunStatus.INTERRUPTED)
    await repo.close()
    service = RunService(db_path=db, workspace=tmp_path, limits=RunConfig(resume_enabled=True))
    assert (await service.inspect(run_id))["decision"] == "manual_review"
    with pytest.raises(ValueError):
        await service.resume(run_id)


@pytest.mark.asyncio
async def test_resume_refuses_exhausted_cumulative_deadline(tmp_path: Path) -> None:
    db = tmp_path.parent / "deadline.db"
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    await repo.save_checkpoint(
        run_id,
        [{"role": "user", "content": "goal"}],
        {
            "turns": 0,
            "tool_calls": 0,
            "elapsed_seconds": 10.0,
            "workspace_fingerprint": workspace_digest(tmp_path),
        },
    )
    await repo.set_status(run_id, RunStatus.INTERRUPTED)
    await repo.close()
    service = RunService(db_path=db, workspace=tmp_path, limits=RunConfig(deadline_seconds=10))
    assert (await service.inspect(run_id))["reason"] == "deadline_exhausted"


@pytest.mark.asyncio
async def test_resume_refuses_changed_recovery_configuration(tmp_path: Path) -> None:
    db = tmp_path.parent / "config-change.db"
    original = AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    repo = await SQLiteSessionRepository.open(db)
    run_id = await repo.create_run(original)
    await repo.set_status(run_id, RunStatus.RUNNING)
    await repo.save_checkpoint(
        run_id,
        [{"role": "user", "content": "goal"}],
        {
            "turns": 0,
            "tool_calls": 0,
            "workspace_fingerprint": workspace_digest(tmp_path),
            "config_fingerprint": original.config_fingerprint(),
        },
    )
    await repo.set_status(run_id, RunStatus.INTERRUPTED)
    await repo.close()
    changed = RunService(db_path=db, workspace=tmp_path, limits=RunConfig(max_turns=31))
    assert (await changed.inspect(run_id))["reason"] == "config_changed"
