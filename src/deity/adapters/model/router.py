"""安全模型路由与故障自动切换（ADR-026）：遵循 ModelProvider Protocol。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace

from deity.core.contracts import ModelCapabilities, ModelEvent, ModelProvider, ModelRequest
from deity.core.runtime import ModelFailure

_FALLBACK_CODES = {"model_rate_limited", "model_connection", "model_http"}


@dataclass(frozen=True)
class ModelRoute:
    """一条具名模型路由：绑定的 Provider 与可选覆盖模型名。"""

    name: str
    provider: ModelProvider
    model_name: str | None = None


class RoutedModelProvider:
    """按阶段路由并在无语义输出前安全降级到备用模型的 Provider 包装器。"""

    def __init__(
        self,
        primary: ModelRoute,
        *,
        fallbacks: tuple[ModelRoute, ...] = (),
        fast_route: ModelRoute | None = None,
    ) -> None:
        self.primary = primary
        self.fallbacks = fallbacks
        self.fast_route = fast_route
        self.last_route_name: str = primary.name
        self.fallback_history: list[tuple[str, str, str]] = []

    def capabilities(self) -> ModelCapabilities:
        return self.primary.provider.capabilities()

    def _select_chain(self, request: ModelRequest) -> tuple[ModelRoute, ...]:
        """无工具调用的轻量请求可优先走 fast_route，其余走 primary + fallbacks。"""
        if self.fast_route is not None and not request.tools:
            return (self.fast_route, self.primary, *self.fallbacks)
        return (self.primary, *self.fallbacks)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        chain = self._select_chain(request)
        last_error: ModelFailure | None = None
        for index, route in enumerate(chain):
            routed_req = (
                replace(request, model=route.model_name)
                if route.model_name is not None
                else request
            )
            emitted_semantic = False
            self.last_route_name = route.name
            try:
                async for event in route.provider.stream(routed_req):
                    if event.kind in {
                        "text_delta",
                        "tool_call_started",
                        "tool_call_arguments_delta",
                        "message_completed",
                    }:
                        emitted_semantic = True
                    yield event
                return
            except ModelFailure as exc:
                last_error = exc
                can_fallback = (
                    not emitted_semantic
                    and not exc.semantic_output
                    and (exc.retryable or exc.code in _FALLBACK_CODES)
                    and index + 1 < len(chain)
                )
                if not can_fallback:
                    raise
                next_route = chain[index + 1]
                self.fallback_history.append((route.name, next_route.name, exc.code))
        if last_error is not None:
            raise last_error

    def status_summary(self) -> str:
        """供 /route 命令展示的可读路由与降级摘要。"""
        lines = [
            f"主路由：{self.primary.name}（{self.primary.model_name or '随请求'}）",
            f"最近使用：{self.last_route_name}",
        ]
        if self.fast_route is not None:
            lines.append(
                f"轻量路由：{self.fast_route.name}（{self.fast_route.model_name or '随请求'}）"
            )
        if self.fallbacks:
            names = " → ".join(r.name for r in self.fallbacks)
            lines.append(f"备用降级链：{names}")
        if self.fallback_history:
            lines.append(f"已触发降级次数：{len(self.fallback_history)}")
            for src, dst, code in self.fallback_history[-5:]:
                lines.append(f"  • {src} → {dst}（{code}）")
        return "\n".join(lines)


__all__ = ["ModelRoute", "RoutedModelProvider"]
