"""流式渲染层：消费 AsyncIterator[ClientEvent]，行式增量输出。

设计说明（与方案 §4.2 的一点偏差）：不用 Rich Live 做原地刷新，改用行式
增量输出——文本增量按节流阈值分批打印、工具调用以开始行 + 完成行呈现。
理由：行式输出在管道/重定向下与终端下一致、可逐行断言、长流不重复渲染
整屏；Live 的原地更新在非 TTY 下本就不可用。diff 仍用 Rich Syntax 渲染。
"""

import asyncio
import difflib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from rich.console import Console
from rich.syntax import Syntax

from deity.core.contracts.client import ClientEvent
from deity.interfaces.cli.chat_status import ChatStatus

DIFF_MAX_LINES = 200
TEXT_FLUSH_INTERVAL_SECONDS = 0.05
TEXT_FLUSH_MIN_CHARS = 4096


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


@dataclass
class _ToolCallNode:
    call_id: str
    name: str
    fragments: list[str] = field(default_factory=list)
    started_at: float = 0.0
    status: str | None = None


def render_diff(old_text: str, new_text: str, *, path: str = "", max_lines: int = 200) -> str:
    """unified diff（只用于展示，不写盘）；超长截断并提示。"""
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{path}" if path else "a/old",
            tofile=f"b/{path}" if path else "b/new",
            lineterm="",
        )
    )
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"…（已截断，只显示前 {max_lines} 行）"]
    return "\n".join(lines) + ("\n" if lines else "")


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
    ) -> None:
        self.console = console
        self.model_name = model_name
        self.mode = mode
        self._clock = clock
        self._flush_interval = flush_interval_seconds
        self._flush_min_chars = flush_min_chars
        self._status = ChatStatus(clock=clock)
        self._text = ""
        self._flushed_len = 0
        self._last_flush_at = clock()
        self._line_open = False
        self._tools: dict[str, _ToolCallNode] = {}
        self._run_id: str | None = None
        self._terminal: ClientEvent | None = None

    @property
    def current_run_id(self) -> str | None:
        """已收到的第一个事件的 run_id（供取消时调用 service.cancel）。"""
        return self._run_id

    async def render(self, stream: AsyncIterator[ClientEvent]) -> RunSummary:
        """消费整条流后打印终态摘要并返回汇总；流抛错时异常向上传播。

        后台每 flush_interval 刷一次文本：模型停顿时已收到的增量也不会
        一直缓存在内存里（只靠事件驱动刷新时，慢流的首个增量永不显示）。
        """
        stop = asyncio.Event()

        async def _flusher() -> None:
            while not stop.is_set():
                await asyncio.sleep(self._flush_interval)
                self._flush_text()

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
        """应用一条事件；终态事件之后到达的事件一律丢弃（取消竞态，R6）。"""
        if self._terminal is not None:
            return
        if self._run_id is None:
            self._run_id = event.run_id
        if event.kind == "text_delta":
            self._text += str(event.payload.get("text", ""))
            self._maybe_flush()
        elif event.kind == "tool_call_started":
            call_id = str(event.payload.get("call_id", ""))
            name = str(event.payload.get("name", ""))
            self._tools[call_id] = _ToolCallNode(
                call_id=call_id, name=name, started_at=self._clock()
            )
            self._print_line(f"▸ {name}")
        elif event.kind == "tool_call_arguments_delta":
            node = self._tools.get(str(event.payload.get("call_id", "")))
            if node is not None:
                node.fragments.append(str(event.payload.get("text", "")))
        elif event.kind == "tool_call_completed":
            self._complete_tool(event)
        elif event.kind == "message_completed" or event.kind == "usage":
            self._status.update(event)
        elif event.kind == "run_status":
            self._terminal = event
            self._status.update(event)

    def finish(self) -> RunSummary:
        """刷出剩余文本并打印终态摘要；取消时只打印一行已取消。"""
        self._flush_text()
        terminal = self._terminal
        status = str(terminal.payload.get("status", "")) if terminal is not None else ""
        if terminal is not None:
            raw_final = terminal.payload.get("final_text")
            final_text = raw_final if isinstance(raw_final, str) else None
        else:
            final_text = self._text
        if self._line_open:
            self.console.print()
            self._line_open = False
        if status == "cancelled":
            self.console.print("已取消本轮运行。", style="yellow", highlight=False, markup=False)
        else:
            self.console.print(
                self._status.summary_line(model_name=self.model_name, mode=self.mode),
                highlight=False,
                markup=False,
            )
        return RunSummary(
            run_id=self._run_id or "",
            final_text=final_text,
            status=status,
            turns=self._status.turns,
            tool_calls=self._status.tool_calls,
            input_tokens=self._status.input_tokens,
            output_tokens=self._status.output_tokens,
            elapsed_seconds=self._status.elapsed_seconds,
        )

    def _complete_tool(self, event: ClientEvent) -> None:
        """工具完成行：名 + 参数摘要 + 状态 + 耗时。"""
        self._status.update(event)
        call_id = str(event.payload.get("call_id", ""))
        name = str(event.payload.get("name", ""))
        status = str(event.payload.get("status", ""))
        node = self._tools.get(call_id)
        elapsed = self._clock() - node.started_at if node is not None else 0.0
        summary = self._arguments_summary(node)
        mark = "✓" if status == "ok" else "✗"
        detail = f"({summary}) " if summary else ""
        self._print_line(f"  {mark} {name}{detail}· {status} · {elapsed:.2f}s")

    @staticmethod
    def _arguments_summary(node: _ToolCallNode | None) -> str:
        """参数摘要：去掉文件内容全文、超长截断（与审批摘要同一口径）。"""
        if node is None or not node.fragments:
            return ""
        import json

        from deity.core.contracts.client import approval_args_summary

        raw = "".join(node.fragments)
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw[:120]
        if not isinstance(args, dict):
            return raw[:120]
        summary = approval_args_summary(args)
        text = json.dumps(summary, ensure_ascii=False)
        return text[:120]

    def _maybe_flush(self) -> None:
        """节流刷新：满 50ms 或满 4KB 才打印（R7，具体数值见实施记录）。"""
        pending = self._text[self._flushed_len :]
        if not pending:
            return
        if (
            self._clock() - self._last_flush_at >= self._flush_interval
            or len(pending) >= self._flush_min_chars
        ):
            self._flush_text()

    def _flush_text(self) -> None:
        pending = self._text[self._flushed_len :]
        if pending:
            self.console.print(pending, end="", markup=False, highlight=False)
            self._flushed_len = len(self._text)
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
    "RunSummary",
    "StreamingRenderer",
    "print_diff",
    "render_diff",
]
