"""Memory repository used by deterministic core tests."""

from typing import Any
from uuid import uuid4

from deity.core.contracts import AgentRequest, RunLease, RunStatus, ToolCall, ToolResult
from deity.core.state import transition


class InMemorySessionRepository:
    """Track run states without an infrastructure dependency."""

    def __init__(self) -> None:
        self.statuses: dict[str, RunStatus] = {}
        self.calls: set[tuple[str, str]] = set()
        self.results: dict[tuple[str, str], ToolResult] = {}
        self.steps: dict[str, list[tuple[str, str]]] = {}
        self.usage: dict[str, list[tuple[int | None, int | None]]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.checkpoints: dict[str, list[dict[str, Any]]] = {}
        self.leases: dict[str, RunLease] = {}
        self.compactions: dict[str, list[tuple[int, int, str]]] = {}
        self.model_attempts: dict[str, list[dict[str, Any]]] = {}

    async def create_run(self, request: AgentRequest) -> str:
        """Create a run in CREATED state."""
        run_id = str(uuid4())
        self.statuses[run_id] = RunStatus.CREATED
        self.messages[run_id] = [{"role": "user", "content": request.user_text}]
        return run_id

    async def record_model_turn(
        self, run_id: str, text: str, calls: list[ToolCall], epoch: int | None = None
    ) -> None:
        """Remember a complete assistant turn."""
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                for call in calls
            ]
        self.messages[run_id].append(message)

    async def context_messages(self, run_id: str) -> list[dict[str, Any]]:
        """Read committed model-visible history."""
        return list(self.messages[run_id])

    async def save_checkpoint(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        budget: dict[str, Any],
        epoch: int | None = None,
    ) -> str:
        """Remember a safe frontier in deterministic tests."""
        checkpoint = {
            "id": str(uuid4()),
            "version": len(self.checkpoints.get(run_id, [])) + 1,
            "messages": list(messages),
            "budget": dict(budget),
        }
        self.checkpoints.setdefault(run_id, []).append(checkpoint)
        return str(checkpoint["id"])

    async def latest_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Return the newest in-memory frontier."""
        checkpoints = self.checkpoints.get(run_id, [])
        return checkpoints[-1] if checkpoints else None

    async def set_status(self, run_id: str, status: RunStatus, epoch: int | None = None) -> None:
        """Apply a checked state transition."""
        self.statuses[run_id] = transition(self.statuses[run_id], status)

    async def acquire_lease(self, run_id: str, owner_id: str) -> RunLease:
        """Acquire a single in-memory owner."""
        if run_id in self.leases:
            raise ValueError("run already has an owner")
        lease = RunLease(run_id, owner_id, 1, float("inf"))
        self.leases[run_id] = lease
        return lease

    async def renew_lease(self, lease: RunLease) -> RunLease:
        """Retain the active owner."""
        if self.leases.get(lease.run_id) != lease:
            raise ValueError("stale run owner")
        return lease

    async def release_lease(self, lease: RunLease) -> None:
        """Release an owner after a terminal state."""
        if self.leases.get(lease.run_id) == lease:
            del self.leases[lease.run_id]

    async def claim_tool(
        self,
        run_id: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
        epoch: int | None = None,
    ) -> bool:
        """Reserve a call ID in this run."""
        key = (run_id, call_id)
        if key in self.calls:
            return False
        self.calls.add(key)
        return True

    async def record_tool(self, run_id: str, result: ToolResult, epoch: int | None = None) -> None:
        """Remember a completed tool result."""
        key = (run_id, result.call_id)
        if key not in self.calls:
            raise ValueError("tool call not claimed")
        self.results[key] = result
        self.messages[run_id].append(
            {"role": "tool", "call_id": result.call_id, "content": result.output}
        )

    async def mark_tool_unknown(
        self, run_id: str, call_id: str, reason: str, epoch: int | None = None
    ) -> None:
        """Keep an unresolved call without inventing an outcome."""
        if (run_id, call_id) not in self.calls:
            raise ValueError("tool call not claimed")

    async def record_hook_event(
        self, run_id: str, payload: dict[str, Any], epoch: int | None = None
    ) -> None:
        """Accept a hook audit fact in the in-memory test repository."""
        return

    async def record_step(
        self, run_id: str, kind: str, status: str, epoch: int | None = None
    ) -> None:
        """Remember a model or tool step."""
        self.steps.setdefault(run_id, []).append((kind, status))

    async def record_usage(
        self,
        run_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        epoch: int | None = None,
    ) -> None:
        """Remember provider-reported usage without inventing missing values."""
        self.usage.setdefault(run_id, []).append((input_tokens, output_tokens))

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
    ) -> None:
        """Remember a provider attempt without secrets or raw response data."""
        self.model_attempts.setdefault(run_id, []).append(
            {
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "status": status,
                "code": code,
                "semantic_output": semantic_output,
                "backoff_seconds": backoff_seconds,
            }
        )

    async def record_context_compaction(
        self,
        run_id: str,
        old_estimate: int,
        new_estimate: int,
        summary_hash: str,
        artifact_ids: tuple[str, ...] = (),
        epoch: int | None = None,
    ) -> None:
        """Remember a deterministic context projection."""
        self.compactions.setdefault(run_id, []).append((old_estimate, new_estimate, summary_hash))
