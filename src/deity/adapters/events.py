"""SQLite 事件订阅：快照 + 只读轮询，不写事件。"""

from pathlib import Path
from typing import Any, Self

from deity.adapters.storage import SQLiteSessionRepository
from deity.core.contracts.client import ClientEvent

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def event_from_record(run_id: str, record: dict[str, Any], *, created_at: str = "") -> ClientEvent:
    """把仓储事件行映射为 ClientEvent；payload 内的 schema_version 不重复暴露。"""
    payload = dict(record.get("payload") or {})
    schema_version = payload.pop("schema_version", 1)
    return ClientEvent(
        seq=int(record["sequence"]),
        run_id=run_id,
        schema_version=int(schema_version),
        kind=str(record["type"]),
        payload=payload,
        created_at=created_at,
    )


class SQLiteEventSubscriber:
    """读 SQLite 已持久化事件；订阅期间持有一个只读仓储连接。"""

    def __init__(
        self, db_path: Path, *, poll_interval_seconds: float = 0.1, max_backoff_seconds: float = 1.0
    ) -> None:
        if poll_interval_seconds <= 0 or max_backoff_seconds < poll_interval_seconds:
            raise ValueError("invalid poll interval")
        self.db_path = db_path
        self.poll_interval_seconds = poll_interval_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._repo: SQLiteSessionRepository | None = None

    async def __aenter__(self) -> Self:
        self._repo = await SQLiteSessionRepository.open(self.db_path)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭订阅持有的仓储连接。"""
        if self._repo is not None:
            await self._repo.close()
            self._repo = None

    async def read(self, run_id: str, after_seq: int = 0) -> list[ClientEvent]:
        """读出 seq 大于 after_seq 的事件；只读，不写事件。"""
        repo = self._repo
        if repo is None:
            repo = await SQLiteSessionRepository.open(self.db_path)
            try:
                rows = await repo.events(run_id)
            finally:
                await repo.close()
        else:
            rows = await repo.events(run_id)
        return [event_from_record(run_id, row) for row in rows if int(row["sequence"]) > after_seq]

    async def snapshot(self, run_id: str, after_seq: int = 0) -> list[ClientEvent]:
        """快照：与 read() 相同语义，供调用方按快照 + 增量理解。"""
        return await self.read(run_id, after_seq)
