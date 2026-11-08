"""Phase 0：AgentLoop live 事件出口与 RunService.run_stream（ADR-016）。

复用 v0.8 已落地的 ClientEvent / ClientService 做持久化订阅；
本文件只覆盖新增的 live-only 出口：实时文本增量、工具完成、
usage 直通、取消终态，以及 run_stream 与 run() 的最终结果一致。
"""

import asyncio
from pathlib import Path

import pytest

from deity.application.config import PermissionConfig, RunConfig
from deity.application.run_service import RunService
from deity.core.contracts import ModelEvent, RunStatus, ToolResult
from deity.core.contracts.client import ClientEvent
from deity.core.runtime import AgentLoop, AgentRequest, BudgetExceeded


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]], delay: float = 0.0) -> None:
        self.turns = iter(turns)
        self.delay = delay

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in next(self.turns):
            yield event


class SlowModel:
    """先吐一个增量，随后一直睡，直到被取消。"""

    async def stream(self, request: object):  # type: ignore[no-untyped-def]
        yield ModelEvent("text_delta", text="slow")
        await asyncio.sleep(30)


class FakeTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, call, context):  # type: ignore[no-untyped-def]
        self.calls.append(call.call_id)
        return ToolResult(call.call_id, "ok", "done")


def _text_turns() -> list[list[ModelEvent]]:
    return [
        [
            ModelEvent("text_delta", text="你好"),
            ModelEvent("text_delta", text="世界"),
            ModelEvent("usage", input_tokens=10, output_tokens=4),
            ModelEvent("message_completed"),
        ]
    ]


def _tool_turns() -> list[list[ModelEvent]]:
    return [
        [
            ModelEvent("tool_call_started", call_id="c1", name="read_file"),
            ModelEvent("tool_call_arguments_delta", call_id="c1", text='{"path":'),
            ModelEvent("tool_call_arguments_delta", call_id="c1", text='"a.txt"}'),
            ModelEvent("message_completed"),
        ],
        [ModelEvent("text_delta", text="ok"), ModelEvent("message_completed")],
    ]


