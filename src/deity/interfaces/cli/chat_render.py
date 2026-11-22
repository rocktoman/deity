"""流式渲染层：消费 AsyncIterator[ClientEvent]，只画视图模型的状态树。

V1.1-M2 起事件折叠归 ChatViewModel（chat_view_model.py），本模块只负责
把 ViewDelta 画成行式增量输出：文本增量按节流阈值分批打印、工具调用
以开始行 + 完成行呈现。理由：行式输出在管道/重定向下与终端下一致、
可逐行断言、长流不重复渲染整屏；Live 的原地更新在非 TTY 下本就不可
用。diff 仍用 Rich Syntax 渲染。

V1.1-M3 渲染增强（均可选，默认关，保持基线行为）：
- render_markdown：模型文本按段落块（空行边界）用 Rich Markdown 渲染，
  尾块在 finish 时渲染；超长尾块仍按节流阈值纯文本刷出以 bound 住缓冲。
- thinking_indicator：无事件到达超过 THINKING_IDLE_SECONDS 时打印一次
  思考/运行提示，事件恢复后自动复位。
- 工具节点折叠：完成行只显示参数摘要，完整参数经 render_tool_details
  展开（/tools 命令）。
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax

from deity.application.approval_diff import render_unified_diff
from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_status import ChatStatus
from deity.interfaces.cli.chat_view_model import ChatViewModel, ToolNode
from deity.interfaces.cli.diff_render import DARK_DIFF_COLORS, format_turn_diff

DIFF_MAX_LINES = 200
TEXT_FLUSH_INTERVAL_SECONDS = 0.05
TEXT_FLUSH_MIN_CHARS = 4096
THINKING_IDLE_SECONDS = 2.0


@dataclass
class RunSummary:
    """一轮流式渲染结束后的汇总：终态、最终文本与状态计数。"""

    run_id: str
    final_text: str | None
    status: str
    turns: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    elapsed_seconds: float


def render_diff(old_text: str, new_text: str, *, path: str = "", max_lines: int = 200) -> str:
    """unified diff（只用于展示，不写盘）；超长截断并提示。"""
    return render_unified_diff(old_text, new_text, path=path, max_lines=max_lines)


def print_diff(
    console: Console, old_text: str, new_text: str, *, path: str = "", max_lines: int = 200
) -> None:
    """把 diff 以语法高亮打印到终端。"""
    console.print(Syntax(render_diff(old_text, new_text, path=path, max_lines=max_lines), "diff"))


class StreamingRenderer:
    """消费 live ClientEvent 流并增量渲染；取消后只打印一行已取消摘要。"""

    def __init__(
        self,
        console: Console,
        *,
        model_name: str = "",
        mode: str = "普通",
        clock: Callable[[], float] = time.monotonic,
        flush_interval_seconds: float = TEXT_FLUSH_INTERVAL_SECONDS,
        flush_min_chars: int = TEXT_FLUSH_MIN_CHARS,
        render_markdown: bool = False,
        thinking_indicator: bool = False,
        context_window_tokens: int | None = None,
    ) -> None:
        self.console = console
        self.model_name = model_name
        self.mode = mode
        self._clock = clock
        self._flush_interval = flush_interval_seconds
        self._flush_min_chars = flush_min_chars
        self._render_markdown = render_markdown
        self._thinking_indicator = thinking_indicator
        self.view = ChatViewModel(clock=clock)
        self.view.status.context_window_tokens = context_window_tokens
        self._flushed_len = 0
        self._last_flush_at = clock()
        self._last_event_at = clock()
        self._thinking_shown = False
        self._line_open = False

    @property
    def _status(self) -> ChatStatus:
        """会话计数状态（视图模型持有，渲染器只读）。"""
        return self.view.status

    @property
    def current_run_id(self) -> str | None:
        """已收到的第一个事件的 run_id（供取消时调用 service.cancel）。"""
        return self.view.run_id

    def status_line(self) -> str:
        """当前状态条一行：轮次/当前工具+耗时/token/上下文/总耗时。"""
        current = self.view.current_tool
        current_text = None
        if current is not None:
            current_text = f"{current.name} {current.elapsed(self._clock):.1f}s"
        return self._status.summary_line(
            model_name=self.model_name, mode=self.mode, current_tool=current_text
        )

    async def render(self, stream: AsyncIterator[ClientEvent]) -> RunSummary:
        """消费整条流后打印终态摘要并返回汇总；流抛错时异常向上传播。

        后台每 flush_interval 刷一次文本：模型停顿时已收到的增量也不会
        一直缓存在内存里（只靠事件驱动刷新时，慢流的首个增量永不显示）；
        同时检查思考指示（长时间无事件时提示一次）。
        """
        stop = asyncio.Event()

        async def _flusher() -> None:
            while not stop.is_set():
                await asyncio.sleep(self._flush_interval)
                self._flush_text()
                self.note_idle()

        flusher = asyncio.create_task(_flusher())
        try:
            async for event in stream:
                self.apply(event)
        finally:
            stop.set()
            flusher.cancel()
            with suppress(asyncio.CancelledError):
                await flusher
        return self.finish()

    def apply(self, event: ClientEvent) -> None:
        """应用一条事件：折叠进视图模型，再按 delta 绘制新增部分。"""
        delta = self.view.apply(event)
        if delta.terminal or delta.text_appended or delta.tool_started or delta.tool_completed:
            self._last_event_at = self._clock()
            self._thinking_shown = False
        if delta.text_appended:
            self._maybe_flush()
        if delta.tool_started is not None:
            self._print_line(f"▸ {delta.tool_started.display_label()}")
        if delta.tool_completed is not None:
            self._complete_tool(delta.tool_completed)
        if delta.diff is not None:
            self._print_turn_diff(delta.diff)

    def note_idle(self) -> None:
        """无事件超过 THINKING_IDLE_SECONDS 时打印一次思考提示（需开启）。"""
        if not self._thinking_indicator or self.view.terminal or self._thinking_shown:
            return
        if self._clock() - self._last_event_at < THINKING_IDLE_SECONDS:
            return
        current = self.view.current_tool
        if current is not None:
            self._print_line(
                f"⋯ {current.name} 运行中（已 {current.elapsed(self._clock):.0f}s）…",
                style="dim",
            )
        else:
            self._print_line("⋯ 思考中…", style="dim")
        self._thinking_shown = True

    def finish(self) -> RunSummary:
        """刷出剩余文本并打印终态摘要；取消时只打印一行已取消。"""
        self._flush_text(final=True)
        status = self.view.status_text
        final_text = self.view.final_text
        if self._line_open:
            self.console.print()
            self._line_open = False
        if status == "cancelled":
            self.console.print("已取消本轮运行。", style="yellow", highlight=False, markup=False)
        else:
            self.console.print(
                self.status_line(),
                highlight=False,
                markup=False,
            )
        return RunSummary(
            run_id=self.view.run_id or "",
            final_text=final_text,
            status=status,
            turns=self._status.turns,
            tool_calls=self._status.tool_calls,
            input_tokens=self._status.input_tokens,
            output_tokens=self._status.output_tokens,
            elapsed_seconds=self._status.elapsed_seconds,
        )

    def render_tool_details(self, call_id: str | None = None) -> None:
        """展开工具节点：打印完整参数（默认全部节点，供 /tools 命令）。"""
        nodes = (
            [self.view.tools[call_id]]
            if call_id is not None and call_id in self.view.tools
            else list(self.view.tools.values())
        )
        for node in nodes:
            mark = "✓" if node.status == "ok" else "✗" if node.status else "▸"
            self._print_line(
                f"  {mark} {node.name}（{node.call_id}）· {node.status or '运行中'}"
                f" · {node.elapsed(self._clock):.2f}s"
            )
            if node.args_text:
                self._print_line(f"    参数：{node.args_text}")
            self._print_tool_output(node)

    def _print_tool_output(self, node: ToolNode) -> None:
        """工具输出预览与退出码（ADR-023）：非零退出码附 `(exit N)`。"""
        if node.exit_code is not None and node.exit_code != 0:
            self._print_line(f"    (exit {node.exit_code})")
        if node.output_preview:
            for line in node.output_preview.splitlines():
                self._print_line(f"    {line}")

    def _complete_tool(self, node: ToolNode) -> None:
        """工具完成行：动词标签 + 状态 + 耗时；参数被截断时标注已折叠。"""
        summary = node.arguments_summary()
        mark = "✓" if node.status == "ok" else "✗"
        folded = " · 已折叠（/tools 展开）" if len(node.args_text) > len(summary) else ""
        exit_part = (
            f" (exit {node.exit_code})"
            if node.exit_code is not None and node.exit_code != 0
            else ""
        )
        self._print_line(
            f"  {mark} {node.display_label()} · {node.status}"
            f" · {node.elapsed(self._clock):.2f}s{exit_part}{folded}"
        )
        # 退出码已附在完成行内，此处只追加预览行（不重复）。
        if node.output_preview:
            for line in node.output_preview.splitlines():
                self._print_line(f"    {line}")

    def _print_turn_diff(self, payload: dict[str, object]) -> None:
        """每轮末 diff（Codex PatchHistoryCell 式）：摘要头 + hunks。"""
        self._flush_text()
        self.console.print(format_turn_diff(payload, DARK_DIFF_COLORS), highlight=False)

    def _maybe_flush(self) -> None:
        """节流刷新：满 50ms 或满 4KB 才打印（R7，具体数值见实施记录）。"""
        pending = self.view.text[self._flushed_len :]
        if not pending:
            return
        if (
            self._clock() - self._last_flush_at >= self._flush_interval
            or len(pending) >= self._flush_min_chars
        ):
            self._flush_text()

    def _flush_text(self, *, final: bool = False) -> None:
        pending = self.view.text[self._flushed_len :]
        if pending and self._render_markdown:
            boundary = pending.rfind("\n\n")
            if boundary >= 0:
                head = pending[:boundary]
                if head.strip():
                    if self._line_open:
                        self.console.print()
                        self._line_open = False
                    self.console.print(Markdown(head))
                self._flushed_len += boundary + 2
                pending = self.view.text[self._flushed_len :]
            if pending and final:
                if self._line_open:
                    self.console.print()
                    self._line_open = False
                self.console.print(Markdown(pending))
                self._flushed_len = len(self.view.text)
                pending = ""
        if pending:
            self.console.print(pending, end="", markup=False, highlight=False)
            self._flushed_len = len(self.view.text)
            self._line_open = True
        self._last_flush_at = self._clock()

    def _print_line(self, text: str, style: str | None = None) -> None:
        """打印一行结构化内容：先刷出待打印文本并换行，避免与流式文本粘连。"""
        self._flush_text()
        if self._line_open:
            self.console.print()
            self._line_open = False
        self.console.print(text, style=style, highlight=False, markup=False)


__all__ = [
    "DIFF_MAX_LINES",
    "TEXT_FLUSH_INTERVAL_SECONDS",
    "TEXT_FLUSH_MIN_CHARS",
    "THINKING_IDLE_SECONDS",
    "RunSummary",
    "StreamingRenderer",
    "print_diff",
    "render_diff",
]
