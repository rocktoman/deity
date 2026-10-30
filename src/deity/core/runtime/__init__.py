"""Provider-independent Agent Loop."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace

from deity.core.context import ContextBuilder
from deity.core.contracts import (
    AgentRequest,
    AgentResult,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    RunStatus,
    SessionRepository,
    ToolCall,
    ToolDispatcher,
)
from deity.core.state import transition
from deity.core.storage import InMemorySessionRepository


class ProtocolError(ValueError):
    """The model stream violates the normalized protocol."""


class BudgetExceeded(RuntimeError):
    """A configured run budget was exhausted."""


class ModelFailure(RuntimeError):
    """A sanitized provider failure with a deterministic retry policy."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AgentLoop:
    """Collect complete model turns, then execute tool calls serially."""

    def __init__(
        self,
        model: ModelProvider,
        tools: ToolDispatcher,
        repository: SessionRepository | None = None,
        context: ContextBuilder | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._repository = repository or InMemorySessionRepository()
        self._context = context or ContextBuilder()
        self._owner = asyncio.Lock()

    async def run(self, request: AgentRequest) -> AgentResult:
        """Run a task with a single owner and bounded model/tool turns."""
        if min(request.max_turns, request.max_tool_calls, request.deadline_seconds) <= 0:
            raise ValueError("运行预算必须大于零")
        if self._owner.locked():
            raise RuntimeError("AgentLoop already has an active run")
        async with self._owner:
            return await self._run_owned(request)

    async def _run_owned(self, request: AgentRequest) -> AgentResult:
        run_id = await self._repository.create_run(request)
        status = transition(RunStatus.CREATED, RunStatus.RUNNING)
        await self._repository.set_status(run_id, status)
        try:
            result = await self._execute(request, run_id)
        except asyncio.CancelledError:
            await self._repository.set_status(run_id, RunStatus.CANCELLED)
            raise
        except Exception:
            await self._repository.set_status(run_id, RunStatus.FAILED)
            raise
        await self._repository.set_status(run_id, RunStatus.COMPLETED)
        return result

    async def _execute(self, request: AgentRequest, run_id: str) -> AgentResult:
        request = replace(request, run_id=run_id)
        deadline = time.monotonic() + request.deadline_seconds
        messages = self._context.initial(request.user_text)
        seen: set[str] = set()
        tool_count = 0
        for _ in range(request.max_turns):
            if time.monotonic() >= deadline:
                raise BudgetExceeded("deadline_seconds")
            definitions = getattr(self._tools, "definitions", None)
            tools = definitions() if callable(definitions) else ()
            model_request = ModelRequest(
                request.model, tuple(messages), tools, request.max_output_tokens
            )
            for attempt in range(3):
                try:
                    text, calls, usage = await asyncio.wait_for(
                        self._collect(self._model.stream(model_request)),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    break
                except ModelFailure as exc:
                    if not exc.retryable or attempt == 2:
                        raise
                    await asyncio.sleep(
                        min(0.05 * (2**attempt), max(0, deadline - time.monotonic()))
                    )
            await self._repository.record_step(run_id, "model", "completed")
            if usage != (None, None):
                await self._repository.record_usage(run_id, *usage)
            if calls:
                if tool_count + len(calls) > request.max_tool_calls:
                    raise BudgetExceeded("max_tool_calls")
                for call in calls:
                    if call.call_id in seen:
                        raise ProtocolError("重复 call_id")
                    seen.add(call.call_id)
                    if not await self._repository.claim_tool(
                        run_id, call.call_id, call.name, call.arguments
                    ):
                        raise ProtocolError("工具调用已被占用")
                messages.append(self._context.assistant_calls(text, calls))
                for call in calls:
                    result = await asyncio.wait_for(
                        self._tools.dispatch(call, request),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    tool_count += 1
                    await self._repository.record_tool(run_id, result)
                    await self._repository.record_step(run_id, "tool", result.status)
                    messages.append(self._context.tool_result(result))
                continue
            if text:
                return AgentResult(run_id, RunStatus.COMPLETED, text)
            raise ProtocolError("模型响应为空")
        raise BudgetExceeded("max_turns")

    @staticmethod
    async def _collect(
        stream: AsyncIterator[ModelEvent],
    ) -> tuple[str, list[ToolCall], tuple[int | None, int | None]]:
        text_parts: list[str] = []
        pending: dict[str, tuple[str, list[str]]] = {}
        completed = False
        finish_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        async for event in stream:
            if not isinstance(event, ModelEvent) or completed:
                raise ProtocolError("非法流事件")
            if event.kind == "text_delta":
                text_parts.append(event.text)
            elif event.kind == "tool_call_started":
                if not event.call_id or not event.name or event.call_id in pending:
                    raise ProtocolError("重复或无效 call_id")
                pending[event.call_id] = (event.name, [])
            elif event.kind == "tool_call_arguments_delta":
                if not event.call_id or event.call_id not in pending:
                    raise ProtocolError("工具参数没有对应调用")
                pending[event.call_id][1].append(event.text)
            elif event.kind == "message_completed":
                completed = True
                finish_reason = event.finish_reason
            elif event.kind == "usage":
                input_tokens = (
                    event.input_tokens if event.input_tokens is not None else input_tokens
                )
                output_tokens = (
                    event.output_tokens if event.output_tokens is not None else output_tokens
                )
            elif event.kind == "error":
                raise ProtocolError("模型流错误")
        if not completed or finish_reason == "length":
            raise ProtocolError("模型消息未完整结束")
        calls: list[ToolCall] = []
        for call_id, (name, fragments) in pending.items():
            try:
                arguments = json.loads("".join(fragments))
            except json.JSONDecodeError as exc:
                raise ProtocolError("工具参数 JSON 不完整") from exc
            if not isinstance(arguments, dict):
                raise ProtocolError("工具参数必须为对象")
            calls.append(ToolCall(call_id, name, arguments))
        return "".join(text_parts), calls, (input_tokens, output_tokens)
