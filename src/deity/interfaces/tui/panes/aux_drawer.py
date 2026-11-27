"""右侧三标签辅助抽屉（Sessions / Files / Tasks）。"""

from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from deity.adapters.storage import SQLiteSessionRepository
from deity.interfaces.cli.chat_view_model import ToolNode
from deity.interfaces.cli.diff_render import format_diff_text
from deity.interfaces.cli.workspace_diff import git_branch, workspace_diff
from deity.interfaces.tui.state import DrawerTab


class AuxDrawer(Vertical):
    """AGY 风格右侧辅助抽屉：会话切换、改动文件、任务/队列进度。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(id="aux-drawer", **kwargs)
        self.active_tab: DrawerTab = "sessions"
        self._session_map: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="drawer-tabs"):
            yield Button("会话", id="tab-sessions", classes="drawer-tab active")
            yield Button("文件", id="tab-files", classes="drawer-tab")
            yield Button("任务", id="tab-tasks", classes="drawer-tab")
        with Vertical(id="pane-sessions", classes="drawer-pane active"):
            yield Static("历史会话", id="sidebar-title", classes="drawer-section-title")
            yield VerticalScroll(id="sidebar-sessions")
            yield Static("", id="sidebar-model")
            yield Button("运行 doctor", id="btn-sidebar-doctor")
        with VerticalScroll(id="pane-files", classes="drawer-pane"):
            yield Static("工作区改动", classes="drawer-section-title")
            yield Static("暂无未提交改动。", id="drawer-files-content")
        with VerticalScroll(id="pane-tasks", classes="drawer-pane"):
            yield Static("任务与队列", classes="drawer-section-title")
            yield Static("暂无活动任务。", id="drawer-tasks-content")

    def select_tab(self, tab: DrawerTab) -> None:
        self.active_tab = tab
        tab_ids = {"sessions": "tab-sessions", "files": "tab-files", "tasks": "tab-tasks"}
        pane_ids = {"sessions": "pane-sessions", "files": "pane-files", "tasks": "pane-tasks"}
        for key, btn_id in tab_ids.items():
            self.query_one(f"#{btn_id}", Button).set_class(key == tab, "active")
        for key, pane_id in pane_ids.items():
            self.query_one(f"#{pane_id}").set_class(key == tab, "active")

    def session_for_button(self, button_id: str) -> str | None:
        return self._session_map.get(button_id)

    async def refresh_sessions(
        self, db_path: Path, current_session_id: str, model_name: str
    ) -> None:
        container = self.query_one("#sidebar-sessions", VerticalScroll)
        await container.remove_children()
        self._session_map = {}
        repo = await SQLiteSessionRepository.open(db_path)
        try:
            items = await repo.sessions(include_archived=True)
        finally:
            await repo.close()
        if not items:
            await container.mount(Static("（暂无已存储会话）直接输入任务即可开始新会话。"))
        for index, item in enumerate(items):
            current = item["id"] == current_session_id
            title = f" {item['title'][:16]}" if item["title"] else ""
            bid = f"session-row-{index}"
            self._session_map[bid] = item["id"]
            await container.mount(
                Button(
                    f"{'● ' if current else ''}{item['id'][:8]}{title}",
                    id=bid,
                    classes="session-row current" if current else "session-row",
                )
            )
        self.query_one("#sidebar-model", Static).update(f"模型：{model_name}")

    async def refresh_files(self, workspace: Path, colors: dict[str, str]) -> None:
        target = self.query_one("#drawer-files-content", Static)
        result = await workspace_diff(workspace)
        diff_text = str(result.get("diff", ""))
        untracked = result.get("untracked", [])
        branch = git_branch(workspace)
        if not diff_text and not untracked:
            prefix = f"分支：{branch}\n" if branch else ""
            target.update(f"{prefix}工作区无未提交改动。")
            return
        rendered = Text()
        if branch:
            rendered.append(f"分支：{branch}\n", style=colors["secondary"])
        if untracked:
            rendered.append(f"未追踪文件（{len(untracked)}）：\n", style=colors["accent"])
            for path in untracked[:20]:
                rendered.append(f"  + {path}\n", style=colors["tertiary"])
        if diff_text:
            rendered.append("\n")
            rendered.append_text(format_diff_text(diff_text, colors, max_lines=80))
        target.update(rendered)

    def refresh_tasks(
        self,
        *,
        run_active: bool,
        run_id: str | None,
        last_plan_id: str | None,
        tools: dict[str, ToolNode],
        queue_items: tuple[str, ...],
        shell_sessions: tuple[dict[str, object], ...] = (),
    ) -> None:
        lines: list[str] = []
        state = "运行中" if run_active else "空闲"
        lines.append(f"状态：{state}")
        if run_id:
            lines.append(f"Run ID：{run_id[:8]}")
        if last_plan_id:
            lines.append(f"最近计划：{last_plan_id[:8]}")
        if shell_sessions:
            lines.append(f"\n后台 Shell 会话（{len(shell_sessions)}）：")
            for sess in shell_sessions:
                status_tag = "运行中" if sess.get("running") else f"已退出({sess.get('exit_code')})"
                lines.append(f"  $ {sess.get('name')} (pid {sess.get('pid')}) [{status_tag}]")
        if queue_items:
            lines.append(f"\n待执行队列（{len(queue_items)}）：")
            for idx, item in enumerate(queue_items, start=1):
                lines.append(f"  {idx}. {item[:40]}")
        if tools:
            lines.append(f"\n本轮工具（{len(tools)}）：")
            for node in list(tools.values())[-12:]:
                status = node.status or "运行中"
                lines.append(f"  • {node.display_label()} [{status}]")
        else:
            lines.append("\n本轮暂无工具调用。")
        self.query_one("#drawer-tasks-content", Static).update("\n".join(lines))


__all__ = ["AuxDrawer"]
