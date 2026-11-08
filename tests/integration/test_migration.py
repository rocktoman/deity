"""V1.0 S1：V0.7 时代 SQLite 库升级后可读、事件可查。

fixture tests/fixtures/legacy-v07.db 由当前仓库代码在 schema v6（V0.7 时代
最新迁移）下生成：1 个已完成 Run、6 条 schema_version=1 事件；1.0.0 不新增
schema 迁移，打开后应原样可读。
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import RunStatus

FIXTURE = Path(__file__).parents[1] / "fixtures" / "legacy-v07.db"
RUN_ID = "legacy-run-0001"


def test_legacy_fixture_is_v07_schema() -> None:
    assert FIXTURE.exists()
    with sqlite3.connect(FIXTURE) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6
        payloads = [
            row[0] for row in db.execute("SELECT payload_json FROM events ORDER BY sequence")
        ]
    assert all('"schema_version": 1' in payload for payload in payloads)


@pytest.mark.asyncio
async def test_legacy_db_opens_and_events_readable(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v07.db"
    shutil.copy(FIXTURE, db_path)
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        assert await repo.status(RUN_ID) == RunStatus.COMPLETED
        events = await repo.events(RUN_ID)
        assert [event["type"] for event in events] == [
            "RunCreated",
            "RunStatusChanged",
            "ModelTurnCompleted",
            "ToolClaimed",
            "ToolCompleted",
            "RunStatusChanged",
        ]
        assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5, 6]
        assert all(event["payload"].get("schema_version", 1) == 1 for event in events)
        info = await repo.run_info(RUN_ID)
        assert info["status"] == "completed"
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_legacy_db_open_does_not_rewrite_old_events(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v07.db"
    shutil.copy(FIXTURE, db_path)
    before = (
        sqlite3.connect(db_path)
        .execute("SELECT payload_json FROM events ORDER BY sequence")
        .fetchall()
    )
    repo = await SQLiteSessionRepository.open(db_path)
    try:
        await repo.events(RUN_ID)
    finally:
        await repo.close()
    after = (
        sqlite3.connect(db_path)
        .execute("SELECT payload_json FROM events ORDER BY sequence")
        .fetchall()
    )
    assert before == after
