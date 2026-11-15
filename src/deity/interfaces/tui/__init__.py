"""Textual TUI 入口（需要 `tui` extra）。

本包初始化不导入 Textual；TuiApp 需从 deity.interfaces.tui.app 导入，
未安装时 CLI 会给出安装提示。

冻结状态（V1.1-M4 决策）：TUI 自 V1.1 起冻结为监视用途——只看 Run
状态、审批与取消/恢复，不再新增功能（无输入框、无对话区）；交互入口
以 CLI 为准。解冻需新的 ADR 取代本决策。
"""

from deity.interfaces.tui.view_model import RunViewModel

__all__ = ["RunViewModel"]
