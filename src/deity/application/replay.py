"""只读回放：重建 Run 终态与证据摘要，绝不执行真实写入或网络副作用。

回放时工具分发器替换为 ReplayDispatcher：写工具与 Shell 一律拒绝并记为
replay_blocked；只读工具可走本地读。事件 schema_version 不一致时只读旧事件
并标注 schema_mismatch，不迁移写入。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deity.adapters.tools import WRITE_TOOLS, LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall, ToolResult
from deity.core.contracts.client import ClientEvent
from deity.interfaces.tui.view_model import RunViewModel

CURRENT_SCHEMA_VERSION = 1
READ_ONLY_TOOLS = {"read_file", "list_files", "glob", "grep", "git_status", "git_diff"}

__all__ = ["ReplayDispatcher", "ReplayResult", "TraceReplayer"]


@dataclass(frozen=True)
class ReplayResult:
    """一次只读回放的结果：终态、证据摘要与拦截记录。"""

    run_id: str
    case_id: str
    status: str
    events_seen: int
    schema_mismatch: bool
    blocked: tuple[str, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)


class ReplayDispatcher:
    """回放期工具分发器：写工具与 Shell 直接拒绝并记 replay_blocked。"""

    def __init__(self) -> None:
        self._reader = LocalToolDispatcher()
        self._blocked: list[str] = []

    @property
    def blocked(self) -> tuple[str, ...]:
        return tuple(self._blocked)

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        """写工具/Shell/未知工具拒绝；只读工具走本地读，不写文件。"""
        if call.name in WRITE_TOOLS | {"shell"} or call.name not in READ_ONLY_TOOLS:
            self._blocked.append(call.call_id)
            return ToolResult(
                call.call_id,
                "denied",
                f"replay_blocked: 回放模式拒绝执行 {call.name}",
            )
        return await self._reader.dispatch(call, context)


class TraceReplayer:
    """读报告或 SQLite events 只读重放，重建终态与证据摘要。"""

    def replay_events(self, run_id: str, events: list[ClientEvent]) -> ReplayResult:
        """折叠事件流为视图快照；旧 schema 只读并标注，不迁移写入。"""
        view_model = RunViewModel(run_id=run_id)
        mismatch = False
        for event in events:
            if event.schema_version != CURRENT_SCHEMA_VERSION:
                mismatch = True
            view_model.apply(event)
        snapshot = view_model.snapshot()
        return ReplayResult(
            run_id=run_id,
            case_id=run_id,
            status=snapshot["status"],
            events_seen=len(events),
            schema_mismatch=mismatch,
            summary={
                "text": snapshot["text"],
                "tool_calls": snapshot["tool_calls"],
                "approvals": snapshot["approvals"],
            },
        )

    def replay_report(self, report_path: Path) -> ReplayResult:
        """读一份评估报告 JSON，重建同一 verdict；不执行任何工具。"""
        if not report_path.is_file():
            raise ValueError(f"报告不存在：{report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        passed = bool(report.get("passed", report.get("verification") == "PASS"))
        case_id = str(report.get("case_id", report_path.stem))
        return ReplayResult(
            run_id=case_id,
            case_id=case_id,
            status="completed" if passed else "failed",
            events_seen=0,
            schema_mismatch=False,
            summary={
                "verification": report.get("verification"),
                "duration_ms": report.get("duration_ms"),
                "model": report.get("model"),
            },
        )

    def replay_sqlite(self, db_path: Path, run_id: str) -> ReplayResult:
        """只读打开 SQLite（mode=ro），按序读 events 重建终态。"""
        if not db_path.is_file():
            raise ValueError(f"数据库不存在：{db_path}")
        uri = f"file:{db_path}?mode=ro"
        events: list[ClientEvent] = []
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute(
                "SELECT sequence, type, payload_json FROM events "
                "WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            status_row = connection.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if not rows and status_row is None:
            raise ValueError(f"未知 Run：{run_id}")
        for sequence, kind, payload_json in rows:
            payload = json.loads(payload_json) if payload_json else {}
            schema_version = int(payload.pop("schema_version", 1))
            events.append(
                ClientEvent(
                    seq=int(sequence),
                    run_id=run_id,
                    kind=str(kind),
                    payload=payload,
                    schema_version=schema_version,
                )
            )
        result = self.replay_events(run_id, events)
        status = str(status_row[0]) if status_row is not None else result.status
        return ReplayResult(
            run_id=run_id,
            case_id=run_id,
            status=status,
            events_seen=result.events_seen,
            schema_mismatch=result.schema_mismatch,
            summary=result.summary,
        )
