"""Stable core DTOs and ports."""

import hashlib
import json
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
    max_output_tokens: int | None = None
    run_id: str = ""
    context_window_tokens: int | None = None
    compaction_enabled: bool = False
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None
    provider_name: str = ""
    extension_fingerprint: str = ""

    def config_fingerprint(self) -> str:
        """Hash recovery-relevant settings without including task text or secrets."""
        settings = {
            "model": self.model,
            "provider": self.provider_name,
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "deadline_seconds": self.deadline_seconds,
            "max_output_tokens": self.max_output_tokens,
            "context_window_tokens": self.context_window_tokens,
            "compaction_enabled": self.compaction_enabled,
            "max_input_tokens_total": self.max_input_tokens_total,
            "max_output_tokens_total": self.max_output_tokens_total,
        }
        if self.extension_fingerprint:
            settings["extension_fingerprint"] = self.extension_fingerprint
        encoded = json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class AgentResult:
    run_id: str
    status: RunStatus
    final_text: str | None
    error_code: str | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunLease:
    """Fenced execution ownership for a single run."""

    run_id: str
    owner_id: str
    epoch: int
    expires_at: float


@dataclass(frozen=True)
class RunBudget:
    """Cumulative limits that must survive successor runs."""

    max_turns: int
    max_tool_calls: int
    deadline_seconds: float
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None

    @classmethod
    def from_request(cls, request: AgentRequest) -> "RunBudget":
        """Read immutable limits from the public request."""
        return cls(
            request.max_turns,
            request.max_tool_calls,
            request.deadline_seconds,
            request.max_input_tokens_total,
            request.max_output_tokens_total,
        )


@dataclass(frozen=True)
class RecoveryDecision:
    """Typed preflight result; no external action is implied."""

    run_id: str
    decision: Literal["resume", "manual_review", "abort"]
    reason: str
    checkpoint_id: str | None


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
    async def set_status(
        self, run_id: str, status: RunStatus, epoch: int | None = None
    ) -> None: ...
    async def record_model_turn(
        self, run_id: str, text: str, calls: list[ToolCall], epoch: int | None = None
    ) -> None: ...
    async def context_messages(self, run_id: str) -> list[dict[str, Any]]: ...
    async def save_checkpoint(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        budget: dict[str, Any],
        epoch: int | None = None,
    ) -> str: ...
    async def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None: ...
    async def claim_tool(
        self,
        run_id: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
        epoch: int | None = None,
    ) -> bool: ...
    async def record_tool(
        self, run_id: str, result: ToolResult, epoch: int | None = None
    ) -> None: ...
    async def mark_tool_unknown(
        self, run_id: str, call_id: str, reason: str, epoch: int | None = None
    ) -> None: ...
    async def record_hook_event(
        self, run_id: str, payload: dict[str, Any], epoch: int | None = None
    ) -> None: ...
    async def record_step(
        self, run_id: str, kind: str, status: str, epoch: int | None = None
    ) -> None: ...
    async def record_usage(
        self,
        run_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        epoch: int | None = None,
    ) -> None: ...
    async def record_model_attempt(
        self,
        run_id: str,
        attempt_id: str,
        attempt_number: int,
        status: str,
        code: str | None = None,
        semantic_output: bool = False,
        backoff_seconds: float | None = None,
        epoch: int | None = None,
    ) -> None: ...
    async def record_context_compaction(
        self,
        run_id: str,
        old_estimate: int,
        new_estimate: int,
        summary_hash: str,
        artifact_ids: tuple[str, ...] = (),
        epoch: int | None = None,
    ) -> None: ...
    async def acquire_lease(self, run_id: str, owner_id: str) -> RunLease: ...
    async def renew_lease(self, lease: RunLease) -> RunLease: ...
    async def release_lease(self, lease: RunLease) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, run_id: str, kind: str, payload: dict[str, Any]) -> None: ...
