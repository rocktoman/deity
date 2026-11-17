"""Textual TUI 入口（需要 `tui` extra）。

本包初始化不导入 Textual；应用类需从 deity.interfaces.tui.app /
deity.interfaces.tui.interactive 导入，未安装时 CLI 会给出安装提示。

定位（ADR-022，取代 V1.1-M4 的冻结决策）：
- interactive.InteractiveApp 是全屏交互工作台——TTY 下的默认交互
  界面（底部输入、流式对话、界面内审批、状态栏）；
- app.TuiApp 保留为监视台——经 ClientService 接入已有 Run
  （`deity tui --run-id`），只看状态、批/拒、取消、恢复。
"""

from deity.interfaces.tui.view_model import RunViewModel

__all__ = ["RunViewModel"]
