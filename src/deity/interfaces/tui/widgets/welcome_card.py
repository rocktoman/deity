"""神之眼头像欢迎卡片：保留 Deity 启动特征并融入 AGY 极简版式。"""

from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from deity import __version__
from deity.interfaces.tui.state import WELCOME_EMBLEM, greeting_for_hour


class WelcomeCard(Horizontal):
    """启动欢迎卡片：左侧神之眼徽记 + 右侧版本/模型/工作区与快捷提示。"""

    def __init__(
        self,
        *,
        hour: int,
        model_name: str,
        workspace: Path,
        emblem: str = WELCOME_EMBLEM,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("id", "welcome-header")
        super().__init__(**kwargs)
        self._hour = hour
        self._model_name = model_name
        self._workspace = workspace
        self._emblem = emblem

    def compose(self) -> ComposeResult:
        greeting = greeting_for_hour(self._hour)
        text = (
            f"{greeting}，欢迎来到 Deity v{__version__}，我是你的编程助手。\n"
            f"模型：{self._model_name}\n"
            f"工作区：{self._workspace}\n"
            "Enter 提交 · /help 命令 · Ctrl+P 面板 · Ctrl+B 抽屉\n"
            "Shift+Tab 切换模式 · Ctrl+C 取消/退出 · Ctrl+Q 退出"
        )
        yield Static(self._emblem, id="welcome-emblem")
        yield Static(text, id="welcome-text")


__all__ = ["WelcomeCard"]
