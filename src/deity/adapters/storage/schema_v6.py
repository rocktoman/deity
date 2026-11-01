"""Forward-only read-only delegation audit schema."""

import sqlite3
from pathlib import Path

import aiosqlite


def backup_v5(path: Path) -> None:
    """Create and verify a v5 snapshot before schema migration."""
    with sqlite3.connect(path) as source:
        version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version != 5:
            return
        backup_path = path.with_suffix(".v5.bak")
        if backup_path.exists():
            try:
                with sqlite3.connect(backup_path) as saved:
                    good = saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    saved_version = saved.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise ValueError("v5 backup verification failed") from exc
            if not good or saved_version != 5:
                raise ValueError("v5 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            good = target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            saved_version = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            if not good or saved_version != 5:
                raise ValueError("v5 backup verification failed")


async def migrate_v6(db: aiosqlite.Connection) -> None:
    """Add durable parent-child linkage without changing checkpoint lineage."""
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS delegations("
            "id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL REFERENCES runs(id), "
            "child_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id), "
            "root_run_id TEXT NOT NULL REFERENCES runs(id), depth INTEGER NOT NULL, "
            "contract_hash TEXT NOT NULL, contract_json TEXT NOT NULL, "
            "status TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, "
            "ended_at TEXT, UNIQUE(parent_run_id, id))"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delegations_parent ON delegations(parent_run_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_delegations_root ON delegations(root_run_id)"
        )
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (6, datetime('now'))")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
