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
