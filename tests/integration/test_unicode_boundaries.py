"""Invalid surrogate input must not crash the interactive run or reach tools."""

from pathlib import Path

import pytest

from deity.adapters.storage import SQLiteSessionRepository
from deity.application.run_service import RunService
from deity.core.contracts import AgentRequest, ModelEvent, ModelRequest, ToolCall, ToolResult
from deity.core.runtime import AgentLoop, ProtocolError


@pytest.mark.asyncio
async def test_user_surrogate_pair_is_normalized_before_persistence(tmp_path: Path) -> None:
    class Model:
        def __init__(self) -> None:
            self.messages: tuple[dict[str, object], ...] = ()

        async def stream(self, request: ModelRequest):
            self.messages = request.messages
            yield ModelEvent("text_delta", text="ok")
            yield ModelEvent("message_completed")

    model = Model()
    service = RunService(db_path=tmp_path / "deity.db", workspace=tmp_path, model=model)
    result = await service.run("写个\ud83d\ude00排序", model_name="fake")
    assert result.final_text == "ok"
    assert model.messages[-1]["content"] == "写个😀排序"


@pytest.mark.asyncio
async def test_model_text_with_isolated_surrogate_is_safe_to_checkpoint(tmp_path: Path) -> None:
    class Model:
        async def stream(self, request: ModelRequest):
            yield ModelEvent("text_delta", text="hello\ud800")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("no tool expected")

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    result = await AgentLoop(Model(), Tool(), repository=repo).run(
        AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    )
    assert result.final_text == "hello�"
    assert (await repo.latest_checkpoint(result.run_id))["messages"][-1]["content"] == "hello�"
    await repo.close()


@pytest.mark.asyncio
async def test_model_surrogate_pair_split_across_chunks_is_joined(tmp_path: Path) -> None:
    class Model:
        async def stream(self, request: ModelRequest):
            yield ModelEvent("text_delta", text="hello\ud83d")
            yield ModelEvent("text_delta", text="\ude00")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("no tool expected")

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    result = await AgentLoop(Model(), Tool(), repository=repo).run(
        AgentRequest("s", "t", str(tmp_path), "goal", "fake")
    )
    assert result.final_text == "hello😀"
    await repo.close()


@pytest.mark.asyncio
async def test_invalid_tool_argument_is_rejected_before_side_effect(tmp_path: Path) -> None:
    class Model:
        async def stream(self, request: ModelRequest):
            yield ModelEvent("tool_call_started", call_id="c1", name="write_file")
            yield ModelEvent(
                "tool_call_arguments_delta",
                call_id="c1",
                text='{"path":"bad\\ud800.py","content":"x"}',
            )
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("invalid argument must not dispatch")

    repo = await SQLiteSessionRepository.open(tmp_path / "deity.db")
    with pytest.raises(ProtocolError, match="Unicode"):
        await AgentLoop(Model(), Tool(), repository=repo).run(
            AgentRequest("s", "t", str(tmp_path), "goal", "fake")
        )
    assert list(tmp_path.glob("bad*")) == []
    await repo.close()
