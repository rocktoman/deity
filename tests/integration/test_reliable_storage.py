"""V0.3 durable facts and migration gates."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts import AgentRequest, ToolCall, ToolResult


@pytest.mark.asyncio
async def test_v2_records_model_visible_tool_outcome_atomically(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "task", "fake"))
    await repo.record_model_turn(run_id, "", [ToolCall("c1", "read_file", {"path": "a"})])
    assert await repo.claim_tool(run_id, "c1", "read_file", {"path": "a"})
    await repo.record_tool(run_id, ToolResult("c1", "ok", "file contents"))
    await repo.close()

    repo = await SQLiteSessionRepository.open(path)
    messages = await repo.context_messages(run_id)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool"]
    assert messages[-1]["content"] == "file contents"
    assert messages[-1]["call_id"] == "c1"
    assert any(e["type"] == "ToolCompleted" for e in await repo.events(run_id))
    await repo.close()


@pytest.mark.asyncio
async def test_v1_migration_keeps_history_and_creates_backup(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    db.execute("INSERT INTO schema_migrations VALUES (1, '2026-01-01')")
    db.commit()
    db.close()

    repo = await SQLiteSessionRepository.open(path)
    async with repo.db.execute("SELECT MAX(version) FROM schema_migrations") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 2
    assert path.with_suffix(".v1.bak").exists()
    assert sqlite3.connect(path.with_suffix(".v1.bak")).execute(
        "SELECT version FROM schema_migrations"
    ).fetchone() == (1,)
    await repo.close()


@pytest.mark.asyncio
async def test_failed_outcome_has_no_message_or_event(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "task", "fake"))
    assert await repo.claim_tool(run_id, "c1", "read_file", {})
    before = await repo.context_messages(run_id)
    with pytest.raises(ValueError):
        await repo.record_tool(run_id, ToolResult("wrong", "ok", "bad"))
    assert await repo.context_messages(run_id) == before
    assert "ToolCompleted" not in [e["type"] for e in await repo.events(run_id)]
    async with repo.db.execute("SELECT status FROM tool_executions WHERE call_id='c1'") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == "pending"
    await repo.close()


@pytest.mark.asyncio
async def test_secret_is_redacted_before_persisting_visible_outcome(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "task", "fake"))
    await repo.record_model_turn(run_id, "", [ToolCall("c1", "read_file", {})])
    assert await repo.claim_tool(run_id, "c1", "read_file", {})
    await repo.record_tool(run_id, ToolResult("c1", "ok", "token=secret-marker"))
    messages = await repo.context_messages(run_id)
    assert "secret-marker" not in json.dumps(messages)
    await repo.close()


@pytest.mark.asyncio
async def test_usage_counts_survive_redaction(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    await repo.record_usage(run_id, 123, 45)
    usage = [
        event["payload"] for event in await repo.events(run_id) if event["type"] == "ModelUsage"
    ]
    assert usage == [{"schema_version": 1, "input_tokens": 123, "output_tokens": 45}]
    await repo.close()


@pytest.mark.asyncio
async def test_artifact_reference_reads_only_committed_redacted_tool_body(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    run_id = await repo.create_run(AgentRequest("s", "t", str(tmp_path), "goal", "fake"))
    assert await repo.claim_tool(run_id, "c1", "read_file", {"path": "a"})
    await repo.record_tool(run_id, ToolResult("c1", "ok", "x" * 1000))
    artifact_id = hashlib.sha256(("x" * 1000).encode()).hexdigest()
    assert await repo.artifact_content(run_id, artifact_id) == "x" * 1000
    with pytest.raises(KeyError):
        await repo.artifact_content(run_id, "not-committed")
    await repo.close()
