"""Session metadata schema v7: archive flag for sessions (ADR-020 A6)."""

import sqlite3
from pathlib import Path

import aiosqlite


def backup_v6(path: Path) -> None:
    """Create and verify a v6 snapshot before schema migration."""
    with sqlite3.connect(path) as source:
        version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version != 6:
            return
        backup_path = path.with_suffix(".v6.bak")
        if backup_path.exists():
            try:
                with sqlite3.connect(backup_path) as saved:
                    good = saved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    saved_version = saved.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise ValueError("v6 backup verification failed") from exc
            if not good or saved_version != 6:
                raise ValueError("v6 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            good = target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            saved_version = target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            if not good or saved_version != 6:
                raise ValueError("v6 backup verification failed")


async def migrate_v7(db: aiosqlite.Connection) -> None:
    """Add sessions.archived and let fork lineage have many children.

    The v2-era unique index one_successor_per_run covered every lineage
    kind; ADR-020 A6 forks need many children per parent, so the index
    becomes partial: uniqueness only for decision='resume' rows.
    """
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("PRAGMA table_info(sessions)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "archived" not in columns:
            await db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        await db.execute("DROP INDEX IF EXISTS one_successor_per_run")
        await db.execute(
            "CREATE UNIQUE INDEX one_successor_per_run "
            "ON run_lineage(parent_run_id) WHERE decision='resume'"
        )
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (7, datetime('now'))")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
