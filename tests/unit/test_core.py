import asyncio
import json

import pytest

from deity.core.context import ContextBuilder
from deity.core.contracts import ModelEvent, RunStatus, ToolResult
from deity.core.runtime import AgentLoop, AgentRequest, BudgetExceeded, ModelFailure, ProtocolError
from deity.core.state import transition
from deity.core.storage import InMemorySessionRepository


class FakeModel:
    def __init__(self, turns: list[list[ModelEvent]]) -> None:
        self.turns = iter(turns)

    async def stream(self, request: object):
        for event in next(self.turns):
            yield event


class FakeTool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def dispatch(self, call, context):
        self.calls.append(call.call_id)
        return ToolResult(call.call_id, "ok", "done")


def test_terminal_state_cannot_restart() -> None:
    assert transition(RunStatus.CREATED, RunStatus.RUNNING) == RunStatus.RUNNING
    with pytest.raises(ValueError):
        transition(RunStatus.COMPLETED, RunStatus.RUNNING)


@pytest.mark.asyncio
async def test_text_only_result() -> None:
    loop = AgentLoop(
        FakeModel([[ModelEvent("text_delta", text="hello"), ModelEvent("message_completed")]]),
        FakeTool(),
    )
    result = await loop.run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "hello"


@pytest.mark.asyncio
async def test_fragments_wait_for_completion() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="c1", name="read_file"),
                ModelEvent("tool_call_arguments_delta", call_id="c1", text='{"path":'),
                ModelEvent("tool_call_arguments_delta", call_id="c1", text='"a"}'),
                ModelEvent("message_completed"),
            ],
            [ModelEvent("text_delta", text="ok"), ModelEvent("message_completed")],
        ]
    )
    result = await AgentLoop(model, tool).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert result.status == RunStatus.COMPLETED
    assert tool.calls == ["c1"]


@pytest.mark.asyncio
async def test_incomplete_stream_never_executes_tool() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="c1", name="write_file"),
                ModelEvent("tool_call_arguments_delta", call_id="c1", text="{"),
            ]
        ]
    )
    with pytest.raises(ProtocolError):
        await AgentLoop(model, tool).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert tool.calls == []


@pytest.mark.asyncio
async def test_duplicate_call_id_rejected() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="c1", name="x"),
                ModelEvent("tool_call_arguments_delta", call_id="c1", text="{}"),
                ModelEvent("tool_call_started", call_id="c1", name="x"),
                ModelEvent("message_completed"),
            ]
        ]
    )
    with pytest.raises(ProtocolError):
        await AgentLoop(model, tool).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert tool.calls == []


@pytest.mark.asyncio
async def test_two_tools_execute_in_order() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="a", name="read_file"),
                ModelEvent("tool_call_arguments_delta", call_id="a", text="{}"),
                ModelEvent("tool_call_started", call_id="b", name="grep"),
                ModelEvent("tool_call_arguments_delta", call_id="b", text="{}"),
                ModelEvent("message_completed"),
            ],
            [ModelEvent("text_delta", text="done"), ModelEvent("message_completed")],
        ]
    )
    result = await AgentLoop(model, tool).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert result.status == RunStatus.COMPLETED
    assert tool.calls == ["a", "b"]


@pytest.mark.asyncio
async def test_tool_budget_rejects_entire_batch() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="a", name="x"),
                ModelEvent("tool_call_arguments_delta", call_id="a", text="{}"),
                ModelEvent("tool_call_started", call_id="b", name="x"),
                ModelEvent("tool_call_arguments_delta", call_id="b", text="{}"),
                ModelEvent("message_completed"),
            ]
        ]
    )
    with pytest.raises(BudgetExceeded):
        await AgentLoop(model, tool).run(
            AgentRequest("s", "t", "/tmp", "hi", "fake", max_tool_calls=1)
        )
    assert tool.calls == []


