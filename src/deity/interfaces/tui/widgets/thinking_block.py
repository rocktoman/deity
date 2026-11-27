"""可折叠原生思考块（ADR-025）：承载模型 reasoning_delta 实时流。"""

import time
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


class ThinkingHeader(Static):
    """思考块标题栏：点击展开或折叠思考原文。"""

    def __init__(self, on_click_cb: Callable[[], None], **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._on_click_cb = on_click_cb

    def on_click(self) -> None:
        self._on_click_cb()


class ThinkingBlock(Vertical):
    """AGY 风格可折叠思考卡片：思考期实时展开，正文/工具开始后自动折叠。"""

    def __init__(self, colors: dict[str, str], **kwargs: Any) -> None:
        kwargs.setdefault("classes", "thinking-block")
        super().__init__(**kwargs)
        self._colors = colors
        self._text = ""
        self._collapsed = False
        self._started_at = time.monotonic()
        self._finished_at: float | None = None

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @property
    def content(self) -> str:
        return self._text

    def compose(self) -> ComposeResult:
        yield ThinkingHeader(self.toggle_collapsed, classes="thinking-header")
        yield Static("", classes="thinking-body")

    def on_mount(self) -> None:
        self._refresh_view()

    def set_text(self, text: str) -> None:
        self._text = text
        self._refresh_view()

    def finish(self, *, auto_collapse: bool = True) -> None:
        if self._finished_at is None:
            self._finished_at = time.monotonic()
        if auto_collapse:
            self._collapsed = True
        self._refresh_view()

    def toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self._refresh_view()

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        elapsed = (
            self._finished_at - self._started_at
            if self._finished_at is not None
            else time.monotonic() - self._started_at
        )
        arrow = "▸ " if self._collapsed else "▾ "
        label = "思考过程（点击展开）" if self._collapsed else "思考过程"
        header = Text()
        header.append(arrow, style=f"bold {self._colors['accent']}")
        header.append(
            f"{label} · {len(self._text)} 字 · {max(0.0, elapsed):.1f}s",
            style=self._colors["tertiary"],
        )
        self.query_one(".thinking-header", ThinkingHeader).update(header)
        body = self.query_one(".thinking-body", Static)
        body.set_class(self._collapsed, "collapsed")
        body.update(self._text)


__all__ = ["ThinkingBlock"]
