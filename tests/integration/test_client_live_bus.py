"""V1.1-M2：ClientService 进程内 Run 走事件总线（live 事件 + 终态）。

同一 Run 的持久化事件仍可经 collect() 读取（SQLite 只做持久化/重放）。
"""

import asyncio
from pathlib import Path

import pytest

from deity.application.client_service import ClientService
from deity.application.config import PermissionConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        for event in next(self.turns):
            yield event


def _service(tmp_path: Path, model: FakeModel) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="auto", workspace_write="auto"),
    )


@pytest.mark.asyncio
async def test_subscribe_events_uses_bus_for_in_process_run(tmp_path: Path) -> None:
    model = FakeModel([[ModelEvent("text_delta", text="hi"), ModelEvent("message_completed")]])
    client = ClientService(_service(tmp_path, model))
    handle = await client.start_run("打招呼", "fake")
    subscription = await client.subscribe_events(handle.run_id, after_seq=0)
    events = []
    async for event in subscription:
        events.append(event)
    kinds = [event.kind for event in events]
    assert "text_delta" in kinds
    assert kinds[-1] == "run_status"
    assert events[-1].payload["status"] == "completed"
    assert events[-1].payload["final_text"] == "hi"
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    # 持久化路径不变：collect 仍读 SQLite 事件。
    persisted = await client.collect(handle.run_id, after_seq=0)
    assert "RunStatusChanged" in {event.kind for event in persisted}


@pytest.mark.asyncio
async def test_bus_replay_for_late_subscriber_after_completion(tmp_path: Path) -> None:
    model = FakeModel([[ModelEvent("text_delta", text="done"), ModelEvent("message_completed")]])
    client = ClientService(_service(tmp_path, model))
    handle = await client.start_run("打招呼", "fake")
    result = await asyncio.wait_for(client.wait(handle.run_id), timeout=30)
    assert result.status == RunStatus.COMPLETED
    subscription = await client.subscribe_events(handle.run_id, after_seq=0)
    events = [event async for event in subscription]
    assert events[-1].kind == "run_status"
    assert events[-1].payload["final_text"] == "done"
