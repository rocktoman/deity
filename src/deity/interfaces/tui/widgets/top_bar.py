"""顶部单行上下文条（AGY 风格极简 TopContextBar）。"""

from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

_CONTEXT_WARN_RATIO = 0.75


class TopContextBar(Horizontal):
    """顶部单行上下文条：分支 · 模型 · 模式 · 运行态 · 上下文占比。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(id="top-bar", **kwargs)

    def compose(self) -> ComposeResult:
        yield Static("", id="top-bar-left")
        yield Static("", id="top-bar-right")

    def update_state(
        self,
        *,
        model_name: str,
        mode_label: str,
        branch: str | None,
        run_active: bool,
        context_ratio: float | None,
        tokens_text: str,
        colors: dict[str, str],
        width: int,
    ) -> None:
        """按终端宽度与状态刷新顶部上下文条。"""
        left = Text()
        left.append("◉ ", style=f"bold {colors['accent']}")
        left.append("Deity", style="bold")
        if branch and width >= 90:
            left.append(f" · {branch}", style=colors["secondary"])
        left.append(f" · {model_name}", style=colors["secondary"])
        left.append(f" · [{mode_label}]", style=colors["accent"])
        left.append(
            " · 运行中" if run_active else " · 空闲",
            style=colors["warning"] if run_active else colors["tertiary"],
        )

        right = Text()
        if context_ratio is not None:
            pct = round(context_ratio * 100)
            pct_style = (
                colors["error"]
                if context_ratio >= 1
                else colors["warning"]
                if context_ratio >= _CONTEXT_WARN_RATIO
                else colors["secondary"]
            )
            right.append(f"Ctx {pct}%", style=pct_style)
            if tokens_text != "—" and width >= 100:
                right.append(f" · {tokens_text}", style=colors["tertiary"])
        elif tokens_text != "—" and width >= 90:
            right.append(tokens_text, style=colors["tertiary"])

        if width > 0 and cell_len(left.plain) + cell_len(right.plain) + 4 > width:
            right = Text()

        self.query_one("#top-bar-left", Static).update(left)
        self.query_one("#top-bar-right", Static).update(right)


__all__ = ["TopContextBar"]
