"""Provider-independent Agent Loop."""

import asyncio
import hashlib
import json
import os
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from deity.core.context import ContextBuilder
from deity.core.context.reliable import (
    ConservativeEstimator,
    ContextBudget,
    ContextCompactor,
    normalize_unicode,
)
from deity.core.contracts import (
    AgentRequest,
    AgentResult,
    ModelEvent,
    ModelProvider,
    ModelRequest,
    RunBudget,
    RunLease,
    RunStatus,
    SessionRepository,
    ToolCall,
    ToolDispatcher,
    ToolResult,
)
from deity.core.hooks import HookPoint, HookRegistry
from deity.core.state import transition
from deity.core.storage import InMemorySessionRepository


class ProtocolError(ValueError):
    """The model stream violates the normalized protocol."""


class PolicyVeto(RuntimeError):
    """A trusted policy hook blocked the next Runtime action."""


class BudgetExceeded(RuntimeError):
    """A configured run budget was exhausted."""


class ModelFailure(RuntimeError):
    """A sanitized provider failure with a deterministic retry policy."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        semantic_output: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.semantic_output = semantic_output


class CancellationScope:
    """Idempotent cancellation signal for one run."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        """Signal cancellation."""
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise before dispatching another effect."""
        if self._event.is_set():
            raise asyncio.CancelledError


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded model retry schedule with provider Retry-After support."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    jitter_fraction: float = 0.2

    def delay(self, attempt: int, retry_after: float | None) -> float:
        """Return a nonnegative delay before the next attempt."""
        if retry_after is not None:
            return max(0.0, retry_after)
        return float(
            self.base_delay_seconds
            * (2**attempt)
            * (1 + float(random.random()) * self.jitter_fraction)
        )


