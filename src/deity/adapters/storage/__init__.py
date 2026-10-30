"""SQLite-backed sessions, runs, tool audit and events."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from deity.core.contracts import AgentRequest, RunStatus, ToolResult
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
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(SCHEMA)
        await db.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1, datetime('now'))")
        await db.commit()
        repo = cls(db, publisher)
        async with repo._lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT id FROM runs WHERE status IN ('running', 'waiting_approval')"
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    await db.execute(
                        "UPDATE runs SET status='interrupted', ended_at=datetime('now') WHERE id=?",
                        (row["id"],),
                    )
                    await repo._event(row["id"], "RunInterrupted", {})
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return repo

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
            (str(uuid4()), run_id, row[0], kind, json.dumps({"schema_version": 1, **payload})),
        )

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
                    (request.task_id, request.session_id, request.user_text),
                )
                await self.db.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, '', ?, NULL, NULL, NULL, ?, ?)",
                    (
                        run_id,
                        request.task_id,
                        RunStatus.CREATED.value,
                        request.model,
                        request.max_turns,
                        request.max_tool_calls,
                    ),
                )
                await self._event(run_id, "RunCreated", {})
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

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Apply a checked state change and append its event."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                current = await self.status(run_id)
                transition(current, status)
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

    async def claim_tool(self, run_id: str, call_id: str, name: str, args: dict[str, Any]) -> bool:
        """Reserve a call ID before any possible side effect."""
        safe_args = {
            key: "[redacted]"
            if any(word in key.lower() for word in ("key", "token", "secret", "password"))
            else value
            for key, value in args.items()
        }
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "INSERT OR IGNORE INTO tool_executions VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL)",
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

    async def record_tool(self, run_id: str, result: ToolResult) -> None:
        """Commit a tool result and completion event in one transaction."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
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
                await self._event(
                    run_id, "ToolCompleted", {"call_id": result.call_id, "status": result.status}
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

        await self._publish(run_id, "ToolCompleted")

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        """Read ordered audit events for a run."""
        async with self.db.execute(
            "SELECT sequence, type, payload_json FROM events WHERE run_id=? ORDER BY sequence",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"sequence": row[0], "type": row[1], "payload": json.loads(row[2])} for row in rows]

    async def record_step(self, run_id: str, kind: str, status: str) -> None:
        """Append a numbered model or tool step."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
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
        self, run_id: str, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        """Append provider usage after receipt; absent counts remain null."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
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
                    (str(uuid4()), session_id, run_id, role, json.dumps(content), row[0]),
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
                    (str(uuid4()), run_id, kind, call_id, summary),
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
