"""工具调用组、单条工具块与每轮 Diff 块组件。"""

import time
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from deity.interfaces.cli.chat_view_model import ToolNode
from deity.interfaces.cli.diff_render import (
    format_diff_text,
    format_turn_diff,
    format_turn_diff_summary,
)
from deity.interfaces.tui.state import ToolGroupState


class ToolBlock(Static):
    """可交互工具条目：支持点击切换展开/折叠完整参数与输出。"""

    def __init__(
        self,
        node: ToolNode,
        on_toggle: Callable[[str], None],
        formatter: Callable[[ToolNode], str | Text],
        **kwargs: Any,
    ) -> None:
        super().__init__(formatter(node), **kwargs)
        self.node = node
        self._on_toggle = on_toggle
        self._formatter = formatter

    def on_click(self) -> None:
        self._on_toggle(self.node.call_id)

    def refresh_node(self) -> None:
        self.update(self._formatter(self.node))


class ToolGroupHeader(Static):
    """工具组可点击标题行：点击折叠/展开组内工具列表。"""

    def __init__(self, on_click_cb: Callable[[], None], **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._on_click_cb = on_click_cb

    def on_click(self) -> None:
        self._on_click_cb()


class ToolGroupWidget(Vertical):
    """AGY 风格可折叠工具阶段组：头部汇总进度/耗时，内部容纳 ToolBlock。"""

    def __init__(self, state: ToolGroupState, colors: dict[str, str], **kwargs: Any) -> None:
        kwargs.setdefault("classes", "tool-group")
        super().__init__(**kwargs)
        self.group_state = state
        self._colors = colors
        self._pending_blocks: list[ToolBlock] = []

    def compose(self) -> ComposeResult:
        yield ToolGroupHeader(self.toggle_collapsed, classes="tool-group-header")
        yield Vertical(classes="tool-group-body")

    def on_mount(self) -> None:
        if self._pending_blocks:
            body = self.query_one(".tool-group-body", Vertical)
            for block in self._pending_blocks:
                body.mount(block)
            self._pending_blocks.clear()
        self.query_one(".tool-group-body", Vertical).set_class(
            self.group_state.collapsed, "collapsed"
        )
        self.refresh_header()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.group_state.collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self.group_state.collapsed = collapsed
        if self.is_mounted:
            self.query_one(".tool-group-body", Vertical).set_class(collapsed, "collapsed")
            self.refresh_header()

    def add_tool_block(self, block: ToolBlock) -> None:
        self.group_state.add_tool(block.node.call_id)
        if self.is_mounted:
            self.query_one(".tool-group-body", Vertical).mount(block)
            self.refresh_header()
        else:
            self._pending_blocks.append(block)

    def mark_completed(self, status: str) -> None:
        self.group_state.mark_completed(status)
        self.refresh_header()

    def refresh_header(self) -> None:
        if not self.is_mounted:
            return
        arrow = "▸ " if self.group_state.collapsed else "▾ "
        line = Text()
        line.append(arrow, style=f"bold {self._colors['accent']}")
        line.append(self.group_state.summary_label(time.monotonic), style=self._colors["secondary"])
        self.query_one(".tool-group-header", ToolGroupHeader).update(line)


class DiffBlock(Static):
    """可折叠 diff 块：默认展开 hunks，点击折叠回摘要。"""

    def __init__(
        self,
        payload: dict[str, Any] | None,
        colors: dict[str, str],
        *,
        raw_text: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._payload = payload
        self._colors = colors
        self._raw_text = raw_text
        self._expanded = True
        if raw_text is not None:
            super().__init__(format_diff_text(raw_text, colors), **kwargs)
        else:
            super().__init__(format_turn_diff(payload or {}, colors), **kwargs)

    def on_click(self) -> None:
        self._expanded = not self._expanded
        if self._raw_text is not None:
            if self._expanded:
                self.update(format_diff_text(self._raw_text, self._colors))
            else:
                preview = "\n".join(self._raw_text.splitlines()[:20])
                self.update(format_diff_text(preview + "\n… 点击展开", self._colors))
            return
        if self._expanded:
            self.update(format_turn_diff(self._payload or {}, self._colors))
        else:
            self.update(format_turn_diff_summary(self._payload or {}, self._colors))


__all__ = ["DiffBlock", "ToolBlock", "ToolGroupWidget"]
