"""状态栏与 /cost、/status 的数据汇总：只读聚合，不编造数字。"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deity.core.contracts.client import ClientEvent


@dataclass
class ChatStatus:
    """一轮/一会话的状态聚合：轮次、工具调用数、耗时、token（有 usage 才有）。"""

    turns: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    elapsed_seconds: float = 0.0
    clock: Callable[[], float] = time.monotonic

    def update(self, event: ClientEvent) -> None:
        """按事件累加计数；usage 缺失时 token 保持 None，不编造。"""
        if event.kind == "message_completed":
            self.turns += 1
        elif event.kind == "tool_call_completed":
            self.tool_calls += 1
        elif event.kind == "usage":
            incoming = event.payload.get("input_tokens")
            outgoing = event.payload.get("output_tokens")
            if isinstance(incoming, int):
                self.input_tokens = (self.input_tokens or 0) + incoming
            if isinstance(outgoing, int):
                self.output_tokens = (self.output_tokens or 0) + outgoing
        self.elapsed_seconds = self.clock() - self.started_at

    def token_text(self) -> str:
        """token 显示：有 usage 才显示数字，否则显示 —。"""
        if self.input_tokens is None and self.output_tokens is None:
            return "—"
        return f"{self.input_tokens or 0}/{self.output_tokens or 0} tokens"

    def summary_line(
        self, *, model_name: str, mode: str, context_messages: int | None = None
    ) -> str:
        """状态栏一行摘要：模型、模式、轮次、工具数、token、耗时。"""
        parts = [
            f"模型 {model_name}",
            mode,
            f"第 {self.turns} 轮",
            f"工具 {self.tool_calls} 次",
            self.token_text(),
            f"{self.elapsed_seconds:.1f}s",
        ]
        if context_messages is not None:
            parts.append(f"上下文 {context_messages} 条")
        return " · ".join(parts)

    def cost_text(self) -> str:
        """/cost 的文本：轮次、工具调用数、耗时；无 usage 时明确标注未接通。"""
        lines = [
            (
                f"本会话：轮次 {self.turns} · 工具调用 {self.tool_calls} 次"
                f" · 耗时 {self.elapsed_seconds:.1f}s"
            )
        ]
        if self.input_tokens is None and self.output_tokens is None:
            lines.append("token 未接通（usage 事件未到达，未编造数字）")
        else:
            lines.append(f"token：输入 {self.input_tokens or 0} · 输出 {self.output_tokens or 0}")
        return "\n".join(lines)


__all__ = ["ChatStatus"]
