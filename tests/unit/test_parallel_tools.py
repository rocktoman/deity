"""F1-A2：只读工具并行（ADR-018）——批次并发、写屏障、结果按原序回注。"""

import asyncio
import json
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

_CALLS = [
    ("r1", "read_file", {"path": "a.txt"}),
    ("r2", "read_file", {"path": "b.txt"}),
    ("w1", "write_file", {"path": "c.txt", "content": "C2"}),
    ("r3", "read_file", {"path": "c.txt"}),
]


class _ScriptedModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            for call_id, name, arguments in _CALLS:
                yield ModelEvent("tool_call_started", call_id=call_id, name=name)
                yield ModelEvent(
                    "tool_call_arguments_delta",
                    call_id=call_id,
                    text=json.dumps(arguments),
                )
            yield ModelEvent("message_completed")
        else:
            yield ModelEvent("text_delta", text="完成")
            yield ModelEvent("message_completed")


class _RecordingTools:
    """带并发计数的假分发器：read 慢、write 改存储，验证批次与屏障语义。"""

    def __init__(self, parallel: bool) -> None:
        self._parallel = parallel
        self.store = {"a.txt": "A", "b.txt": "B", "c.txt": "C1"}
        self.in_flight = 0
        self.max_in_flight = 0

    def supports_parallel(self, name: str) -> bool:
        return self._parallel and name == "read_file"

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            if call.name == "read_file":
                return ToolResult(call.call_id, "ok", self.store[call.arguments["path"]])
            self.store[call.arguments["path"]] = call.arguments["content"]
            return ToolResult(call.call_id, "ok", "created")
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_read_batch_runs_concurrently_with_write_barrier() -> None:
    model = _ScriptedModel()
    tools = _RecordingTools(parallel=True)
    result = await AgentLoop(model, tools).run(
        AgentRequest("s", "t", "/fixture", "task", "scripted")
    )
    assert result.status == RunStatus.COMPLETED
    assert tools.max_in_flight >= 2  # r1/r2 并发
    results = [
        message
        for message in model.requests[1].messages
        if isinstance(message, dict) and "call_id" in message and "status" in message
    ]
    assert [message["call_id"] for message in results] == ["r1", "r2", "w1", "r3"]
    contents = {message["call_id"]: message["content"] for message in results}
    assert contents["r1"] == "A" and contents["r2"] == "B"
    assert contents["r3"] == "C2"  # 写屏障：写后的读必须看到新内容


@pytest.mark.asyncio
async def test_without_supports_parallel_everything_stays_serial() -> None:
    model = _ScriptedModel()
    tools = _RecordingTools(parallel=False)
    result = await AgentLoop(model, tools).run(
        AgentRequest("s", "t", "/fixture", "task", "scripted")
    )
    assert result.status == RunStatus.COMPLETED
    assert tools.max_in_flight == 1
