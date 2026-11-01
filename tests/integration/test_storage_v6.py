import asyncio
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.delegation import DelegationBudget, DelegationRequest, SubAgentResult


@pytest.mark.asyncio
async def test_delegation_is_atomic_idempotent_and_bounded(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        parent = AgentRequest("s", "parent", str(tmp_path), "inspect", "fake", 5, 10)
        parent_id = await repo.create_run(parent)
        await repo.set_status(parent_id, RunStatus.RUNNING)
        contract = DelegationRequest(
            "d1", parent_id, "inspect files", ("read_file",), DelegationBudget(2, 3, 20)
        )
        child = AgentRequest("s", "child", str(tmp_path), "inspect files", "fake", 2, 3, 20)
        child_id = await repo.create_delegation(
            contract, child, max_depth=1, max_children=2, parent_deadline_seconds=900
        )
        assert child_id == await repo.create_delegation(
            contract, child, max_depth=1, max_children=2, parent_deadline_seconds=900
        )
        assert len(await repo.delegation_tree(parent_id)) == 1
        with pytest.raises(ValueError):
            await repo.create_delegation(
                DelegationRequest(
                    "d2", parent_id, "more", ("read_file",), DelegationBudget(4, 8, 20)
                ),
                AgentRequest("s", "child2", str(tmp_path), "more", "fake", 4, 8, 20),
                max_depth=1,
                max_children=2,
                parent_deadline_seconds=900,
            )
        await repo.set_status(child_id, RunStatus.RUNNING)
        await repo.set_status(child_id, RunStatus.COMPLETED)
        await repo.finish_delegation(SubAgentResult("d1", child_id, "completed", "ok"))
        assert (await repo.delegation_tree(parent_id))[0]["status"] == "completed"
        assert await repo.delegated_budget(parent_id) == (0, 0, 0, 0)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_v5_backup_exists_before_v6_and_can_restore(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE delegations")
        db.execute("DELETE FROM schema_migrations WHERE version=6")
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path.with_suffix(".v5.bak")) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (5,)
    restored = tmp_path / "isolated" / "restored-v5.db"
    restored.parent.mkdir()
    shutil.copy2(path.with_suffix(".v5.bak"), restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (5,)
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='delegations'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_restart_marks_orphaned_child_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    parent = AgentRequest("s", "p", str(tmp_path), "inspect", "fake", 6, 10)
    parent_id = await repo.create_run(parent)
    await repo.set_status(parent_id, RunStatus.RUNNING)
    contract = DelegationRequest(
        "d", parent_id, "inspect", ("read_file",), DelegationBudget(2, 2, 20)
    )
    child = AgentRequest("s", "c", str(tmp_path), "inspect", "fake", 2, 2, 20)
    child_id = await repo.create_delegation(
        contract, child, max_depth=1, max_children=2, parent_deadline_seconds=900
    )
    await repo.set_status(child_id, RunStatus.RUNNING)
    await repo.close()
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert await repo.status(child_id) == RunStatus.INTERRUPTED
        assert (await repo.delegation_tree(parent_id))[0]["status"] == "interrupted"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_restart_cancels_queued_child_after_parent_interrupt(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    parent_id = await repo.create_run(
        AgentRequest("s", "p", str(tmp_path), "inspect", "fake", 6, 10)
    )
    await repo.set_status(parent_id, RunStatus.RUNNING)
    contract = DelegationRequest(
        "queued", parent_id, "inspect", ("read_file",), DelegationBudget(2, 2, 20)
    )
    child_id = await repo.create_delegation(
        contract,
        AgentRequest("s", "c", str(tmp_path), "inspect", "fake", 2, 2, 20),
        max_depth=1,
        max_children=2,
        parent_deadline_seconds=900,
    )
    await repo.close()
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert await repo.status(parent_id) == RunStatus.INTERRUPTED
        assert await repo.status(child_id) == RunStatus.CANCELLED
        assert (await repo.delegation_tree(parent_id))[0]["status"] == "cancelled"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_invalid_child_evidence_is_rejected(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        parent_id = await repo.create_run(
            AgentRequest("s", "p", str(tmp_path), "inspect", "fake", 6, 10)
        )
        await repo.set_status(parent_id, RunStatus.RUNNING)
        contract = DelegationRequest(
            "d", parent_id, "inspect", ("read_file",), DelegationBudget(2, 2, 20)
        )
        child_id = await repo.create_delegation(
            contract,
            AgentRequest("s", "c", str(tmp_path), "inspect", "fake", 2, 2, 20),
            max_depth=1,
            max_children=2,
            parent_deadline_seconds=900,
        )
        await repo.set_status(child_id, RunStatus.RUNNING)
        await repo.set_status(child_id, RunStatus.COMPLETED)
        with pytest.raises(ValueError, match="invalid child evidence"):
            await repo.finish_delegation(
                SubAgentResult("d", child_id, "completed", "claim", ("fabricated",))
            )
        assert (await repo.delegation_tree(parent_id))[0]["status"] == "created"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_corrupt_v5_backup_blocks_migration(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE delegations")
        db.execute("DELETE FROM schema_migrations WHERE version=6")
    path.with_suffix(".v5.bak").write_text("corrupt")
    with pytest.raises(ValueError, match="v5 backup verification failed"):
        await SQLiteSessionRepository.open(path)


@pytest.mark.asyncio
async def test_concurrent_budget_reservations_allow_only_one_child(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    owner = await SQLiteSessionRepository.open(path)
    parent_id = await owner.create_run(
        AgentRequest("s", "p", str(tmp_path), "inspect", "fake", 3, 5)
    )
    await owner.set_status(parent_id, RunStatus.RUNNING)
    lease = await owner.acquire_lease(parent_id, f"{os.getpid()}:test")
    first = await SQLiteSessionRepository.open(path)
    second = await SQLiteSessionRepository.open(path)
    try:

        async def reserve(repo: SQLiteSessionRepository, ordinal: int) -> str:
            return await repo.create_delegation(
                DelegationRequest(
                    f"d{ordinal}",
                    parent_id,
                    "inspect",
                    ("read_file",),
                    DelegationBudget(2, 2, 20),
                ),
                AgentRequest("s", f"c{ordinal}", str(tmp_path), "inspect", "fake", 2, 2, 20),
                max_depth=1,
                max_children=2,
                parent_deadline_seconds=900,
            )

        results = await asyncio.gather(
            reserve(first, 1), reserve(second, 2), return_exceptions=True
        )
        assert sum(isinstance(result, str) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert len(await owner.delegation_tree(parent_id)) == 1
    finally:
        await first.close()
        await second.close()
        await owner.release_lease(lease)
        await owner.close()
