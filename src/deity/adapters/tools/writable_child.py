"""ADR-021 B6：可写子 Agent 的分发器——白名单复检，写入只发生在暂存副本。

与 ReadOnlyToolDispatcher 同构：definitions 只广告允许集，prepare 与
dispatch 双重校验。真正的隔离来自子 Run 的 workspace 指向暂存副本
（RunService 负责搭建与回收），本分发器只保证工具面不越界。
写工具的 approve 回调即批次级审批的落点：可写委派在 _delegate 入口
已逐批取得显式批准，此处只复检工具仍在允许集内，不再有交互通道。
"""

from deity.adapters.tools import LocalToolDispatcher
from deity.core.contracts import AgentRequest, ToolCall, ToolResult
from deity.core.contracts.delegation import WRITABLE_DELEGATION_TOOLS


class WritableChildDispatcher:
    """Expose only the contract's tools to a writable child in staging."""

    def __init__(self, allowed_tools: tuple[str, ...]) -> None:
        if not allowed_tools or not set(allowed_tools) <= WRITABLE_DELEGATION_TOOLS:
            raise ValueError("invalid writable delegation tool allowlist")
        self._allowed = frozenset(allowed_tools)
        self._local = LocalToolDispatcher(approve=lambda name, args: name in self._allowed)

    def definitions(self) -> tuple[dict[str, object], ...]:
        """Only advertise child capabilities to the model."""
        return tuple(
            definition
            for definition in self._local.definitions()
            if definition["name"] in self._allowed
        )

    def supports_parallel(self, name: str) -> bool:
        return name in self._allowed and self._local.supports_parallel(name)

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
