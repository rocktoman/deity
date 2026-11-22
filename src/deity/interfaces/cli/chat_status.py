"""状态栏与 /cost、/status 的数据汇总：只读聚合，不编造数字。"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deity.core.contracts.client import ClientEvent


def fmt_elapsed_compact(elapsed_secs: int) -> str:
    """紧凑耗时格式（Codex status_indicator 式）：`0s` / `1m 00s` / `1h 02m 03s`。"""
    if elapsed_secs < 60:
        return f"{elapsed_secs}s"
    if elapsed_secs < 3600:
        return f"{elapsed_secs // 60}m {elapsed_secs % 60:02d}s"
    hours = elapsed_secs // 3600
    minutes = (elapsed_secs % 3600) // 60
    seconds = elapsed_secs % 60
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


@dataclass
class ChatStatus:
    """一轮/一会话的状态聚合：轮次、工具调用数、耗时、token（有 usage 才有）。"""

    turns: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    last_input_tokens: int | None = None
    context_window_tokens: int | None = None
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
                # 最近一次 usage 的输入量近似当前上下文占用（状态条用）。
                self.last_input_tokens = incoming
            if isinstance(outgoing, int):
                self.output_tokens = (self.output_tokens or 0) + outgoing
        self.elapsed_seconds = self.clock() - self.started_at

    def token_text(self) -> str:
        """token 显示：有 usage 才显示数字，否则显示 —。"""
        if self.input_tokens is None and self.output_tokens is None:
            return "—"
        return f"{self.input_tokens or 0}/{self.output_tokens or 0} tokens"

    def context_text(self) -> str | None:
        """上下文占用：最近输入 token / 窗口；数据不全时返回 None 不编造。"""
        if self.last_input_tokens is None or not self.context_window_tokens:
            return None
        return f"上下文 {self.last_input_tokens}/{self.context_window_tokens}"

    def context_ratio(self) -> float | None:
        """上下文占用比例（最近输入 token / 窗口）；数据不全时返回 None。"""
        if self.last_input_tokens is None or not self.context_window_tokens:
            return None
        return self.last_input_tokens / self.context_window_tokens

    def summary_line(
        self,
        *,
        model_name: str,
        mode: str,
        context_messages: int | None = None,
        current_tool: str | None = None,
    ) -> str:
        """状态栏一行摘要：模型、模式、轮次、工具数、当前工具、token、上下文、耗时。"""
        parts = [
            f"模型 {model_name}",
            mode,
            f"第 {self.turns} 轮",
            f"工具 {self.tool_calls} 次",
        ]
        if current_tool:
            parts.append(f"当前工具 {current_tool}")
        parts.append(self.token_text())
        context = self.context_text()
        if context is not None:
            parts.append(context)
        parts.append(f"{self.elapsed_seconds:.1f}s")
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

    def usage_text(self) -> str:
        """/usage 的文本（ADR-023）：本会话本地用量汇总；无账号配额后端，不编造。"""
        lines = [
            "本会话用量（本地统计，无账号配额后端）：",
            (
                f"轮次：{self.turns} · 工具调用：{self.tool_calls} 次"
                f" · 耗时：{self.elapsed_seconds:.1f}s"
            ),
        ]
        if self.input_tokens is None and self.output_tokens is None:
            lines.append("Token：未接通（usage 事件未到达，未编造数字）")
        else:
            lines.append(
                f"Token：输入 {self.input_tokens or 0} · 输出 {self.output_tokens or 0}"
                f" · 合计 {(self.input_tokens or 0) + (self.output_tokens or 0)}"
            )
        context = self.context_text()
        lines.append(
            f"上下文占用：{context}" if context is not None else "上下文占用：—（无 usage 数据）"
        )
        return "\n".join(lines)


__all__ = ["ChatStatus"]
