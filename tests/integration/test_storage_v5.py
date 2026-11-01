import shutil
import sqlite3
from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, RunStatus
from deity.core.contracts.execution import (
    ArtifactEntry,
    ArtifactManifest,
    BackendKind,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    ResourceBudget,
    SandboxCleanupReport,
    SandboxSpec,
)


@pytest.mark.asyncio
async def test_v4_backup_and_v5_migration(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path) as db:
        for table in (
            "sandbox_cleanups",
            "sandbox_artifacts",
            "artifact_manifests",
            "sandbox_executions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_migrations WHERE version=5")
    repo = await SQLiteSessionRepository.open(path)
    try:
        async with repo.db.execute("SELECT MAX(version) FROM schema_migrations") as cursor:
            assert (await cursor.fetchone())[0] == 5
        async with repo.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert {
            "sandbox_executions",
            "artifact_manifests",
            "sandbox_artifacts",
            "sandbox_cleanups",
        } <= tables
    finally:
        await repo.close()
    with sqlite3.connect(path.with_suffix(".v4.bak")) as backup:
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (4,)


@pytest.mark.asyncio
async def test_v4_backup_restores_in_isolated_path(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path) as db:
        for table in (
            "sandbox_cleanups",
            "sandbox_artifacts",
            "artifact_manifests",
            "sandbox_executions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_migrations WHERE version=5")
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    restored = tmp_path / "isolated" / "restored-v4.db"
    restored.parent.mkdir()
    shutil.copy2(path.with_suffix(".v4.bak"), restored)
    with sqlite3.connect(restored) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (4,)
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='sandbox_executions'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_corrupt_v4_backup_blocks_migration(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    await repo.close()
    with sqlite3.connect(path) as db:
        for table in (
            "sandbox_cleanups",
            "sandbox_artifacts",
            "artifact_manifests",
            "sandbox_executions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_migrations WHERE version=5")
    path.with_suffix(".v4.bak").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ValueError):
        await SQLiteSessionRepository.open(path)


@pytest.mark.asyncio
async def test_orphaned_sandbox_intent_becomes_unknown(tmp_path: Path) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    run_id = await repo.create_run(AgentRequest("session", "task", str(tmp_path), "test", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    request = ExecutionRequest(
        "crashed-execution",
        run_id,
        "crashed-call",
        ("true",),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            "sha256:" + "a" * 64,
            ResourceBudget(5, 1.0, 1024, 2, 1000, 1024),
        ),
    )
    await repo.prepare_sandbox_execution(request)
    await repo.close()
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert await repo.status(run_id) == RunStatus.INTERRUPTED
        async with repo.db.execute(
            "SELECT status FROM sandbox_executions WHERE id=?", (request.execution_id,)
        ) as cursor:
            assert (await cursor.fetchone())[0] == "unknown"
        async with repo.db.execute(
            "SELECT status FROM sandbox_cleanups WHERE execution_id=?", (request.execution_id,)
        ) as cursor:
            assert (await cursor.fetchone())[0] == "unknown"
    finally:
        await repo.close()
    inspection = await RunService(db_path=path, workspace=tmp_path).inspect(run_id)
    assert inspection["decision"] == "manual_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare_apply", [False, True])
async def test_orphaned_artifact_apply_becomes_unknown(tmp_path: Path, prepare_apply: bool) -> None:
    path = tmp_path / "deity.db"
    repo = await SQLiteSessionRepository.open(path)
    run_id = await repo.create_run(AgentRequest("session", "task", str(tmp_path), "test", "fake"))
    await repo.set_status(run_id, RunStatus.RUNNING)
    request = ExecutionRequest(
        "artifact-execution",
        run_id,
        "artifact-call",
        ("true",),
        ".",
        SandboxSpec(
            BackendKind.DOCKER,
            "sha256:" + "a" * 64,
            ResourceBudget(5, 1.0, 1024, 2, 1000, 1024),
        ),
    )
    await repo.prepare_sandbox_execution(request)
    await repo.record_sandbox_result(
        run_id, ExecutionResult(request.execution_id, ExecutionStatus.SUCCEEDED, 0, "", 1)
    )
    await repo.record_sandbox_manifest(
        run_id,
        ArtifactManifest(
            request.execution_id,
            (ArtifactEntry("a.txt", "modify", "a" * 64, "b" * 64, 1),),
            "collected",
        ),
    )
    if prepare_apply:
        await repo.prepare_artifact_apply(run_id, request.execution_id, 1)
    await repo.close()
    repo = await SQLiteSessionRepository.open(path)
    try:
        assert await repo.status(run_id) == RunStatus.INTERRUPTED
        async with repo.db.execute(
            "SELECT a.status, m.status FROM sandbox_artifacts a "
            "JOIN artifact_manifests m ON m.id=a.manifest_id WHERE m.execution_id=?",
            (request.execution_id,),
        ) as cursor:
            assert tuple(await cursor.fetchone()) == ("unknown", "unknown")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_sandbox_intent_outcome_and_cleanup_are_ordered(tmp_path: Path) -> None:
    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        run_id = await repo.create_run(
            AgentRequest("session", "task", str(tmp_path), "test", "fake")
        )
        request = ExecutionRequest(
            "execution",
            run_id,
            "call",
            ("true",),
            ".",
            SandboxSpec(
                BackendKind.DOCKER,
                "sha256:" + "a" * 64,
                ResourceBudget(5, 1.0, 1024, 2, 1000, 1024),
            ),
        )
        await repo.prepare_sandbox_execution(request)
        await repo.record_sandbox_container(run_id, request.execution_id, "a" * 64)
        await repo.record_sandbox_result(
            run_id,
            ExecutionResult(
                request.execution_id,
                ExecutionStatus.SUCCEEDED,
                0,
                "ok",
                12,
            ),
        )
        await repo.record_sandbox_manifest(run_id, ArtifactManifest("execution", (), "collected"))
        await repo.record_sandbox_cleanup(
            run_id,
            SandboxCleanupReport(
                "execution",
                "cleaned",
                True,
                True,
            ),
        )
        events = [item["type"] for item in await repo.events(run_id)]
        assert events.index("SandboxExecutionPrepared") < events.index("SandboxStarted")
        assert events.index("SandboxStarted") < events.index("SandboxCompleted")
        assert events.index("SandboxCompleted") < events.index("SandboxCleanupCompleted")
    finally:
        await repo.close()
