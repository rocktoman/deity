from collections.abc import AsyncIterator

import pytest

from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
    ToolResult,
)
from deity.core.contracts.client import ClientEvent
from deity.core.runtime import AgentLoop


class ScriptedModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ModelEvent("tool_call_started", call_id="read-1", name="read_file")
            yield ModelEvent("tool_call_arguments_delta", call_id="read-1", text='{"path":"a.py"}')
            yield ModelEvent("message_completed")
        else:
            yield ModelEvent("text_delta", text="读取完成")
            yield ModelEvent("message_completed")


class ScriptedTool:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        self.calls.append(call)
        assert context.workspace == "/fixture"
        return ToolResult(call.call_id, "ok", "print('hello')")


@pytest.mark.asyncio
async def test_fake_model_tool_context_and_final_answer() -> None:
    model = ScriptedModel()
    tool = ScriptedTool()
    result = await AgentLoop(model, tool).run(
        AgentRequest("session-1", "task-1", "/fixture", "读取 a.py", "scripted")
    )
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "读取完成"
    assert len(tool.calls) == 1
    assert tool.calls[0].arguments == {"path": "a.py"}
    assert model.requests[1].messages[-1]["call_id"] == "read-1"
    assert model.requests[1].messages[-1]["content"] == "print('hello')"
    assert model.requests[1].messages[-1]["status"] == "ok"


# ADR-023：live tool_call_completed 事件带输出预览与退出码


class _PreviewTool:
    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        return ToolResult(
            call.call_id,
            "failed",
            "FAILED tests/test_x.py::test_a\nassert 1 == 2\n1 failed\n2 passed",
            exit_code=1,
        )


@pytest.mark.asyncio
async def test_tool_call_completed_event_carries_preview_and_exit_code() -> None:
    model = ScriptedModel()
    tool = _PreviewTool()
    seen: list[ClientEvent] = []
    result = await AgentLoop(model, tool).run(
        AgentRequest("session-1", "task-1", "/fixture", "读取 a.py", "scripted"),
        live_event_sink=seen.append,
    )
    assert result.status == RunStatus.COMPLETED
    completed = [e for e in seen if e.kind == "tool_call_completed"]
    assert len(completed) == 1
    assert completed[0].payload["exit_code"] == 1
    assert completed[0].payload["output_preview"].startswith("FAILED tests/test_x.py")
    assert "1 failed" in completed[0].payload["output_preview"]
    # 预览只取前 3 行，第 4 行不出现。
    assert "2 passed" not in completed[0].payload["output_preview"]
