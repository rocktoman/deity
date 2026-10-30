"""Memory repository used by deterministic core tests."""

from typing import Any
from uuid import uuid4

from deity.core.contracts import AgentRequest, RunStatus, ToolResult
from deity.core.state import transition


class InMemorySessionRepository:
    """Track run states without an infrastructure dependency."""

    def __init__(self) -> None:
        self.statuses: dict[str, RunStatus] = {}
        self.calls: set[tuple[str, str]] = set()
        self.results: dict[tuple[str, str], ToolResult] = {}
        self.steps: dict[str, list[tuple[str, str]]] = {}
        self.usage: dict[str, list[tuple[int | None, int | None]]] = {}

    async def create_run(self, request: AgentRequest) -> str:
        """Create a run in CREATED state."""
        run_id = str(uuid4())
        self.statuses[run_id] = RunStatus.CREATED
        return run_id

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        """Apply a checked state transition."""
        self.statuses[run_id] = transition(self.statuses[run_id], status)

    async def claim_tool(self, run_id: str, call_id: str, name: str, args: dict[str, Any]) -> bool:
        """Reserve a call ID in this run."""
        key = (run_id, call_id)
        if key in self.calls:
            return False
        self.calls.add(key)
        return True

    async def record_tool(self, run_id: str, result: ToolResult) -> None:
        """Remember a completed tool result."""
        key = (run_id, result.call_id)
        if key not in self.calls:
            raise ValueError("tool call not claimed")
        self.results[key] = result

    async def record_step(self, run_id: str, kind: str, status: str) -> None:
        """Remember a model or tool step."""
        self.steps.setdefault(run_id, []).append((kind, status))

    async def record_usage(
        self, run_id: str, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        """Remember provider-reported usage without inventing missing values."""
        self.usage.setdefault(run_id, []).append((input_tokens, output_tokens))
