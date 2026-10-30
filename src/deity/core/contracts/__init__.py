"""Stable core DTOs and ports."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AgentRequest:
    session_id: str
    task_id: str
    workspace: str
    user_text: str
    model: str
    max_turns: int = 30
    max_tool_calls: int = 100
    deadline_seconds: float = 900
    max_output_tokens: int = 1024
    run_id: str = ""


@dataclass(frozen=True)
class AgentResult:
    run_id: str
    status: RunStatus
    final_text: str | None
    error_code: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    status: Literal["ok", "error", "denied", "timeout", "cancelled"]
    output: str
    exit_code: int | None = None
    truncated: bool = False
    evidence_id: str | None = None


@dataclass(frozen=True)
class ModelEvent:
    kind: Literal[
        "text_delta",
        "tool_call_started",
        "tool_call_arguments_delta",
        "message_completed",
        "usage",
        "error",
    ]
    text: str = ""
    call_id: str | None = None
    name: str | None = None
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool
    streaming: bool
    parallel_tool_calls: bool = False
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    usage_reporting: bool = True


class ModelProvider(Protocol):
    def capabilities(self) -> ModelCapabilities: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class ToolDispatcher(Protocol):
    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult: ...


class SessionRepository(Protocol):
    async def create_run(self, request: AgentRequest) -> str: ...
    async def set_status(self, run_id: str, status: RunStatus) -> None: ...
    async def claim_tool(
        self, run_id: str, call_id: str, name: str, args: dict[str, Any]
    ) -> bool: ...
    async def record_tool(self, run_id: str, result: ToolResult) -> None: ...
    async def record_step(self, run_id: str, kind: str, status: str) -> None: ...
    async def record_usage(
        self, run_id: str, input_tokens: int | None, output_tokens: int | None
    ) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, run_id: str, kind: str, payload: dict[str, Any]) -> None: ...
