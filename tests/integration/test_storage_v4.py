import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SCHEMA, SQLiteSessionRepository


def _create_v3(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO schema_migrations VALUES (1, '2026-01-01')")
        db.execute("INSERT INTO schema_migrations VALUES (2, '2026-01-02')")
        db.execute("INSERT INTO schema_migrations VALUES (3, '2026-01-03')")
        db.execute(
            "CREATE TABLE mcp_launch_trust("
            "server_name TEXT PRIMARY KEY, digest TEXT NOT NULL, approved_at TEXT NOT NULL)"
        )


@pytest.mark.asyncio
async def test_v3_is_backed_up_and_migrated_to_v4(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    _create_v3(path)
    repo = await SQLiteSessionRepository.open(path)
    try:
        async with repo.db.execute("SELECT MAX(version) FROM schema_migrations") as cursor:
            row = await cursor.fetchone()
            assert row is not None and row[0] == 4
        async with repo.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert {
            "workspace_snapshots",
            "plans",
            "plan_builds",
            "change_sets",
            "file_changes",
            "verification_runs",
            "verification_checks",
        } <= tables
    finally:
        await repo.close()

    backup = path.with_suffix(".v3.bak")
    assert backup.exists()
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (3,)


@pytest.mark.asyncio
async def test_invalid_existing_v3_backup_blocks_migration(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    _create_v3(path)
    backup = path.with_suffix(".v3.bak")
    backup.write_text("not a database", encoding="utf-8")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        await SQLiteSessionRepository.open(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (3,)
