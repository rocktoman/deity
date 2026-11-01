"""Fail-closed tool view for read-only child agents."""

from deity.adapters.tools import LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall, ToolResult
from deity.core.contracts.delegation import READ_ONLY_TOOLS


class ReadOnlyToolDispatcher:
    """Expose only approved local read operations and check every call again."""

    def __init__(self, allowed_tools: tuple[str, ...]) -> None:
        if not allowed_tools or not set(allowed_tools) <= READ_ONLY_TOOLS:
            raise ValueError("invalid read-only tool allowlist")
        self._allowed = frozenset(allowed_tools)
        self._local = LocalToolDispatcher()

    def definitions(self) -> tuple[dict[str, object], ...]:
        """Only advertise child capabilities to the model."""
        return tuple(
            definition
            for definition in self._local.definitions()
            if definition["name"] in self._allowed
        )

    async def prepare(self, call: ToolCall, context: AgentRequest) -> ToolResult | None:
        """Reject unauthorized tools before the durable execution claim."""
        if call.name not in self._allowed:
            return ToolResult(call.call_id, "denied", "delegation_tool_denied")
        return await self._local.prepare(call, context)

    async def dispatch(self, call: ToolCall, context: AgentRequest) -> ToolResult:
        """Recheck even when a caller skips prepare."""
        if call.name not in self._allowed:
            return ToolResult(call.call_id, "denied", "delegation_tool_denied")
        return await self._local.dispatch(call, context)
