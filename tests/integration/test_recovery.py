"""Lease fencing and explicit successor-run recovery."""

from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, RunStatus, ToolResult


@pytest.mark.asyncio
async def test_live_owner_survives_second_repository_open(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    first = await SQLiteSessionRepository.open(path)
    run_id = await first.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    lease = await first.acquire_lease(run_id, "live-owner")
    await first.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    second = await SQLiteSessionRepository.open(path)
    assert await second.status(run_id) == RunStatus.RUNNING
    with pytest.raises(ValueError):
        await second.acquire_lease(run_id, "other-owner")
    await second.close()
    await first.close()


@pytest.mark.asyncio
async def test_interrupted_run_with_live_lease_cannot_be_resumed(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    lease = await repo.acquire_lease(run_id, "live-owner")
    await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    checkpoint = await repo.save_checkpoint(
        run_id, [{"role": "user", "content": "goal"}], {"turns": 0}, epoch=lease.epoch
    )
    await repo.set_status(run_id, RunStatus.INTERRUPTED, epoch=lease.epoch)
    assert await repo.has_active_owner(run_id)
    with pytest.raises(ValueError, match="may still have an owner"):
        await repo.create_resumed_run(run_id, checkpoint)
    await repo.release_lease(lease)
    assert not await repo.has_active_owner(run_id)
    await repo.close()


@pytest.mark.asyncio
async def test_stale_epoch_cannot_write_or_dispatch(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    lease = await repo.acquire_lease(run_id, "owner")
    await repo.set_status(run_id, RunStatus.RUNNING, epoch=lease.epoch)
    with pytest.raises(ValueError):
        await repo.claim_tool(run_id, "c1", "write_file", {}, epoch=lease.epoch + 1)
    with pytest.raises(ValueError):
        await repo.set_status(run_id, RunStatus.COMPLETED, epoch=lease.epoch + 1)
    with pytest.raises(ValueError):
        await repo.record_model_turn(run_id, "late", [], epoch=lease.epoch + 1)
    with pytest.raises(ValueError):
        await repo.record_tool(run_id, ToolResult("c1", "ok", "late"), epoch=lease.epoch + 1)
    await repo.close()


@pytest.mark.asyncio
async def test_resume_creates_new_run_without_reviving_interrupted(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    checkpoint = await repo.save_checkpoint(
        run_id, [{"role": "user", "content": "goal"}], {"turns": 0, "tool_calls": 0}
    )
    await repo.set_status(run_id, RunStatus.INTERRUPTED)
    new_run = await repo.create_resumed_run(run_id, checkpoint)
    assert new_run != run_id
    assert await repo.status(run_id) == RunStatus.INTERRUPTED
    assert await repo.status(new_run) == RunStatus.CREATED
    assert (await repo.latest_checkpoint(new_run)) is not None
    with pytest.raises(ValueError, match="already has a successor"):
        await repo.create_resumed_run(run_id, checkpoint)
    await repo.close()


@pytest.mark.asyncio
async def test_cancel_marks_pending_effect_unknown_in_same_terminal_transaction(
    tmp_path: Path,
) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    assert await repo.claim_tool(run_id, "shell-1", "shell", {"argv": ["sleep", "10"]})
    await repo.set_status(run_id, RunStatus.CANCELLED)
    assert await repo.status(run_id) == RunStatus.CANCELLED
    assert (await repo.unresolved_tools(run_id))[0]["status"] == "unknown"
    events = await repo.events(run_id)
    assert [event["type"] for event in events].count("ToolOutcomeUnknown") == 1
    assert [event["type"] for event in events].count("RunStatusChanged") == 2
    await repo.close()
