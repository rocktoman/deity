"""ADR-020 A6：会话自动标题、归档、分叉独立演进与 v7 迁移。"""

import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.adapters.tools.workspace import workspace_digest
from deity.application.config import RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, RunStatus


@pytest.mark.asyncio
async def test_auto_title_archive_and_unarchive(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        await repo.create_run(
            AgentRequest("s1", "t1", str(tmp_path), "修复登录页崩溃\n详细描述", "fake")
        )
        sessions = await repo.sessions()
        assert sessions[0]["title"] == "修复登录页崩溃"
        await repo.archive_session("s1")
        assert await repo.sessions() == []
        archived = await repo.sessions(include_archived=True)
        assert archived[0]["archived"] == "1"
        await repo.archive_session("s1", archived=False)
        assert (await repo.sessions())[0]["archived"] == "0"
        with pytest.raises(KeyError):
            await repo.archive_session("missing")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_schema_v7_migration_from_v6_with_backup(tmp_path: Path) -> None:
    db = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(db)
    await repo.close()
    # 把库回退成 v6 形态：删 v7 迁移记录与 archived 列，再重开应自动迁移。
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=7")
        connection.execute("ALTER TABLE sessions DROP COLUMN archived")
        connection.commit()
    repo = await SQLiteSessionRepository.open(db)
    try:
        async with repo.db.execute("SELECT MAX(version) FROM schema_migrations") as cursor:
            assert (await cursor.fetchone())[0] == 7
        async with repo.db.execute("PRAGMA table_info(sessions)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        assert "archived" in columns
    finally:
        await repo.close()
    backup = db.with_suffix(".v6.bak")
    assert backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6


@pytest.mark.asyncio
async def test_fork_twice_and_independent_evolution(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        parent = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
        await repo.set_status(parent, RunStatus.RUNNING)
        await repo.save_checkpoint(
            parent,
            [{"role": "user", "content": "goal"}],
            {"turns": 0, "tool_calls": 0, "workspace_fingerprint": workspace_digest(tmp_path)},
        )
        await repo.set_status(parent, RunStatus.COMPLETED)
        fork_a = await repo.create_forked_run(parent)
        fork_b = await repo.create_forked_run(parent)
        assert fork_a != fork_b
        assert await repo.status(fork_a) == RunStatus.INTERRUPTED
        messages_a = await repo.context_messages(fork_a)
        messages_b = await repo.context_messages(fork_b)
        assert messages_a == messages_b == [{"role": "user", "content": "goal"}]
        async with repo.db.execute(
            "SELECT decision FROM run_lineage WHERE new_run_id=?", (fork_a,)
        ) as cursor:
            assert (await cursor.fetchone())[0] == "fork"
        # 独立演进：在分叉 A 追加一轮，分叉 B 与父 run 都不受影响。
        await repo.record_model_turn(fork_a, "branch A continues", [])
        assert len(await repo.context_messages(fork_a)) == 2
        assert len(await repo.context_messages(fork_b)) == 1
        assert len(await repo.context_messages(parent)) == 1
        with pytest.raises(KeyError):
            await repo.create_forked_run("missing-run")
    finally:
        await repo.close()


class _FinalModel:
    async def stream(self, request: ModelRequest):
        yield ModelEvent("text_delta", text="forked and resumed")
        yield ModelEvent("message_completed")


@pytest.mark.asyncio
async def test_forked_run_resumes_through_run_service(tmp_path: Path) -> None:
    # DB 放工作区外：分叉写库不应改变 workspace 指纹（与 resume 既有测试同款）。
    db = tmp_path.parent / "fork-resume.db"
    repo = await SQLiteSessionRepository.open(db)
    parent = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.set_status(parent, RunStatus.RUNNING)
    await repo.save_checkpoint(
        parent,
        [{"role": "user", "content": "goal"}],
        {"turns": 0, "tool_calls": 0, "workspace_fingerprint": workspace_digest(tmp_path)},
    )
    await repo.set_status(parent, RunStatus.COMPLETED)
    fork = await repo.create_forked_run(parent)
    await repo.close()

    service = RunService(
        db_path=db, workspace=tmp_path, model=_FinalModel(), limits=RunConfig(resume_enabled=True)
    )
    result = await service.resume(fork)
    assert result.final_text == "forked and resumed"
    repo = await SQLiteSessionRepository.open(db)
    try:
        assert await repo.status(result.run_id) == RunStatus.COMPLETED
        assert await repo.status(parent) == RunStatus.COMPLETED
    finally:
        await repo.close()
