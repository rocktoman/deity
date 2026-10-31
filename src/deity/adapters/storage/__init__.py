"""SQLite-backed sessions, runs, tool audit and events."""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from deity.core.context.reliable import canonical_json, normalize_unicode
from deity.core.contracts import AgentRequest, RunLease, RunStatus, ToolCall, ToolResult
from deity.core.state import transition

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, workspace_root TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, title TEXT);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), prompt TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), status TEXT NOT NULL, model_provider TEXT NOT NULL, model_name TEXT NOT NULL, started_at TEXT, ended_at TEXT, error_code TEXT, max_turns INTEGER NOT NULL, max_tool_calls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS steps(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), ordinal INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, UNIQUE(run_id, ordinal));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(run_id, sequence));
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), run_id TEXT REFERENCES runs(id), role TEXT NOT NULL, content_json TEXT NOT NULL, ordinal INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tool_executions(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL, tool_name TEXT NOT NULL, args_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, approval TEXT, UNIQUE(run_id, call_id));
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, source_tool_call_id TEXT, summary TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, sequence);
"""


def _redact(value: Any) -> Any:
    """Remove credential-shaped fields before storing model-visible facts."""
    if isinstance(value, dict):
        return {
            key: (
                _redact(item)
                if key in {"input_tokens", "output_tokens", "cache_tokens"}
                and (item is None or type(item) is int)
                else "[redacted]"
                if any(word in key.lower() for word in ("key", "token", "secret", "password"))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r"(?i)\b(api_key|token|secret|password)\s*[:=]\s*[^\s,;]+",
            r"\1=[redacted]",
            normalize_unicode(value),
        )
    return value


def _backup_v1(path: Path) -> None:
    """Save an existing v1 database before the forward-only migration."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with sqlite3.connect(path) as source:
        version = source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if version is None:
            return
        current = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if current != 1:
            return
        backup_path = path.with_suffix(".v1.bak")
        if backup_path.exists():
            with sqlite3.connect(backup_path) as saved:
                if saved.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 1:
                    raise ValueError("v1 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            if target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 1:
                raise ValueError("v1 backup verification failed")


def _backup_v2(path: Path) -> None:
    """Keep a verified v2 snapshot before adding extension trust storage."""
    with sqlite3.connect(path) as source:
        current = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if current != 2:
            return
        backup_path = path.with_suffix(".v2.bak")
        if backup_path.exists():
            with sqlite3.connect(backup_path) as saved:
                if saved.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 2:
                    raise ValueError("v2 backup verification failed")
            return
        with sqlite3.connect(backup_path) as target:
            source.backup(target)
            if target.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 2:
                raise ValueError("v2 backup verification failed")


def _owner_alive(owner_id: str) -> bool:
    """Treat unknown owner formats as alive to avoid unsafe takeover."""
    try:
        pid = int(owner_id.split(":", 1)[0])
    except ValueError:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SQLiteSessionRepository:
    """Serialize SQLite writes and publish only committed audit facts."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        publisher: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.db = db
        self._lock = asyncio.Lock()
        self._publisher = publisher

    @classmethod
    async def open(
        cls,
        path: Path,
        publisher: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> "SQLiteSessionRepository":
        """Open storage, migrate schema, and mark orphaned runs interrupted."""
        existing = path.exists() and path.stat().st_size > 0
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup_v1(path)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(SCHEMA)
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, datetime('now'))")
        await db.commit()
        repo = cls(db, publisher)
        await repo._migrate_v2()
        if existing:
            _backup_v2(path)
        await repo._migrate_v3()
        async with repo._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT runs.id, run_leases.owner_id, run_leases.expires_at "
                    "FROM runs LEFT JOIN run_leases ON runs.id=run_leases.run_id "
                    "WHERE runs.status IN ('running', 'waiting_approval')"
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    if row["owner_id"] is not None and _owner_alive(row["owner_id"]):
                        continue
                    await db.execute(
                        "UPDATE runs SET status='interrupted', ended_at=datetime('now') WHERE id=?",
                        (row["id"],),
                    )
                    await db.execute(
                        "UPDATE tool_executions SET status='unknown' "
                        "WHERE run_id=? AND status='pending'",
                        (row["id"],),
                    )
                    await repo._event(row["id"], "RunInterrupted", {})
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return repo

    async def _assert_epoch(self, run_id: str, epoch: int | None) -> None:
        async with self.db.execute(
            "SELECT epoch, expires_at FROM run_leases WHERE run_id=?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None and (epoch != row[0] or row[1] <= time.time()):
            raise ValueError("stale run owner")

    async def acquire_lease(self, run_id: str, owner_id: str) -> RunLease:
        """Acquire a fenced owner only while no live owner exists."""
        now = time.time()
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT owner_id, epoch, expires_at FROM run_leases WHERE run_id=?",
                    (run_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None and (row[2] > now or _owner_alive(row[0])):
                    raise ValueError("run already has an owner")
                epoch = row[1] + 1 if row is not None else 1
                await self.db.execute(
                    "INSERT INTO run_leases VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET owner_id=excluded.owner_id, "
                    "epoch=excluded.epoch, expires_at=excluded.expires_at, "
                    "heartbeat_at=excluded.heartbeat_at",
                    (run_id, owner_id, epoch, now + 30, now),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return RunLease(run_id, owner_id, epoch, now + 30)

    async def renew_lease(self, lease: RunLease) -> RunLease:
        """Extend a lease only when its owner and epoch still match."""
        now = time.time()
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "UPDATE run_leases SET expires_at=?, heartbeat_at=? "
                    "WHERE run_id=? AND owner_id=? AND epoch=? AND expires_at>?",
                    (now + 30, now, lease.run_id, lease.owner_id, lease.epoch, now),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale run owner")
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return RunLease(lease.run_id, lease.owner_id, lease.epoch, now + 30)

    async def release_lease(self, lease: RunLease) -> None:
        """Release only the caller's current lease."""
        async with self._lock:
            await self.db.execute(
                "DELETE FROM run_leases WHERE run_id=? AND owner_id=? AND epoch=?",
                (lease.run_id, lease.owner_id, lease.epoch),
            )
            await self.db.commit()

    async def has_active_owner(self, run_id: str) -> bool:
        """Conservatively reject recovery while a lease or process may still own it."""
        async with self.db.execute(
            "SELECT owner_id, expires_at FROM run_leases WHERE run_id=?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None and (row[1] > time.time() or _owner_alive(row[0]))

    async def create_resumed_run(self, parent_run_id: str, checkpoint_id: str) -> str:
        """Create a new run from a verified parent checkpoint, never reviving it."""
        checkpoint = await self.latest_checkpoint(parent_run_id)
        if checkpoint is None or checkpoint["id"] != checkpoint_id:
            raise ValueError("invalid recovery checkpoint")
        new_run_id = str(uuid4())
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT task_id, status, model_provider, model_name, max_turns, max_tool_calls "
                    "FROM runs WHERE id=?",
                    (parent_run_id,),
                ) as cursor:
                    parent = await cursor.fetchone()
                if parent is None or parent["status"] != RunStatus.INTERRUPTED.value:
                    raise ValueError("parent run is not interrupted")
                async with self.db.execute(
                    "SELECT 1 FROM run_lineage WHERE parent_run_id=? LIMIT 1",
                    (parent_run_id,),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise ValueError("parent run already has a successor")
                async with self.db.execute(
                    "SELECT owner_id, expires_at FROM run_leases WHERE run_id=?",
                    (parent_run_id,),
                ) as cursor:
                    owner = await cursor.fetchone()
                if owner is not None and (owner[1] > time.time() or _owner_alive(owner[0])):
                    raise ValueError("parent run may still have an owner")
                async with self.db.execute(
                    "SELECT COUNT(*) FROM tool_executions WHERE run_id=? "
                    "AND status IN ('pending','unknown')",
                    (parent_run_id,),
                ) as cursor:
                    unresolved = await cursor.fetchone()
                if unresolved is not None and unresolved[0]:
                    raise ValueError("unresolved side effect requires review")
                async with self.db.execute(
                    "SELECT 1 FROM events WHERE run_id=? AND sequence>? "
                    "AND type IN ('ToolClaimed','ToolCompleted','ToolOutcomeUnknown') LIMIT 1",
                    (parent_run_id, checkpoint["last_event_seq"]),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        raise ValueError("effect after checkpoint requires review")
                await self.db.execute(
                    "INSERT INTO runs VALUES (?, ?, 'created', ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        new_run_id,
                        parent["task_id"],
                        parent["model_provider"],
                        parent["model_name"],
                        parent["max_turns"],
                        parent["max_tool_calls"],
                    ),
                )
                await self.db.execute(
                    "INSERT INTO run_lineage VALUES (?, ?, ?, 'resume', datetime('now'))",
                    (new_run_id, parent_run_id, checkpoint_id),
                )
                for index, message in enumerate(checkpoint["messages"], 1):
                    await self.db.execute(
                        "INSERT INTO context_messages VALUES (?, ?, ?, 1)",
                        (new_run_id, index, json.dumps(message, ensure_ascii=False)),
                    )
                await self._event(new_run_id, "RunResumedFrom", {"parent_run_id": parent_run_id})
                payload = canonical_json(
                    {
                        "schema_version": 1,
                        "messages": checkpoint["messages"],
                        "budget": checkpoint["budget"],
                    }
                )
                await self.db.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, 1, 1, ?, ?, ?, datetime('now'))",
                    (
                        str(uuid4()),
                        new_run_id,
                        len(checkpoint["messages"]),
                        payload,
                        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                    ),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(new_run_id, "RunResumedFrom")
        return new_run_id

    async def _migrate_v2(self) -> None:
        """Add durable model-visible facts without altering v1 history."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS context_messages("
                    "run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL, "
                    "payload_json TEXT NOT NULL, payload_version INTEGER NOT NULL, "
                    "PRIMARY KEY(run_id,seq))"
                )
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS checkpoints("
                    "id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), "
                    "version INTEGER NOT NULL, last_event_seq INTEGER NOT NULL, "
                    "last_message_seq INTEGER NOT NULL, snapshot_json TEXT NOT NULL, "
                    "sha256 TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "UNIQUE(run_id,version), UNIQUE(run_id,last_event_seq))"
                )
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS run_leases("
                    "run_id TEXT PRIMARY KEY REFERENCES runs(id), owner_id TEXT NOT NULL, "
                    "epoch INTEGER NOT NULL, expires_at REAL NOT NULL, heartbeat_at REAL NOT NULL)"
                )
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS run_lineage("
                    "new_run_id TEXT PRIMARY KEY REFERENCES runs(id), "
                    "parent_run_id TEXT NOT NULL REFERENCES runs(id), "
                    "source_checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id), "
                    "decision TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                await self.db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS one_successor_per_run "
                    "ON run_lineage(parent_run_id)"
                )
                async with self.db.execute("PRAGMA table_info(tool_executions)") as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}
                for name, column_type in (
                    ("outcome_json", "TEXT"),
                    ("effect_class", "TEXT"),
                    ("owner_epoch", "INTEGER"),
                    ("intent_at", "TEXT"),
                    ("outcome_at", "TEXT"),
                ):
                    if name not in columns:
                        await self.db.execute(
                            f"ALTER TABLE tool_executions ADD COLUMN {name} {column_type}"
                        )
                await self.db.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (2, datetime('now'))"
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def _migrate_v3(self) -> None:
        """Add digest-scoped local MCP launch authorization."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "CREATE TABLE IF NOT EXISTS mcp_launch_trust("
                    "server_name TEXT PRIMARY KEY, digest TEXT NOT NULL, approved_at TEXT NOT NULL)"
                )
                await self.db.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES (3, datetime('now'))"
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def mcp_launch_trusted(self, server_name: str, digest: str) -> bool:
        """Check whether the exact launch configuration was approved."""
        async with self.db.execute(
            "SELECT digest FROM mcp_launch_trust WHERE server_name=?", (server_name,)
        ) as cursor:
            row = await cursor.fetchone()
        return row is not None and row[0] == digest

    async def trust_mcp_launch(self, server_name: str, digest: str) -> None:
        """Persist an explicit approval for one launch digest."""
        async with self._lock:
            await self.db.execute(
                "INSERT INTO mcp_launch_trust VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(server_name) DO UPDATE SET digest=excluded.digest, "
                "approved_at=excluded.approved_at",
                (server_name, digest),
            )
            await self.db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        await self.db.close()

    async def _publish(self, run_id: str, kind: str) -> None:
        if self._publisher is not None:
            await self._publisher(run_id, kind)

    async def _event(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        async with self.db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        await self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (
                str(uuid4()),
                run_id,
                row[0],
                kind,
                json.dumps(_redact({"schema_version": 1, **payload})),
            ),
        )

    async def _context_message(self, run_id: str, payload: dict[str, Any]) -> None:
        async with self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM context_messages WHERE run_id=?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        await self.db.execute(
            "INSERT INTO context_messages VALUES (?, ?, ?, 1)",
            (run_id, row[0], json.dumps(_redact(payload), ensure_ascii=False)),
        )

    async def context_messages(self, run_id: str) -> list[dict[str, Any]]:
        """Load exactly the committed model-visible messages for a run."""
        async with self.db.execute(
            "SELECT payload_json FROM context_messages WHERE run_id=? ORDER BY seq", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def save_checkpoint(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        budget: dict[str, Any],
        epoch: int | None = None,
    ) -> str:
        """Commit a safe-boundary snapshot and its event atomically."""
        checkpoint_id = str(uuid4())
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                async with self.db.execute(
                    "SELECT COUNT(*) FROM tool_executions WHERE run_id=? AND status IN ('pending','unknown')",
                    (run_id,),
                ) as cursor:
                    unresolved = await cursor.fetchone()
                if unresolved is not None and unresolved[0]:
                    raise ValueError("unresolved tool prevents checkpoint")
                async with self.db.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?", (run_id,)
                ) as cursor:
                    event_row = await cursor.fetchone()
                async with self.db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM context_messages WHERE run_id=?", (run_id,)
                ) as cursor:
                    message_row = await cursor.fetchone()
                async with self.db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM checkpoints WHERE run_id=?", (run_id,)
                ) as cursor:
                    version_row = await cursor.fetchone()
                assert event_row is not None and message_row is not None and version_row is not None
                async with self.db.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE run_id=? AND last_message_seq=?",
                    (run_id, message_row[0]),
                ) as cursor:
                    duplicate = await cursor.fetchone()
                if duplicate is not None and duplicate[0]:
                    raise ValueError("checkpoint already exists for message frontier")
                payload = {
                    "schema_version": 1,
                    "messages": _redact(messages),
                    "budget": budget,
                }
                encoded = canonical_json(payload)
                digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                await self.db.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (
                        checkpoint_id,
                        run_id,
                        version_row[0],
                        event_row[0],
                        message_row[0],
                        encoded,
                        digest,
                    ),
                )
                await self._event(run_id, "CheckpointCreated", {"checkpoint_id": checkpoint_id})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "CheckpointCreated")
        return checkpoint_id

    async def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Return the newest valid snapshot, falling back past corruption."""
        async with self.db.execute(
            "SELECT id, version, last_event_seq, last_message_seq, snapshot_json, sha256 "
            "FROM checkpoints WHERE run_id=? ORDER BY version DESC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            encoded = row[4]
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row[5]:
                continue
            try:
                payload = json.loads(encoded)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or not isinstance(payload.get("messages"), list)
                or not isinstance(payload.get("budget"), dict)
            ):
                continue
            return {
                "id": row[0],
                "version": row[1],
                "last_event_seq": row[2],
                "last_message_seq": row[3],
                "messages": payload["messages"],
                "budget": payload["budget"],
            }
        return None

    async def record_model_turn(
        self, run_id: str, text: str, calls: list[ToolCall], epoch: int | None = None
    ) -> None:
        """Commit a complete assistant turn before any tool intent."""
        payload: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            payload["tool_calls"] = [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ]
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                await self._context_message(run_id, payload)
                await self._event(run_id, "ModelTurnCompleted", {"tool_calls": len(calls)})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "ModelTurnCompleted")

    async def create_run(self, request: AgentRequest) -> str:
        """Create session/task if needed and persist a new run atomically."""
        run_id = str(uuid4())
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "INSERT OR IGNORE INTO sessions VALUES (?, ?, datetime('now'), datetime('now'), NULL)",
                    (request.session_id, request.workspace),
                )
                async with self.db.execute(
                    "SELECT workspace_root FROM sessions WHERE id=?", (request.session_id,)
                ) as cursor:
                    session_row = await cursor.fetchone()
                if session_row is None or session_row[0] != request.workspace:
                    raise ValueError("session workspace mismatch")
                await self.db.execute(
                    "INSERT OR IGNORE INTO tasks VALUES (?, ?, ?, datetime('now'))",
                    (request.task_id, request.session_id, _redact(request.user_text)),
                )
                await self.db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        run_id,
                        request.task_id,
                        RunStatus.CREATED.value,
                        request.provider_name,
                        request.model,
                        request.max_turns,
                        request.max_tool_calls,
                    ),
                )
                await self._event(run_id, "RunCreated", {})
                await self._context_message(run_id, {"role": "user", "content": request.user_text})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "RunCreated")
        return run_id

    async def status(self, run_id: str) -> RunStatus:
        """Read current persisted run status."""
        async with self.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunStatus(row[0])

    async def set_status(self, run_id: str, status: RunStatus, epoch: int | None = None) -> None:
        """Apply a checked state change and append its event."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                current = await self.status(run_id)
                transition(current, status)
                if status in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
                    async with self.db.execute(
                        "SELECT call_id FROM tool_executions WHERE run_id=? AND status='pending'",
                        (run_id,),
                    ) as cursor:
                        unknown = await cursor.fetchall()
                    await self.db.execute(
                        "UPDATE tool_executions SET status='unknown' "
                        "WHERE run_id=? AND status='pending'",
                        (run_id,),
                    )
                    for row in unknown:
                        await self._event(run_id, "ToolOutcomeUnknown", {"call_id": row[0]})
                await self.db.execute(
                    "UPDATE runs SET status=?, ended_at=CASE WHEN ? IN ('completed','failed','cancelled','interrupted') THEN datetime('now') ELSE ended_at END WHERE id=?",
                    (status.value, status.value, run_id),
                )
                await self._event(run_id, "RunStatusChanged", {"status": status.value})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

        await self._publish(run_id, "RunStatusChanged")

    async def claim_tool(
        self,
        run_id: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
        epoch: int | None = None,
    ) -> bool:
        """Reserve a call ID before any possible side effect."""
        safe_args = _redact(args)
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                cursor = await self.db.execute(
                    "INSERT OR IGNORE INTO tool_executions "
                    "(id, run_id, call_id, tool_name, args_json, status, result_json, "
                    "approval, intent_at) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, datetime('now'))",
                    (str(uuid4()), run_id, call_id, name, json.dumps(safe_args)),
                )
                claimed = cursor.rowcount == 1
                if claimed:
                    await self._event(run_id, "ToolClaimed", {"call_id": call_id, "name": name})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        if claimed:
            await self._publish(run_id, "ToolClaimed")
        return claimed

    async def record_tool(self, run_id: str, result: ToolResult, epoch: int | None = None) -> None:
        """Commit a tool result and completion event in one transaction."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                cursor = await self.db.execute(
                    "UPDATE tool_executions SET status=?, result_json=? WHERE run_id=? AND call_id=? AND status='pending'",
                    (
                        result.status,
                        json.dumps(
                            {
                                "status": result.status,
                                "exit_code": result.exit_code,
                                "truncated": result.truncated,
                            }
                        ),
                        run_id,
                        result.call_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("tool call not pending")
                visible = {
                    "role": "tool",
                    "call_id": result.call_id,
                    "content": result.output[:16_000],
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "truncated": result.truncated or len(result.output) > 16_000,
                }
                await self.db.execute(
                    "UPDATE tool_executions SET outcome_json=?, outcome_at=datetime('now') "
                    "WHERE run_id=? AND call_id=?",
                    (json.dumps(_redact(visible)), run_id, result.call_id),
                )
                await self._context_message(run_id, visible)
                await self._event(
                    run_id, "ToolCompleted", {"call_id": result.call_id, "status": result.status}
                )
                if result.status == "ok" and result.evidence_id is not None:
                    if result.evidence_id.startswith("skill:"):
                        kind = "SkillActivated"
                    elif result.evidence_id.startswith("skill_resource:"):
                        kind = "SkillResourceLoaded"
                    else:
                        kind = None
                    if kind is not None:
                        await self._event(
                            run_id,
                            kind,
                            {
                                "schema_version": 1,
                                "call_id": result.call_id,
                                "reference": result.evidence_id,
                            },
                        )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

        await self._publish(run_id, "ToolCompleted")

    async def mark_tool_unknown(
        self, run_id: str, call_id: str, reason: str, epoch: int | None = None
    ) -> None:
        """Record an uncertain external outcome without creating a tool message."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                cursor = await self.db.execute(
                    "UPDATE tool_executions SET status='unknown' "
                    "WHERE run_id=? AND call_id=? AND status='pending'",
                    (run_id, call_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("tool call not pending")
                await self._event(
                    run_id, "ToolOutcomeUnknown", {"call_id": call_id, "reason": reason}
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "ToolOutcomeUnknown")

    async def record_hook_event(
        self, run_id: str, payload: dict[str, Any], epoch: int | None = None
    ) -> None:
        """Persist one versioned hook decision before exposing its effect."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                await self._event(run_id, "HookDecision", {"schema_version": 1, **payload})
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "HookDecision")

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        """Read ordered audit events for a run."""
        async with self.db.execute(
            "SELECT sequence, type, payload_json FROM events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"sequence": row[0], "type": row[1], "payload": json.loads(row[2])} for row in rows]

    async def record_step(
        self, run_id: str, kind: str, status: str, epoch: int | None = None
    ) -> None:
        """Append a numbered model or tool step."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                async with self.db.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM steps WHERE run_id=?", (run_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                await self.db.execute(
                    "INSERT INTO steps VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (str(uuid4()), run_id, row[0], kind, status),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def record_usage(
        self,
        run_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        epoch: int | None = None,
    ) -> None:
        """Append provider usage after receipt; absent counts remain null."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                await self._event(
                    run_id,
                    "ModelUsage",
                    {"input_tokens": input_tokens, "output_tokens": output_tokens},
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "ModelUsage")

    async def record_context_compaction(
        self,
        run_id: str,
        old_estimate: int,
        new_estimate: int,
        summary_hash: str,
        artifact_ids: tuple[str, ...] = (),
        epoch: int | None = None,
    ) -> None:
        """Persist the exact before/after estimate and projection hash."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                async with self.db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM context_messages WHERE run_id=?",
                    (run_id,),
                ) as cursor:
                    source_row = await cursor.fetchone()
                assert source_row is not None
                await self._event(
                    run_id,
                    "ContextCompacted",
                    {
                        "old_estimate": old_estimate,
                        "new_estimate": new_estimate,
                        "summary_hash": summary_hash,
                        "source_message_seq_end": source_row[0],
                        "artifact_ids": list(artifact_ids),
                        "estimator_version": 1,
                    },
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "ContextCompacted")

    async def artifact_content(self, run_id: str, artifact_id: str) -> str:
        """Read a committed, redacted tool body by its content digest."""
        async with self.db.execute(
            "SELECT payload_json FROM context_messages WHERE run_id=? ORDER BY seq",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            payload = json.loads(row[0])
            if payload.get("role") != "tool":
                continue
            content = payload.get("content")
            if (
                isinstance(content, str)
                and hashlib.sha256(content.encode()).hexdigest() == artifact_id
            ):
                return content
        raise KeyError(artifact_id)

    async def record_model_attempt(
        self,
        run_id: str,
        attempt_id: str,
        attempt_number: int,
        status: str,
        code: str | None = None,
        semantic_output: bool = False,
        backoff_seconds: float | None = None,
        epoch: int | None = None,
    ) -> None:
        """Audit one bounded provider attempt after the event transaction commits."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self._assert_epoch(run_id, epoch)
                await self._event(
                    run_id,
                    "ModelAttempt",
                    {
                        "attempt_id": attempt_id,
                        "attempt_number": attempt_number,
                        "status": status,
                        "code": code,
                        "semantic_output": semantic_output,
                        "backoff_seconds": backoff_seconds,
                    },
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "ModelAttempt")

    async def steps(self, run_id: str) -> list[dict[str, str]]:
        """Read ordered model and tool steps."""
        async with self.db.execute(
            "SELECT kind, status FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"kind": row[0], "status": row[1]} for row in rows]

    async def append_message(self, session_id: str, run_id: str, role: str, content: str) -> None:
        """Append a session message in order."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM messages WHERE session_id=?",
                    (session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                assert row is not None
                await self.db.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), session_id, run_id, role, json.dumps(_redact(content)), row[0]),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def record_evidence(
        self, run_id: str, kind: str, summary: str, call_id: str | None = None
    ) -> None:
        """Persist a verification fact and its event together."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, '{}', datetime('now'))",
                    (str(uuid4()), run_id, kind, call_id, _redact(summary)),
                )
                await self._event(
                    run_id, "VerificationRecorded", {"kind": kind, "call_id": call_id}
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        await self._publish(run_id, "VerificationRecorded")

    async def sessions(self) -> list[dict[str, str]]:
        """List known sessions newest first."""
        async with self.db.execute(
            "SELECT id, workspace_root FROM sessions ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"id": row[0], "workspace": row[1]} for row in rows]

    async def run_info(self, run_id: str) -> dict[str, Any]:
        """Read task and workspace metadata needed for recovery decisions."""
        async with self.db.execute(
            "SELECT runs.id, runs.task_id, runs.status, runs.model_provider, runs.model_name, "
            "runs.max_turns, runs.max_tool_calls, tasks.prompt, "
            "sessions.id AS session_id, sessions.workspace_root "
            "FROM runs JOIN tasks ON runs.task_id=tasks.id "
            "JOIN sessions ON tasks.session_id=sessions.id WHERE runs.id=?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    async def unresolved_tools(self, run_id: str) -> list[dict[str, str]]:
        """Show pending and unknown effects without leaking arguments."""
        async with self.db.execute(
            "SELECT call_id, tool_name, status FROM tool_executions "
            "WHERE run_id=? AND status IN ('pending','unknown') ORDER BY intent_at",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def effects_after_checkpoint(self, run_id: str, event_seq: int) -> bool:
        """Detect any tool intent or outcome beyond the last safe frontier."""
        async with self.db.execute(
            "SELECT 1 FROM events WHERE run_id=? AND sequence>? "
            "AND type IN ('ToolClaimed','ToolCompleted','ToolOutcomeUnknown') LIMIT 1",
            (run_id, event_seq),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        """Read ordered messages for a session."""
        async with self.db.execute(
            "SELECT role, content_json FROM messages WHERE session_id=? ORDER BY ordinal",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"role": row[0], "content": json.loads(row[1])} for row in rows]

    async def evidence(self, run_id: str) -> list[dict[str, str]]:
        """Read persisted verification facts for a run."""
        async with self.db.execute(
            "SELECT kind, summary FROM evidence WHERE run_id=? ORDER BY created_at", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"kind": row[0], "summary": row[1]} for row in rows]
