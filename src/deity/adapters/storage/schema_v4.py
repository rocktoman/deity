"""Forward-only SQLite v4 migration and verified v3 backup."""

import sqlite3
from pathlib import Path

import aiosqlite


def backup_v3(path: Path) -> None:
    """Create or verify a v3 backup before mutating the source database."""
    with sqlite3.connect(path) as source:
        current = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if current != 3:
            return
        backup_path = path.with_suffix(".v3.bak")
        if backup_path.exists():
            try:
                with sqlite3.connect(backup_path) as saved:
                    saved_version = saved.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise ValueError("v3 backup verification failed") from exc
            if saved_version != 3:
                raise ValueError("v3 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            version = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version != 3:
                raise ValueError("v3 backup verification failed")


async def migrate_v4(db: aiosqlite.Connection) -> None:
    """Add dedicated V0.5 facts in one transaction."""
    statements = (
        (
            "CREATE TABLE IF NOT EXISTS workspace_snapshots("
            "id TEXT PRIMARY KEY, workspace_root TEXT NOT NULL, git_head TEXT, "
            "status_json TEXT NOT NULL, digest TEXT NOT NULL, backend_version TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS plans("
            "id TEXT PRIMARY KEY, source_run_id TEXT NOT NULL REFERENCES runs(id), "
            "session_id TEXT NOT NULL REFERENCES sessions(id), status TEXT NOT NULL, "
            "schema_version INTEGER NOT NULL, artifact_json TEXT NOT NULL, sha256 TEXT NOT NULL, "
            "requirements_sha256 TEXT NOT NULL, workspace_snapshot_id TEXT NOT NULL "
            "REFERENCES workspace_snapshots(id), created_at TEXT NOT NULL, approved_at TEXT)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS plan_builds("
            "plan_id TEXT PRIMARY KEY REFERENCES plans(id), build_run_id TEXT NOT NULL UNIQUE "
            "REFERENCES runs(id), approval_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS change_sets("
            "id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), "
            "baseline_snapshot_id TEXT NOT NULL REFERENCES workspace_snapshots(id), "
            "status TEXT NOT NULL, summary_json TEXT NOT NULL, created_at TEXT NOT NULL, "
            "completed_at TEXT)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS file_changes("
            "id TEXT PRIMARY KEY, change_set_id TEXT NOT NULL REFERENCES change_sets(id), "
            "ordinal INTEGER NOT NULL, path TEXT NOT NULL, operation TEXT NOT NULL, "
            "before_sha256 TEXT, after_sha256 TEXT, source_call_id TEXT NOT NULL, "
            "status TEXT NOT NULL, UNIQUE(change_set_id, ordinal))"
        ),
        (
            "CREATE TABLE IF NOT EXISTS verification_runs("
            "id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id), workspace_snapshot_id TEXT "
            "REFERENCES workspace_snapshots(id), status TEXT NOT NULL, plan_json TEXT NOT NULL, "
            "started_at TEXT NOT NULL, ended_at TEXT)"
        ),
        (
            "CREATE TABLE IF NOT EXISTS verification_checks("
            "id TEXT PRIMARY KEY, verification_id TEXT NOT NULL REFERENCES verification_runs(id), "
            "ordinal INTEGER NOT NULL, kind TEXT NOT NULL, argv_json TEXT NOT NULL, "
            "status TEXT NOT NULL, exit_code INTEGER, evidence_id TEXT REFERENCES evidence(id), "
            "duration_ms INTEGER NOT NULL, UNIQUE(verification_id, ordinal))"
        ),
    )
    await db.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            await db.execute(statement)
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (4, datetime('now'))")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