def _service(tmp_path: Path, model: object, **kwargs: object) -> RunService:
    return RunService(
        db_path=tmp_path / "deity.db",
        workspace=tmp_path,
        model=model,  # type: ignore[arg-type]
        permission=PermissionConfig(shell="ask", workspace_write="ask"),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_live_sink_receives_model_events_in_order() -> None:
    seen: list[ClientEvent] = []
    loop = AgentLoop(FakeModel(_tool_turns()), FakeTool())
    result = await loop.run(
        AgentRequest("s", "t", "/tmp", "hi", "fake"),
        live_event_sink=seen.append,
    )
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "ok"
    kinds = [event.kind for event in seen]
    assert kinds == [
        "tool_call_started",
        "tool_call_arguments_delta",
        "tool_call_arguments_delta",
        "message_completed",
        "tool_call_completed",
        "text_delta",
        "message_completed",
    ]
    assert [event.seq for event in seen] == list(range(1, len(seen) + 1))
    assert all(event.run_id == result.run_id for event in seen)
    assert all(event.schema_version == 1 for event in seen)
    started = seen[0]
    assert started.payload["call_id"] == "c1"
    assert started.payload["name"] == "read_file"
    completed = seen[4]
    assert completed.payload["call_id"] == "c1"
    assert completed.payload["name"] == "read_file"
    assert completed.payload["status"] == "ok"


@pytest.mark.asyncio
async def test_live_sink_receives_usage_event() -> None:
    seen: list[ClientEvent] = []
    loop = AgentLoop(FakeModel(_text_turns()), FakeTool())
    result = await loop.run(
        AgentRequest("s", "t", "/tmp", "hi", "fake"),
        live_event_sink=seen.append,
    )
    assert result.final_text == "你好世界"
    usage = [event for event in seen if event.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].payload["input_tokens"] == 10
    assert usage[0].payload["output_tokens"] == 4


@pytest.mark.asyncio
async def test_live_sink_failure_does_not_break_run() -> None:
    def _boom(event: ClientEvent) -> None:
        raise RuntimeError("sink 炸了")

    loop = AgentLoop(FakeModel(_text_turns()), FakeTool())
    result = await loop.run(
        AgentRequest("s", "t", "/tmp", "hi", "fake"),
        live_event_sink=_boom,
    )
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "你好世界"


@pytest.mark.asyncio
async def test_run_without_sink_is_unchanged() -> None:
    loop = AgentLoop(FakeModel(_text_turns()), FakeTool())
    result = await loop.run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "你好世界"


@pytest.mark.asyncio
async def test_run_stream_matches_run_final_text(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(_text_turns()))
    streamed: list[ClientEvent] = []
    async for event in service.run_stream("打招呼", model_name="fake"):
        streamed.append(event)
    assert streamed[-1].kind == "run_status"
    assert streamed[-1].payload["status"] == "completed"
    assert streamed[-1].payload["final_text"] == "你好世界"
    run_id = streamed[0].run_id
    assert run_id
    assert all(event.run_id == run_id for event in streamed)
    assert [event.seq for event in streamed] == list(range(1, len(streamed) + 1))
    text = "".join(
        str(event.payload.get("text", "")) for event in streamed if event.kind == "text_delta"
    )
    assert text == "你好世界"

    expected = await _service(tmp_path, FakeModel(_text_turns())).run("打招呼", model_name="fake")
    assert expected.status == RunStatus.COMPLETED
    assert expected.final_text == "你好世界"


@pytest.mark.asyncio
async def test_run_stream_emits_tool_call_completed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    service = _service(tmp_path, FakeModel(_tool_turns()))
    streamed: list[ClientEvent] = []
    async for event in service.run_stream("读文件", model_name="fake"):
        streamed.append(event)
    kinds = [event.kind for event in streamed]
    assert "tool_call_completed" in kinds
    completed = next(event for event in streamed if event.kind == "tool_call_completed")
    assert completed.payload["call_id"] == "c1"
    assert completed.payload["name"] == "read_file"
    assert completed.payload["status"] == "ok"
    assert streamed[-1].kind == "run_status"
    assert streamed[-1].payload["final_text"] == "ok"


@pytest.mark.asyncio
async def test_run_stream_usage_reaches_persisted_events(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeModel(_text_turns()))
    streamed: list[ClientEvent] = []
    async for event in service.run_stream("打招呼", model_name="fake"):
        streamed.append(event)
    usage = [event for event in streamed if event.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].payload["input_tokens"] == 10
    assert usage[0].payload["output_tokens"] == 4
    run_id = streamed[0].run_id
    from deity.adapters.storage import SQLiteSessionRepository

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    try:
        kinds = {row["type"] for row in await repo.events(run_id)}
    finally:
        await repo.close()
    assert "ModelUsage" in kinds


@pytest.mark.asyncio
async def test_run_stream_cancel_yields_cancelled_status(tmp_path: Path) -> None:
    service = _service(tmp_path, SlowModel())
    streamed: list[ClientEvent] = []

    async def _consume() -> None:
        async for event in service.run_stream("慢任务", model_name="fake"):
            streamed.append(event)

    consumer = asyncio.create_task(_consume())
    try:
        for _ in range(100):
            if streamed:
                break
            await asyncio.sleep(0.05)
        assert streamed and streamed[0].kind == "text_delta"
        assert service.cancel(streamed[0].run_id) is True
        await asyncio.wait_for(consumer, timeout=10)
    finally:
        if not consumer.done():
            consumer.cancel()
    assert streamed[-1].kind == "run_status"
    assert streamed[-1].payload["status"] == "cancelled"


@pytest.mark.asyncio
async def test_run_stream_propagates_error_after_drain(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        FakeModel(_tool_turns()),
        limits=RunConfig(max_turns=1),
    )
    streamed: list[ClientEvent] = []
    with pytest.raises(BudgetExceeded):
        async for event in service.run_stream("读文件", model_name="fake"):
            streamed.append(event)
    assert any(event.kind == "tool_call_started" for event in streamed)
    assert not any(event.kind == "run_status" for event in streamed)
