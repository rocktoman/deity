"""V0.3 retry and cancellation safety gates."""

import asyncio

import pytest

from deity.core.contracts import (
    AgentRequest,
    ModelEvent,
    ModelRequest,
    RunStatus,
    ToolCall,
    ToolResult,
)
from deity.core.runtime import AgentLoop, BudgetExceeded, ModelFailure, RetryPolicy
from deity.core.storage import InMemorySessionRepository


@pytest.mark.asyncio
async def test_partial_model_stream_is_not_retried() -> None:
    class PartialModel:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ModelRequest):
            self.calls += 1
            yield ModelEvent("text_delta", text="partial")
            raise ModelFailure("model_connection", retryable=True)

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("partial model response must not execute tools")

    model = PartialModel()
    with pytest.raises(ModelFailure):
        await AgentLoop(model, Tool()).run(AgentRequest("s", "t", ".", "goal", "fake"))
    assert model.calls == 1


@pytest.mark.asyncio
async def test_cancel_during_tool_marks_unknown_without_new_dispatch() -> None:
    started = asyncio.Event()
    repo = InMemorySessionRepository()

    class Model:
        async def stream(self, request: ModelRequest):
            yield ModelEvent("tool_call_started", call_id="c1", name="shell")
            yield ModelEvent(
                "tool_call_arguments_delta", call_id="c1", text='{"argv":["sleep","10"]}'
            )
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            started.set()
            await asyncio.Future()
            raise AssertionError

    loop = AgentLoop(Model(), Tool(), repository=repo)
    task = asyncio.create_task(loop.run(AgentRequest("s", "t", ".", "goal", "fake")))
    await started.wait()
    run_id = next(iter(repo.statuses))
    assert loop.cancel(run_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.statuses[run_id] == RunStatus.CANCELLED
    assert (run_id, "c1") not in repo.results


@pytest.mark.asyncio
async def test_provider_usage_exhausts_hard_token_budget() -> None:
    class Model:
        async def stream(self, request: ModelRequest):
            yield ModelEvent("usage", input_tokens=101, output_tokens=5)
            yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("no tool expected")

    with pytest.raises(BudgetExceeded, match="max_input_tokens_total"):
        await AgentLoop(Model(), Tool()).run(
            AgentRequest(
                "s",
                "t",
                ".",
                "goal",
                "fake",
                max_input_tokens_total=100,
                max_output_tokens_total=100,
            )
        )


@pytest.mark.asyncio
async def test_retry_attempts_have_stable_audit_ids_and_stop_reason() -> None:
    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ModelRequest):
            self.calls += 1
            if self.calls == 1:
                raise ModelFailure("model_rate_limited", retryable=True, retry_after=0)
            yield ModelEvent("text_delta", text="done")
            yield ModelEvent("message_completed")

    class Tool:
        async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
            pytest.fail("no tool expected")

    repo = InMemorySessionRepository()
    loop = AgentLoop(Model(), Tool(), repository=repo)
    loop._retry = RetryPolicy(base_delay_seconds=0)
    result = await loop.run(AgentRequest("s", "t", ".", "goal", "fake"))
    attempts = repo.model_attempts[result.run_id]
    assert [event["status"] for event in attempts] == ["started", "retry", "started", "completed"]
    assert attempts[0]["attempt_id"] == attempts[1]["attempt_id"]
    assert attempts[2]["attempt_id"] == attempts[3]["attempt_id"]
    assert attempts[0]["attempt_id"] != attempts[2]["attempt_id"]
    assert attempts[1]["code"] == "model_rate_limited"
