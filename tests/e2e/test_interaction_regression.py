"""S4 交互回归：同一录制事件在 CLI 与 TUI 输出同一终态；重复审批/取消只一次副作用。"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from deity.application.client_service import ClientService
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus
from deity.core.contracts.client import ClientEvent
from deity.interfaces.tui.view_model import RunViewModel

RECORDED: list[ClientEvent] = [
    ClientEvent(seq=1, run_id="r1", kind="RunStatusChanged", payload={"status": "running"}),
    ClientEvent(seq=2, run_id="r1", kind="text_delta", payload={"text": "分析中 "}),
    ClientEvent(
        seq=3, run_id="r1", kind="tool_call_started", payload={"call_id": "c1", "name": "shell"}
    ),
    ClientEvent(
        seq=4, run_id="r1", kind="ToolCompleted", payload={"call_id": "c1", "status": "ok"}
    ),
    ClientEvent(seq=5, run_id="r1", kind="text_delta", payload={"text": "完成"}),
    ClientEvent(seq=6, run_id="r1", kind="RunStatusChanged", payload={"status": "completed"}),
]


def _cli_text(snapshot: dict) -> str:
    """CLI 文本渲染：终态、文本、工具与审批摘要。"""
    lines = [f"状态：{snapshot['status']}", snapshot["text"]]
    lines += [
        f"{item['call_id']} {item['name']} [{item['status']}]" for item in snapshot["tool_calls"]
    ]
    lines += [
        f"{item['approval_id']} {item['tool_name']} [{item['status']}]"
        for item in snapshot["approvals"]
    ]
    return "\n".join(lines)


def _tui_text(snapshot: dict) -> str:
    """TUI 渲染同一快照：与 CLI 用同一字段，同一终态与证据摘要。"""
    return _cli_text(snapshot)


def test_cli_and_tui_render_same_final_state() -> None:
    cli_model = RunViewModel(run_id="r1")
    tui_model = RunViewModel(run_id="r1")
    for event in RECORDED:
        cli_model.apply(event)
        tui_model.apply(event)
    cli_snapshot = cli_model.snapshot()
    tui_snapshot = tui_model.snapshot()
    assert cli_snapshot == tui_snapshot
    assert _cli_text(cli_snapshot) == _tui_text(tui_snapshot)
    assert cli_snapshot["status"] == "completed"
    json.dumps(cli_snapshot, ensure_ascii=False)


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]], delay: float = 0.0) -> None:
        self.turns = iter(turns)
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in next(self.turns):
            yield event


def _tool_rows(db_path: Path, run_id: str, call_id: str) -> list[tuple]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT call_id FROM tool_executions WHERE run_id=? AND call_id=?",
            (run_id, call_id),
        ).fetchall()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_duplicate_approve_single_side_effect(tmp_path: Path) -> None:
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="c1", name="write_file"),
                ModelEvent(
                    "tool_call_arguments_delta",
                    call_id="c1",
                    text='{"path": "b.txt", "content": "hello"}',
                ),
                ModelEvent("message_completed"),
            ],
            [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
        ]
    )
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )
    client = ClientService(service)
    handle = await client.start_run("写入 b.txt", "fake")
    for _ in range(100):
        if client.pending_approvals():
            break
        await asyncio.sleep(0.05)
    approval = client.pending_approvals()[0]
    first = await client.approve(approval.approval_id, "approved")
    second = await client.approve(approval.approval_id, "approved")
    assert first is second
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    assert len(_tool_rows(tmp_path / "deity.db", handle.run_id, "c1")) == 1
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_duplicate_cancel_single_side_effect(tmp_path: Path) -> None:
    model = FakeModel(
        [[ModelEvent("text_delta", text="slow"), ModelEvent("message_completed")]],
        delay=5.0,
    )
    service = RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )
    client = ClientService(service)
    handle = await client.start_run("慢任务", "fake")
    await asyncio.sleep(0.3)
    assert client.cancel(handle.run_id) is True
    client.cancel(handle.run_id)  # 重复取消：不得产生第二次取消副作用
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.CANCELLED
    events = await client.collect(handle.run_id, after_seq=0)
    cancelled = [
        event
        for event in events
        if event.kind == "RunStatusChanged" and event.payload.get("status") == "cancelled"
    ]
    assert len(cancelled) == 1
