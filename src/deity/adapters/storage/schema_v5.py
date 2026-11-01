"""Forward-only sandbox audit schema."""

import sqlite3
from pathlib import Path

import aiosqlite


def backup_v4(path: Path) -> None:
    """Create and verify a v4 backup before migrating the source."""
    with sqlite3.connect(path) as source:
        version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version != 4:
            return
        backup_path = path.with_suffix(".v4.bak")
        if backup_path.exists():
            try:
                with sqlite3.connect(backup_path) as saved:
                    saved_version = saved.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise ValueError("v4 backup verification failed") from exc
            if saved_version != 4:
                raise ValueError("v4 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            if target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 4:
                raise ValueError("v4 backup verification failed")


async def migrate_v5(db: aiosqlite.Connection) -> None:
    """Persist sandbox intent, outcomes, artifacts and cleanup separately."""
    statements = (
        (
            "CREATE TABLE IF NOT EXISTS sandbox_executions("
            "id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT, "
            "backend TEXT NOT NULL, status TEXT NOT NULL, spec_json TEXT NOT NULL, "
            "capabilities_json TEXT NOT NULL, image_digest TEXT, container_id TEXT, "
            "started_at TEXT NOT NULL, ended_at TEXT, exit_code INTEGER, error_code TEXT, "
            "result_json TEXT, UNIQUE(run_id, call_id))"
        ),
        (
            "CREATE TABLE IF NOT EXISTS artifact_manifests("
            "id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE REFERENCES sandbox_executions(id), "
            "status TEXT NOT NULL, summary_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS sandbox_artifacts("
            "id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id), "
            "ordinal INTEGER NOT NULL, path TEXT NOT NULL, operation TEXT NOT NULL, "
            "before_sha256 TEXT, after_sha256 TEXT, size_bytes INTEGER, status TEXT NOT NULL, "
            "UNIQUE(manifest_id, ordinal))"
        ),
        (
            "CREATE TABLE IF NOT EXISTS sandbox_cleanups("
            "id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE REFERENCES sandbox_executions(id), "
            "status TEXT NOT NULL, container_removed INTEGER NOT NULL, staging_removed INTEGER NOT NULL, "
            "diagnostics_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
    )
    await db.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            await db.execute(statement)
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (5, datetime('now'))")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