class AgentLoop:
    """Collect complete model turns, then execute tool calls serially."""

    def __init__(
        self,
        model: ModelProvider,
        tools: ToolDispatcher,
        repository: SessionRepository | None = None,
        context: ContextBuilder | None = None,
        workspace_fingerprint: Callable[[], str] | None = None,
        hooks: HookRegistry | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._repository = repository or InMemorySessionRepository()
        self._context = context or ContextBuilder()
        self._workspace_fingerprint = workspace_fingerprint
        self._hooks = hooks
        self._owner = asyncio.Lock()
        self._active_task: asyncio.Task[Any] | None = None
        self._active_run_id: str | None = None
        self._cancellation = CancellationScope()
        self._retry = RetryPolicy()

    def cancel(self, run_id: str) -> bool:
        """Cancel only this loop's active run."""
        if run_id != self._active_run_id or self._active_task is None:
            return False
        self._cancellation.cancel()
        self._active_task.cancel()
        return True

    async def run(
        self, request: AgentRequest, initial_messages: list[dict[str, Any]] | None = None
    ) -> AgentResult:
        """Run a task with a single owner and bounded model/tool turns."""
        request = replace(request, user_text=normalize_unicode(request.user_text))
        if min(request.max_turns, request.max_tool_calls, request.deadline_seconds) <= 0:
            raise ValueError("运行预算必须大于零")
        if self._owner.locked():
            raise RuntimeError("AgentLoop already has an active run")
        async with self._owner:
            return await self._run_owned(request, initial_messages=initial_messages)

    async def run_existing(
        self,
        request: AgentRequest,
        run_id: str,
        messages: list[dict[str, Any]],
        budget: dict[str, Any],
    ) -> AgentResult:
        """Execute a newly created successor run from a verified checkpoint."""
        if self._owner.locked():
            raise RuntimeError("AgentLoop already has an active run")
        async with self._owner:
            return await self._run_owned(request, run_id, messages, budget)

    async def _run_owned(
        self,
        request: AgentRequest,
        existing_run_id: str | None = None,
        initial_messages: list[dict[str, Any]] | None = None,
        initial_budget: dict[str, Any] | None = None,
    ) -> AgentResult:
        run_id = existing_run_id or await self._repository.create_run(request)
        self._active_run_id = run_id
        self._active_task = asyncio.current_task()
        lease = await self._repository.acquire_lease(run_id, f"{os.getpid()}:{uuid4()}")
        current = asyncio.current_task()
        heartbeat = asyncio.create_task(self._heartbeat(lease, current))
        try:
            status = transition(RunStatus.CREATED, RunStatus.RUNNING)
            await self._repository.set_status(run_id, status, epoch=lease.epoch)
            result = await self._execute(
                request, run_id, lease.epoch, initial_messages, initial_budget
            )
        except asyncio.CancelledError:
            await asyncio.wait_for(
                self._repository.set_status(run_id, RunStatus.CANCELLED, epoch=lease.epoch),
                timeout=5,
            )
            raise
        except Exception:
            await self._repository.set_status(run_id, RunStatus.FAILED, epoch=lease.epoch)
            raise
        else:
            await self._repository.set_status(run_id, RunStatus.COMPLETED, epoch=lease.epoch)
            return result
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self._repository.release_lease(lease)
            self._active_run_id = None
            self._active_task = None

    async def _heartbeat(self, lease: RunLease, owner: asyncio.Task[Any] | None) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                lease = await self._repository.renew_lease(lease)
            except ValueError:
                if owner is not None:
                    owner.cancel()
                return

    async def _execute(
        self,
        request: AgentRequest,
        run_id: str,
        epoch: int,
        initial_messages: list[dict[str, Any]] | None = None,
        initial_budget: dict[str, Any] | None = None,
    ) -> AgentResult:
        request = replace(request, run_id=run_id)
        self._config_fingerprint = request.config_fingerprint()
        limits = RunBudget.from_request(request)
        started_at = time.monotonic()
        previous_elapsed = float((initial_budget or {}).get("elapsed_seconds", 0))
        deadline = started_at + limits.deadline_seconds - previous_elapsed
        messages = (
            initial_messages
            if initial_messages is not None
            else self._context.initial(request.user_text)
        )
        if initial_budget is None:
            await self._repository.save_checkpoint(
                run_id, messages, self._budget_payload(0, 0, elapsed_seconds=0), epoch=epoch
            )
        seen: set[str] = set()
        tool_count = int((initial_budget or {}).get("tool_calls", 0))
        input_total = int((initial_budget or {}).get("input_tokens", 0))
        output_total = int((initial_budget or {}).get("output_tokens", 0))
        for turn_index in range(int((initial_budget or {}).get("turns", 0)), limits.max_turns):
            self._cancellation.raise_if_cancelled()
            if time.monotonic() >= deadline:
                raise BudgetExceeded("deadline_seconds")
            definitions = getattr(self._tools, "definitions", None)
            internal_tools = definitions() if callable(definitions) else ()
            wire_to_internal: dict[str, str] = {}
            tools_list: list[dict[str, Any]] = []
            for definition in internal_tools:
                name = str(definition["name"])
                wire_name = (
                    "mcp_" + hashlib.sha256(name.encode()).hexdigest()[:24]
                    if name.startswith("mcp:")
                    else name
                )
                if wire_name in wire_to_internal:
                    raise ProtocolError("重复模型工具别名")
                wire_to_internal[wire_name] = name
                tools_list.append({**definition, "name": wire_name})
            tools = tuple(tools_list)
            if request.compaction_enabled:
                if request.context_window_tokens is None:
                    raise BudgetExceeded("context_window_unknown")
                budget = ContextBudget(
                    request.context_window_tokens,
                    request.max_output_tokens or max(1, request.context_window_tokens // 10),
                    max(1, request.context_window_tokens // 10),
                )
                estimator = ConservativeEstimator()
                schema_tokens = (
                    estimator.estimate([{"role": "system", "content": json.dumps(tools)}])
                    if tools
                    else 0
                )
                old_estimate = estimator.estimate(messages, schema_tokens)
                snapshot = ContextCompactor(estimator).compact(messages, budget, schema_tokens)
                if snapshot.estimated_tokens < old_estimate:
                    await self._repository.record_context_compaction(
                        run_id,
                        old_estimate,
                        snapshot.estimated_tokens,
                        snapshot.sha256,
                        tuple(ref.id for ref in snapshot.artifacts),
                        epoch=epoch,
                    )
                messages = list(snapshot.messages)
            model_request = ModelRequest(
                request.model, tuple(messages), tools, request.max_output_tokens
            )
            await self._run_hook(run_id, epoch, "BeforeModel", {"turn": turn_index})
            for attempt in range(self._retry.max_attempts):
                attempt_id = str(uuid4())
                await self._repository.record_model_attempt(
                    run_id, attempt_id, attempt + 1, "started", epoch=epoch
                )
                try:
                    text, calls, usage = await asyncio.wait_for(
                        self._collect(self._tracked_stream(self._model.stream(model_request))),
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    calls = [
                        replace(call, name=wire_to_internal.get(call.name, call.name))
                        for call in calls
                    ]
                    await self._repository.record_model_attempt(
                        run_id, attempt_id, attempt + 1, "completed", epoch=epoch
                    )
                    break
                except ModelFailure as exc:
                    delay = self._retry.delay(attempt, exc.retry_after)
                    retry = exc.retryable and attempt < self._retry.max_attempts - 1
                    await self._repository.record_model_attempt(
                        run_id,
                        attempt_id,
                        attempt + 1,
                        "retry" if retry else "failed",
                        code=exc.code,
                        semantic_output=exc.semantic_output,
                        backoff_seconds=delay if retry else None,
                        epoch=epoch,
                    )
                    if not exc.retryable or attempt == self._retry.max_attempts - 1:
                        raise
                    if delay >= deadline - time.monotonic():
                        raise BudgetExceeded("deadline_seconds") from exc
                    await asyncio.sleep(delay)
            await self._repository.record_step(run_id, "model", "completed", epoch=epoch)
            if usage != (None, None):
                await self._repository.record_usage(run_id, *usage, epoch=epoch)
            await self._repository.record_model_turn(run_id, text, calls, epoch=epoch)
            if usage[0] is None and request.max_input_tokens_total is not None:
                raise BudgetExceeded("input_token_usage_unknown")
            if usage[1] is None and request.max_output_tokens_total is not None:
                raise BudgetExceeded("output_token_usage_unknown")
            input_total += usage[0] or 0
            output_total += usage[1] or 0
            if (
                limits.max_input_tokens_total is not None
                and input_total > limits.max_input_tokens_total
            ):
                raise BudgetExceeded("max_input_tokens_total")
            if (
                limits.max_output_tokens_total is not None
                and output_total > limits.max_output_tokens_total
            ):
                raise BudgetExceeded("max_output_tokens_total")
            if calls:
                if tool_count + len(calls) > limits.max_tool_calls:
                    raise BudgetExceeded("max_tool_calls")
                for call in calls:
                    if call.call_id in seen:
                        raise ProtocolError("重复 call_id")
                    seen.add(call.call_id)
                messages.append(self._context.assistant_calls(text, calls))
                for call in calls:
                    self._cancellation.raise_if_cancelled()
                    veto = await self._run_hook(
                        run_id,
                        epoch,
                        "BeforeTool",
                        {"call_id": call.call_id, "name": call.name, "arguments": call.arguments},
                    )
                    prepare = getattr(self._tools, "prepare", None)
                    prepared_result: ToolResult | None = (
                        ToolResult(call.call_id, "denied", "hook_veto")
                        if veto
                        else await prepare(call, request)
                        if callable(prepare)
                        else None
                    )
                    if not await self._repository.claim_tool(
                        run_id, call.call_id, call.name, call.arguments, epoch=epoch
                    ):
                        raise ProtocolError("工具调用已被占用")
                    if prepared_result is None:
                        try:
                            result = await asyncio.wait_for(
                                self._tools.dispatch(call, request),
                                timeout=max(0.001, deadline - time.monotonic()),
                            )
                        except (Exception, asyncio.CancelledError) as exc:
                            await self._repository.mark_tool_unknown(
                                run_id, call.call_id, type(exc).__name__, epoch=epoch
                            )
                            raise
                    else:
                        result = prepared_result
                    tool_count += 1
                    await self._repository.record_tool(run_id, result, epoch=epoch)
                    await self._run_hook(
                        run_id,
                        epoch,
                        "AfterTool",
                        {"call_id": call.call_id, "name": call.name, "status": result.status},
                    )
                    await self._repository.record_step(run_id, "tool", result.status, epoch=epoch)
                    messages.append(self._context.tool_result(result))
                await self._repository.save_checkpoint(
                    run_id,
                    messages,
                    self._budget_payload(
                        turn_index + 1,
                        tool_count,
                        input_total,
                        output_total,
                        previous_elapsed + time.monotonic() - started_at,
                    ),
                    epoch=epoch,
                )
                continue
            if text:
                await self._run_hook(run_id, epoch, "BeforeFinish", {"text_length": len(text)})
                messages.append({"role": "assistant", "content": text})
                await self._repository.save_checkpoint(
                    run_id,
                    messages,
                    self._budget_payload(
                        turn_index + 1,
                        tool_count,
                        input_total,
                        output_total,
                        previous_elapsed + time.monotonic() - started_at,
                    ),
                    epoch=epoch,
                )
                return AgentResult(run_id, RunStatus.COMPLETED, text)
            raise ProtocolError("模型响应为空")
        raise BudgetExceeded("max_turns")

    async def _run_hook(
        self, run_id: str, epoch: int, point: HookPoint, facts: dict[str, Any]
    ) -> bool:
        """Audit every registered Hook result before applying a veto."""
        if self._hooks is None:
            return False
        outcomes = await self._hooks.invoke(run_id, point, facts)
        veto = False
        for outcome in outcomes:
            await self._repository.record_hook_event(
                run_id,
                {
                    "point": point,
                    "name": outcome.name,
                    "kind": outcome.kind,
                    "status": outcome.status,
                    "reason": outcome.reason,
                },
                epoch=epoch,
            )
            veto = veto or outcome.status == "veto"
        if veto and point != "BeforeTool":
            raise PolicyVeto(f"{point} vetoed")
        return veto

    @staticmethod
    async def _tracked_stream(stream: AsyncIterator[ModelEvent]) -> AsyncIterator[ModelEvent]:
        semantic_output = False
        try:
            async for event in stream:
                if event.kind in {
                    "text_delta",
                    "tool_call_started",
                    "tool_call_arguments_delta",
                    "message_completed",
                }:
                    semantic_output = True
                yield event
        except ModelFailure as exc:
            if semantic_output:
                raise ModelFailure(exc.code, retryable=False, semantic_output=True) from None
            raise

    def _budget_payload(
        self,
        turns: int,
        tool_calls: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_seconds: float = 0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "turns": turns,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "elapsed_seconds": elapsed_seconds,
        }
        if self._workspace_fingerprint is not None:
            payload["workspace_fingerprint"] = self._workspace_fingerprint()
        payload["config_fingerprint"] = self._config_fingerprint
        return payload

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
            try:
                json.dumps(
                    {"call_id": call_id, "name": name, "arguments": arguments}, ensure_ascii=False
                ).encode("utf-8")
            except UnicodeEncodeError:
                raise ProtocolError("工具参数含非法 Unicode 字符") from None
            calls.append(ToolCall(call_id, name, arguments))
        return normalize_unicode("".join(text_parts)), calls, (input_tokens, output_tokens)
