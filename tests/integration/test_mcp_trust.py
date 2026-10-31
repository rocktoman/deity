import shutil
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest


@pytest.mark.asyncio
async def test_mcp_launch_trust_is_digest_scoped(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        assert not await repo.mcp_launch_trusted("demo", "digest-a")
        await repo.trust_mcp_launch("demo", "digest-a")
        assert await repo.mcp_launch_trusted("demo", "digest-a")
        assert not await repo.mcp_launch_trusted("demo", "digest-b")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_v2_database_backup_and_forward_migration(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "old task", "fake"))
    await repo.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE mcp_launch_trust")
        db.execute("DELETE FROM schema_migrations WHERE version=3")
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert not await repo.mcp_launch_trusted("demo", "digest")
    finally:
        await repo.close()
    backup = path.with_suffix(".v2.bak")
    assert backup.exists()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (2,)
        assert db.execute("SELECT id FROM runs").fetchone() == (run_id,)
    restored = tmp_path / "restored.db"
    shutil.copy2(backup, restored)
    reopened = await SQLiteSessionRepository.open(restored)
    try:
        assert (await reopened.run_info(run_id))["prompt"] == "old task"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_unknown_tool_outcome_is_persisted_without_visible_success(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "task", "fake"))
        assert await repo.claim_tool(run_id, "c1", "mcp:one:write", {})
        await repo.mark_tool_unknown(run_id, "c1", "transport_lost")
        assert (await repo.unresolved_tools(run_id))[0]["status"] == "unknown"
        assert "ToolCompleted" not in [e["type"] for e in await repo.events(run_id)]
    finally:
        await repo.close()
