"""S1 集成：ClientService 经 RunService 跑完整闭环。"""

import asyncio
from pathlib import Path

import pytest

from deity.application.client_service import ClientService
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus, ToolResult


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]], delay: float = 0.0) -> None:
        self.turns = iter(turns)
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in next(self.turns):
            yield event


class FakeTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, call, context):  # type: ignore[no-untyped-def]
        self.calls.append(call.call_id)
        return ToolResult(call.call_id, "ok", "done")


def _service(tmp_path: Path, model: FakeModel) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
    )


@pytest.mark.asyncio
async def test_start_run_subscribe_approve_complete(tmp_path: Path) -> None:
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
    client = ClientService(_service(tmp_path, model))
    handle = await client.start_run("写入 b.txt", "fake")
    assert handle.run_id
    assert handle.session_id
    sub = await client.subscribe_events(handle.run_id, after_seq=0)
    try:
        for _ in range(100):
            pending = client.pending_approvals()
            if pending:
                break
            await asyncio.sleep(0.05)
        pending = client.pending_approvals()
        assert len(pending) == 1
        assert pending[0].tool_name == "write_file"
        assert "hello" not in str(pending[0].args)
        decided = await client.approve(pending[0].approval_id, "approved")
        assert decided.status == "approved"
        result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
        assert result.status == RunStatus.COMPLETED
        assert result.final_text == "done"
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "hello"
        events = await client.collect(handle.run_id, after_seq=0)
        kinds = {event.kind for event in events}
        assert "RunStatusChanged" in kinds
        assert "ToolCompleted" in kinds
        assert len({event.seq for event in events}) == len(events)
    finally:
        await sub.close()


@pytest.mark.asyncio
async def test_cli_and_client_service_see_same_run(tmp_path: Path) -> None:
    model = FakeModel([[ModelEvent("text_delta", text="hi"), ModelEvent("message_completed")]])
    service = _service(tmp_path, model)
    client = ClientService(service)
    handle = await client.start_run("打招呼", "fake")
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    report = await service.inspect(handle.run_id)
    assert report["decision"] in {"abort", "manual_review", "resume"}
    events = await client.collect(handle.run_id, after_seq=0)
    assert events
    assert all(event.run_id == handle.run_id for event in events)
    assert all(event.schema_version == 1 for event in events)


@pytest.mark.asyncio
async def test_cancel_propagates_to_running_run(tmp_path: Path) -> None:
    model = FakeModel(
        [[ModelEvent("text_delta", text="slow"), ModelEvent("message_completed")]],
        delay=5.0,
    )
    client = ClientService(_service(tmp_path, model))
    handle = await client.start_run("慢任务", "fake")
    await asyncio.sleep(0.3)
    assert client.cancel(handle.run_id) is True
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_duplicate_approve_has_single_effect(tmp_path: Path) -> None:
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
    client = ClientService(_service(tmp_path, model))
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
    events = await client.collect(handle.run_id, after_seq=0)
    claimed = [event for event in events if event.kind == "ToolClaimed"]
    assert len(claimed) == 1
