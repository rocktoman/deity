"""Validated model-facing delegation tool; coordination stays in Application."""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deity.core.contracts import AgentRequest, ToolCall, ToolDispatcher, ToolResult
from deity.core.contracts.delegation import DelegationBudget, DelegationRequest


class ChildTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=8000)
    allowed_tools: list[str] = Field(min_length=1)
    # ADR-021 B6：writable 任务在暂存副本里执行、回收 patch（需配置开启）。
    mode: str = Field(default="read_only", pattern="^(read_only|writable)$")
    max_turns: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    deadline_seconds: float = Field(gt=0)
    max_input_tokens_total: int | None = Field(default=None, ge=1)
    max_output_tokens_total: int | None = Field(default=None, ge=1)


class DelegateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[ChildTaskArgs] = Field(min_length=1, max_length=32)


class DelegationDispatcher:
    """Wrap an existing dispatcher without bypassing its tool pipeline."""

    def __init__(
        self,
        inner: ToolDispatcher,
        delegate: Callable[[tuple[DelegationRequest, ...], AgentRequest], Awaitable[str]],
        max_children: int,
    ) -> None:
        self.inner = inner
        self.delegate = delegate
        self.max_children = max_children
        self._prepared: dict[tuple[str, str], tuple[DelegationRequest, ...]] = {}

    def definitions(self) -> tuple[dict[str, Any], ...]:
        """Advertise a bounded batch delegation schema."""
        inner = getattr(self.inner, "definitions", None)
        definitions = inner() if callable(inner) else ()
        return (
            *definitions,
            {
                "name": "delegate",
                "description": (
                    "Delegate bounded child tasks. Default mode is read_only analysis; "
                    "mode=writable runs the child in a staging copy and returns a "
                    "reviewable patch instead of touching the workspace "
                    "(requires writable delegation enabled; never auto-approved)."
                ),
                "parameters": DelegateArgs.model_json_schema(),
            },
        )

    def supports_parallel(self, name: str) -> bool:
        if name == "delegate":
            return False
        check = getattr(self.inner, "supports_parallel", None)
        return bool(check(name)) if callable(check) else False

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        """Validate a complete batch before the Runtime claims the tool call."""
        if call.name != "delegate":
            inner = getattr(self.inner, "prepare", None)
            return await inner(call, context) if callable(inner) else None
        if not context.run_id:
            return ToolResult(call.call_id, "denied", "parent_run_id_required")
        try:
            args = DelegateArgs.model_validate(call.arguments)
            if len(args.tasks) > self.max_children:
                raise ValueError("too many child tasks")
            requests = tuple(
                DelegationRequest(
                    f"{context.run_id}:{call.call_id}:{index}",
                    context.run_id,
                    task.goal,
                    tuple(task.allowed_tools),
                    DelegationBudget(
                        task.max_turns,
                        task.max_tool_calls,
                        task.deadline_seconds,
                        task.max_input_tokens_total,
                        task.max_output_tokens_total,
                    ),
                    mode=task.mode,  # type: ignore[arg-type]
                )
                for index, task in enumerate(args.tasks)
            )
        except (ValidationError, ValueError):
            return ToolResult(call.call_id, "denied", "invalid_delegation")
        self._prepared[(context.run_id, call.call_id)] = requests
        return None

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        """Run only the previously validated child batch."""
        if call.name != "delegate":
            return await self.inner.dispatch(call, context)
        key = (context.run_id, call.call_id)
        requests = self._prepared.pop(key, None)
        if requests is None:
            early = await self.prepare(call, context)
            if early is not None:
                return early
            requests = self._prepared.pop(key)
        try:
            output = await self.delegate(requests, context)
        except ValueError as exc:
            return ToolResult(call.call_id, "denied", str(exc))
        return ToolResult(call.call_id, "ok", output)
