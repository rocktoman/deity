"""Textual TUI 入口（需要 `tui` extra）。

本包初始化不导入 Textual；TuiApp 需从 deity.interfaces.tui.app 导入，
未安装时 CLI 会给出安装提示。
"""

from deity.interfaces.tui.view_model import RunViewModel

__all__ = ["RunViewModel"]