@pytest.mark.asyncio
async def test_truncated_message_never_executes() -> None:
    tool = FakeTool()
    model = FakeModel(
        [
            [
                ModelEvent("tool_call_started", call_id="a", name="write_file"),
                ModelEvent("tool_call_arguments_delta", call_id="a", text="{}"),
                ModelEvent("message_completed", finish_reason="length"),
            ]
        ]
    )
    with pytest.raises(ProtocolError):
        await AgentLoop(model, tool).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert tool.calls == []


@pytest.mark.asyncio
async def test_failure_is_terminal_in_repository() -> None:
    repo = InMemorySessionRepository()
    model = FakeModel([[ModelEvent("error")]])
    with pytest.raises(ProtocolError):
        await AgentLoop(model, FakeTool(), repository=repo).run(
            AgentRequest("s", "t", "/tmp", "hi", "fake")
        )
    assert list(repo.statuses.values()) == [RunStatus.FAILED]


@pytest.mark.asyncio
async def test_cancellation_is_terminal_in_repository() -> None:
    class SlowModel:
        async def stream(self, request):
            await asyncio.sleep(10)
            yield ModelEvent("message_completed")

    repo = InMemorySessionRepository()
    task = asyncio.create_task(
        AgentLoop(SlowModel(), FakeTool(), repository=repo).run(
            AgentRequest("s", "t", "/tmp", "hi", "fake")
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(repo.statuses.values()) == [RunStatus.CANCELLED]


def test_context_is_serializable_and_bounded() -> None:
    context = ContextBuilder(max_tool_chars=4)
    messages = context.initial("hello")
    messages.append(context.tool_result(ToolResult("c", "ok", "abcdef")))
    assert messages[-1]["truncated"] is True
    assert "abcd" in json.dumps(messages, ensure_ascii=False)


def test_project_instructions_are_context_not_authority() -> None:
    context = ContextBuilder(project_instructions="read-only project note")
    messages = context.initial("hello")
    assert messages[0]["role"] == "system"
    assert "read-only project note" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "hello"}


@pytest.mark.asyncio
async def test_transient_model_error_retries_before_tools() -> None:
    class FlakyModel:
        def __init__(self) -> None:
            self.attempts = 0

        async def stream(self, request):
            self.attempts += 1
            if self.attempts == 1:
                raise ModelFailure("model_rate_limited", retryable=True)
            yield ModelEvent("text_delta", text="ok")
            yield ModelEvent("message_completed")

    model = FlakyModel()
    result = await AgentLoop(model, FakeTool()).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert result.status == RunStatus.COMPLETED
    assert model.attempts == 2


@pytest.mark.asyncio
async def test_auth_model_error_does_not_retry() -> None:
    class AuthModel:
        def __init__(self) -> None:
            self.attempts = 0

        async def stream(self, request):
            self.attempts += 1
            raise ModelFailure("model_auth", retryable=False)
            yield ModelEvent("message_completed")

    model = AuthModel()
    with pytest.raises(ModelFailure):
        await AgentLoop(model, FakeTool()).run(AgentRequest("s", "t", "/tmp", "hi", "fake"))
    assert model.attempts == 1


@pytest.mark.asyncio
async def test_loop_rejects_second_owner() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel:
        async def stream(self, request):
            entered.set()
            await release.wait()
            yield ModelEvent("text_delta", text="ok")
            yield ModelEvent("message_completed")

    loop = AgentLoop(BlockingModel(), FakeTool())
    request = AgentRequest("s", "t", "/tmp", "hi", "fake")
    first = asyncio.create_task(loop.run(request))
    await entered.wait()
    with pytest.raises(RuntimeError, match="active run"):
        await loop.run(request)
    release.set()
    assert (await first).status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_model_usage_is_recorded_when_reported() -> None:
    repo = InMemorySessionRepository()
    model = FakeModel(
        [
            [
                ModelEvent("usage", input_tokens=7, output_tokens=3),
                ModelEvent("text_delta", text="ok"),
                ModelEvent("message_completed"),
            ]
        ]
    )
    result = await AgentLoop(model, FakeTool(), repository=repo).run(
        AgentRequest("s", "t", "/tmp", "hi", "fake")
    )
    assert repo.usage[result.run_id] == [(7, 3)]
